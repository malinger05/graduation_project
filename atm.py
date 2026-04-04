import json
import os
import hashlib
import time
from datetime import datetime
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

# ============================================
# CONFIGURATION (use .env — see .env.example)
# ============================================

CONTRACT_ADDRESS = os.environ.get("CONTRACT_ADDRESS", "").strip()
ETH_PRIVATE_KEY = os.environ.get("ETH_PRIVATE_KEY", "").strip()

# ============================================
# SMART CONTRACT ABI (simplified)
# ============================================

CONTRACT_ABI = [
    {
        "inputs": [{"internalType": "string", "name": "_transactionHash", "type": "string"}],
        "name": "storeLog",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "getLogCount",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# ============================================
# BLOCKCHAIN LOGGER CLASS
# ============================================

class BlockchainLogger:
    def __init__(self):
        if not CONTRACT_ADDRESS or not ETH_PRIVATE_KEY:
            raise ValueError(
                "Missing CONTRACT_ADDRESS or ETH_PRIVATE_KEY. "
                "Copy .env.example to .env and set your deployed contract address and wallet private key."
            )
        print("Connecting to blockchain...")
        self.w3 = Web3(Web3.HTTPProvider('https://ethereum-sepolia.publicnode.com'))
        
        if not self.w3.is_connected():
            raise Exception("Failed to connect to Sepolia")
        print("✓ Connected to Sepolia")
        
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=CONTRACT_ABI
        )
        self.account = self.w3.eth.account.from_key(ETH_PRIVATE_KEY)
        print(f"✓ Blockchain logger ready")
    
    def create_log_hash(self, transaction_data):
        """Create unique hash from transaction details"""
        timestamp = datetime.now().isoformat()
        data_string = f"{transaction_data}|{timestamp}"
        return hashlib.sha256(data_string.encode()).hexdigest()
    
  
    def store_transaction_log(self, transaction_details):
        """Store transaction log on blockchain"""
        log_hash = self.create_log_hash(transaction_details)
        
        try:
            # Build transaction
            store_txn = self.contract.functions.storeLog(log_hash).build_transaction({
                'from': self.account.address,
                'nonce': self.w3.eth.get_transaction_count(self.account.address),
                'gas': 200000,
                'gasPrice': self.w3.eth.gas_price
            })
            
            # Sign the transaction
            signed_txn = self.account.sign_transaction(store_txn)
            
            # Send the transaction - FIX for web3 v6+
            if hasattr(signed_txn, 'raw_transaction'):
                tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            else:
                tx_hash = self.w3.eth.send_raw_transaction(signed_txn.rawTransaction)
            
            print(f"  📝 Blockchain record: {tx_hash.hex()[:20]}...")
            return {'success': True, 'tx_hash': tx_hash.hex(), 'log_hash': log_hash}
            
        except Exception as e:
            print(f"  ❌ Blockchain error: {e}")
            return {'success': False, 'log_hash': log_hash}
# ============================================
# ATM SYSTEM (with database for balances)
# ============================================

ACCOUNTS_FILE = "accounts.json"
TRANSACTIONS_FILE = "transactions.json"

class ATMWithBlockchain:
    def __init__(self, blockchain_logger):
        self.blockchain = blockchain_logger
        self.accounts = {}
        self.current_account = None
        self.load_accounts()
        self.load_transactions()
    
    def load_accounts(self):
        """Load balances from database (JSON file)"""
        if os.path.exists(ACCOUNTS_FILE):
            with open(ACCOUNTS_FILE, 'r') as f:
                self.accounts = json.load(f)
            print(f"✓ Loaded {len(self.accounts)} accounts")
        else:
            # Demo accounts
            self.accounts = {
                "1001": {"name": "John Doe", "pin": "1234", "balance": 500},
                "1002": {"name": "Jane Smith", "pin": "5678", "balance": 1000},
                "1003": {"name": "Bob Wilson", "pin": "9012", "balance": 250}
            }
            self.save_accounts()
            print("✓ Created demo accounts")
    
    def save_accounts(self):
        with open(ACCOUNTS_FILE, 'w') as f:
            json.dump(self.accounts, f, indent=2)
    
    def load_transactions(self):
        """Load local transaction history"""
        if os.path.exists(TRANSACTIONS_FILE):
            with open(TRANSACTIONS_FILE, 'r') as f:
                self.local_transactions = json.load(f)
        else:
            self.local_transactions = []
    
    def save_transaction(self, transaction):
        self.local_transactions.append(transaction)
        with open(TRANSACTIONS_FILE, 'w') as f:
            json.dump(self.local_transactions, f, indent=2)
    
    def authenticate(self, account_number, pin):
        if account_number in self.accounts:
            if self.accounts[account_number]["pin"] == pin:
                self.current_account = account_number
                print(f"\n✓ Welcome {self.accounts[account_number]['name']}!")
                return True
        print("\n❌ Invalid credentials")
        return False
    
    def withdraw(self, amount):
        account = self.accounts[self.current_account]
        
        if amount <= 0:
            return False, "Amount must be positive"
        
        if amount > account["balance"]:
            return False, f"Insufficient funds. Balance: ${account['balance']}"
        
        # Update balance in database
        old_balance = account["balance"]
        account["balance"] -= amount
        self.save_accounts()
        
        # Create log entry
        log_entry = {
            "type": "WITHDRAW",
            "account": self.current_account,
            "name": account["name"],
            "amount": amount,
            "old_balance": old_balance,
            "new_balance": account["balance"],
            "timestamp": datetime.now().isoformat(),
            "status": "SUCCESS"
        }
        
        # Store on blockchain
        print(f"\n  Recording withdrawal on blockchain...")
        blockchain_result = self.blockchain.store_transaction_log(json.dumps(log_entry))
        
        log_entry["blockchain_tx"] = blockchain_result.get("tx_hash", "FAILED")
        log_entry["blockchain_hash"] = blockchain_result.get("log_hash", "")
        self.save_transaction(log_entry)
        
        return True, f"Withdrew ${amount}. New balance: ${account['balance']}", blockchain_result['success']
    
    def deposit(self, amount):
        account = self.accounts[self.current_account]
        
        if amount <= 0:
            return False, "Amount must be positive"
        
        # Update balance in database
        old_balance = account["balance"]
        account["balance"] += amount
        self.save_accounts()
        
        # Create log entry
        log_entry = {
            "type": "DEPOSIT",
            "account": self.current_account,
            "name": account["name"],
            "amount": amount,
            "old_balance": old_balance,
            "new_balance": account["balance"],
            "timestamp": datetime.now().isoformat(),
            "status": "SUCCESS"
        }
        
        # Store on blockchain
        print(f"\n  Recording deposit on blockchain...")
        blockchain_result = self.blockchain.store_transaction_log(json.dumps(log_entry))
        
        log_entry["blockchain_tx"] = blockchain_result.get("tx_hash", "FAILED")
        log_entry["blockchain_hash"] = blockchain_result.get("log_hash", "")
        self.save_transaction(log_entry)
        
        return True, f"Deposited ${amount}. New balance: ${account['balance']}", blockchain_result['success']
    
    def check_balance(self):
        return self.accounts[self.current_account]["balance"]
    
    def show_transaction_history(self):
        print("\n" + "="*60)
        print("TRANSACTION HISTORY (with blockchain proof)")
        print("="*60)
        
        user_txns = [t for t in self.local_transactions if t.get("account") == self.current_account]
        
        if not user_txns:
            print("No transactions found")
            return
        
        for txn in user_txns[-10:]:  # Show last 10
            blockchain_status = "✓ ON CHAIN" if txn.get("blockchain_tx") != "FAILED" else "❌ NOT ON CHAIN"
            print(f"\n  {txn['type']}: ${txn['amount']} | {txn['timestamp'][:19]}")
            print(f"    Balance: ${txn['old_balance']} → ${txn['new_balance']}")
            print(f"    Blockchain: {blockchain_status}")
            print(f"    Hash: {txn.get('blockchain_hash', 'N/A')[:30]}...")
    
    def logout(self):
        self.current_account = None
        print("\n✓ Logged out")

# ============================================
# MAIN PROGRAM
# ============================================

def main():
    print("="*50)
    print("ATM WITH BLOCKCHAIN LOGGING")
    print("="*50)
    
    # Initialize blockchain connection
    try:
        blockchain = BlockchainLogger()
    except Exception as e:
        print(f"Cannot connect to blockchain: {e}")
        print("Make sure you're online and contract address is correct")
        return
    
    # Initialize ATM
    atm = ATMWithBlockchain(blockchain)
    
    print("\nDemo Accounts:")
    print("  Account 1001 | PIN: 1234 | John Doe | Balance: $500")
    print("  Account 1002 | PIN: 5678 | Jane Smith | Balance: $1000")
    print("  Account 1003 | PIN: 9012 | Bob Wilson | Balance: $250")
    
    while True:
        print("\n" + "-"*30)
        print("1. Login")
        print("2. Exit")
        choice = input("Choose: ")
        
        if choice == "1":
            acc_num = input("Account number: ")
            pin = input("PIN: ")
            
            if atm.authenticate(acc_num, pin):
                while True:
                    print(f"\n--- Main Menu ---")
                    print(f"Balance: ${atm.check_balance()}")
                    print("1. Withdraw")
                    print("2. Deposit")
                    print("3. Check Balance")
                    print("4. Transaction History")
                    print("5. Logout")
                    
                    action = input("Choose: ")
                    
                    if action == "1":
                        amount = float(input("Amount to withdraw: $"))
                        success, msg, blockchain_ok = atm.withdraw(amount)
                        print(f"\n{msg}")
                        if blockchain_ok:
                            print("✓ Recorded permanently on blockchain")
                        else:
                            print("⚠️ Transaction recorded locally but blockchain pending")
                    
                    elif action == "2":
                        amount = float(input("Amount to deposit: $"))
                        success, msg, blockchain_ok = atm.deposit(amount)
                        print(f"\n{msg}")
                        if blockchain_ok:
                            print("✓ Recorded permanently on blockchain")
                    
                    elif action == "3":
                        print(f"\nYour balance: ${atm.check_balance()}")
                    
                    elif action == "4":
                        atm.show_transaction_history()
                    
                    elif action == "5":
                        atm.logout()
                        break
        
        elif choice == "2":
            print("Goodbye!")
            break

if __name__ == "__main__":
    main()