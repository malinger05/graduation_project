import json
import os
import hashlib
import qrcode
from PIL import Image  # Comes with qrcode
import time
from datetime import datetime
from web3 import Web3
from dotenv import load_dotenv
import requests
import base64
from cryptography.fernet import Fernet
from pinatapy import PinataPy
from secure_user_db import SecureUserDatabase
load_dotenv()

# ============================================
# CONFIGURATION (use .env — see .env.example)
# ============================================

PINATA_API_KEY = os.environ.get("PINATA_API_KEY", "").strip()
PINATA_API_SECRET = os.environ.get("PINATA_API_SECRET", "").strip()
CONTRACT_ADDRESS = os.environ.get("CONTRACT_ADDRESS", "").strip()
ETH_PRIVATE_KEY = os.environ.get("ETH_PRIVATE_KEY", "").strip()
if CONTRACT_ADDRESS:
    print(f"DEBUG: atm.py using contract address: {CONTRACT_ADDRESS}")
#Python (Fernet) → Creates key → Encrypts log → Sends to IPFS
ENCRYPTION_KEY_FILE = "encryption_key.key"
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
    },
    {
    "inputs": [{"internalType": "string", "name": "_logHash", "type": "string"}],
    "name": "verifyLog",
    "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
    "stateMutability": "view",
    "type": "function"
}
]
# ============================================
# IPFS STORAGE CLASS
# ============================================

class IPFSStorage:
    def __init__(self):
        # Connect to Pinata
        self.pinata = PinataPy(PINATA_API_KEY, PINATA_API_SECRET)
        self.encryption_key = self.get_or_create_encryption_key()
        self.cipher = Fernet(self.encryption_key)
        print("✓ IPFS storage ready")
    
    def get_or_create_encryption_key(self):
        """Load existing encryption key or create a new one"""
        if os.path.exists(ENCRYPTION_KEY_FILE):
            with open(ENCRYPTION_KEY_FILE, 'rb') as f:
                return f.read()
        else:
            # Create new key and save it
            key = Fernet.generate_key()
            with open(ENCRYPTION_KEY_FILE, 'wb') as f:
                f.write(key)
            print(f"✓ Created new encryption key: {ENCRYPTION_KEY_FILE}")
            print("⚠️  KEEP THIS FILE SAFE! Needed to decrypt logs.")
            return key
    
    def encrypt_log(self, log_data):
        """Encrypt transaction log before uploading"""
        # Convert dict to string
        log_string = json.dumps(log_data)
        # Encrypt
        encrypted = self.cipher.encrypt(log_string.encode())
        # Convert to base64 for easy storage
        return base64.b64encode(encrypted).decode()
    
    def upload_to_ipfs(self, encrypted_log):
        """Upload encrypted log to IPFS via Pinata"""
        try:
            # Create a JSON object with the encrypted log
            data = {
                "encrypted_log": encrypted_log,
                "timestamp": datetime.now().isoformat(),
                "version": "1.0"
            }
            
            # Upload to IPFS
            result = self.pinata.pin_json_to_ipfs(data)
            ipfs_hash = result['IpfsHash']
            
            print(f"  📦 IPFS CID: {ipfs_hash}...")
            return ipfs_hash
            
        except Exception as e:
            print(f"  ❌ IPFS upload error: {e}")
            return None
    
    def retrieve_from_ipfs(self, ipfs_hash):
        """Retrieve and decrypt log from IPFS"""
        try:
            # Fetch from IPFS gateway
            url = f"https://gateway.pinata.cloud/ipfs/{ipfs_hash}"
            response = requests.get(url)
            data = response.json()
            
            # Decrypt
            encrypted = base64.b64decode(data['encrypted_log'])
            decrypted = self.cipher.decrypt(encrypted)
            
            return json.loads(decrypted.decode())
            
        except Exception as e:
            print(f"❌ IPFS retrieval error: {e}")
            return None


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
        print(" Connected to Sepolia")
        
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
    
    def verify_transaction(self, log_hash):
        """Check if a transaction hash exists on the blockchain"""
        try:
            print(f"DEBUG: Calling contract.verifyLog with: {log_hash[:30]}...")  # ← ADD
            exists = self.contract.functions.verifyLog(log_hash).call()
            print(f"DEBUG: Contract returned: {exists}")  # ← ADD
            return exists
        except Exception as e:
            print(f"Verification error: {e}")
            return False
  
    def store_transaction_log(self, transaction_details, ipfs_storage=None):
        """Store transaction log on blockchain + IPFS"""
        
        # Step 1: Create log hash (for blockchain)
        log_hash = self.create_log_hash(transaction_details)
        print(f"DEBUG: Created log_hash: {log_hash}")  # ← ADD THIS
        
        # Step 2: Upload to IPFS (if available)
        ipfs_cid = None
        if ipfs_storage:
            encrypted_log = ipfs_storage.encrypt_log(transaction_details)
            ipfs_cid = ipfs_storage.upload_to_ipfs(encrypted_log)
        
        # Step 3: Store ONLY the hash on blockchain
        try:
            print(f"DEBUG: Storing on blockchain: {log_hash[:30]}...")  # ← ADD THIS
            
            store_txn = self.contract.functions.storeLog(log_hash).build_transaction({
                'from': self.account.address,
                'nonce': self.w3.eth.get_transaction_count(self.account.address),
                'gas': 200000,
                'gasPrice': self.w3.eth.gas_price
            })
            
            signed_txn = self.account.sign_transaction(store_txn)
            
            if hasattr(signed_txn, 'raw_transaction'):
                tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            else:
                tx_hash = self.w3.eth.send_raw_transaction(signed_txn.rawTransaction)
            
            print(f"  📝 Blockchain: {tx_hash.hex()[:20]}...")
            return {
                'success': True, 
                'tx_hash': tx_hash.hex(), 
                'log_hash': log_hash,
                'ipfs_cid': ipfs_cid
            }
            
        except Exception as e:
            print(f"  ❌ Blockchain error: {e}")
            return {'success': False, 'log_hash': log_hash, 'ipfs_cid': ipfs_cid}


# ============================================
# ATM SYSTEM (with database for balances)
# ============================================

TRANSACTIONS_FILE = "transactions.json"

class ATMWithBlockchain:
    def __init__(self, blockchain_logger):
        self.blockchain = blockchain_logger
        self.current_account = None
        self.current_user = None
        self.user_db = SecureUserDatabase()
        self.ipfs = IPFSStorage()
        self.load_transactions()
    
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
        user = self.user_db.verify_credentials(account_number, pin)
        if user:
            self.current_account = account_number
            self.current_user = user
            print(f"\n✓ Welcome {user['full_name']}!")
            return True
        print("\n❌ Invalid credentials")
        return False
    def verify_integrity(self, transaction):
        """Check if a transaction has been tampered with"""
        
        stored_hash = transaction.get("blockchain_hash")
        
        print(f"DEBUG: Checking hash: {stored_hash}")  # ← ADD THIS
        
        if not stored_hash:
            return False, "No blockchain hash found"
        
        exists = self.blockchain.verify_transaction(stored_hash)
        print(f"DEBUG: verify_transaction returned: {exists}")  # ← ADD THIS
        if exists:
            return True, "✅ AUTHENTIC - Hash verified on blockchain"
        else:
            return False, "❌ Transaction is TAMPERED - Hash not found on blockchain"
    def withdraw(self, amount):
        account = self.user_db.get_user_by_account(self.current_account)
        if not account:
            return False, "Account not found", False
        
        if amount <= 0:
            return False, "Amount must be positive", False
        
        if amount > account["balance"]:
            return False, f"Insufficient funds. Balance: ${account['balance']}", False
        
        # Update balance in database
        old_balance = account["balance"]
        new_balance = old_balance - amount
        self.user_db.update_balance(self.current_account, new_balance)
        if self.current_user:
            self.current_user["balance"] = new_balance
        # Create log entry
        log_entry = {
            "type": "WITHDRAW",
            "account": self.current_account,
            "name": account["full_name"],
            "user_id": account["user_id"],
            "amount": amount,
            "old_balance": old_balance,
            "new_balance": new_balance,
            "timestamp": datetime.now().isoformat(),
            "status": "SUCCESS"
        }
        
        # Store on blockchain
        print(f"\n  Recording withdrawal on blockchain...")
        blockchain_result = self.blockchain.store_transaction_log(json.dumps(log_entry), self.ipfs)
        
        log_entry["blockchain_tx"] = blockchain_result.get("tx_hash", "FAILED")
        log_entry["blockchain_hash"] = blockchain_result.get("log_hash", "")
        log_entry["ipfs_cid"] = blockchain_result.get("ipfs_cid", "")
        self.save_transaction(log_entry)
        # After blockchain_result is successful
        if blockchain_result['success']:
            print("\n  📱 Generating QR receipt...")
            self.generate_qr_receipt(log_entry)
        return True, f"Withdrew ${amount}. New balance: ${new_balance}", blockchain_result['success']
    
    def deposit(self, amount):
        account = self.user_db.get_user_by_account(self.current_account)
        if not account:
            return False, "Account not found", False
        
        if amount <= 0:
            return False, "Amount must be positive", False
        
        # Update balance in database
        old_balance = account["balance"]
        new_balance = old_balance + amount
        self.user_db.update_balance(self.current_account, new_balance)
        if self.current_user:
            self.current_user["balance"] = new_balance
        
        # Create log entry
        log_entry = {
            "type": "DEPOSIT",
            "account": self.current_account,
            "name": account["full_name"],
            "user_id": account["user_id"],
            "amount": amount,
            "old_balance": old_balance,
            "new_balance": new_balance,
            "timestamp": datetime.now().isoformat(),
            "status": "SUCCESS"
        }
        
        # Store on blockchain
        print(f"\n  Recording deposit on blockchain...")
        blockchain_result = self.blockchain.store_transaction_log(json.dumps(log_entry), self.ipfs)
        
        log_entry["blockchain_tx"] = blockchain_result.get("tx_hash", "FAILED")
        log_entry["blockchain_hash"] = blockchain_result.get("log_hash", "")
        log_entry["ipfs_cid"] = blockchain_result.get("ipfs_cid", "")
        self.save_transaction(log_entry)
        
        return True, f"Deposited ${amount}. New balance: ${new_balance}", blockchain_result['success']
    
    def check_balance(self):
        account = self.user_db.get_user_by_account(self.current_account)
        if not account:
            return 0
        if self.current_user:
            self.current_user["balance"] = account["balance"]
        return account["balance"]
    
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
    
    def generate_qr_receipt(self, transaction):
        """Generate QR code receipt for a transaction"""
        
        # Get transaction details
        tx_hash = transaction.get("blockchain_tx", "")
        log_hash = transaction.get("blockchain_hash", "")
        amount = transaction.get("amount", 0)
        txn_type = transaction.get("type", "TRANSACTION")
        timestamp = transaction.get("timestamp", "")
        
        if not tx_hash or tx_hash == "FAILED":
            print("❌ Cannot generate QR: Transaction not on blockchain")
            return None

        # Create blockchain explorer link
        blockchain_link = f"https://sepolia.etherscan.io/tx/{tx_hash}"
        
        # Create receipt data (what the QR code will contain)
        receipt_data = {
            "transaction_type": txn_type,
            "amount": f"${amount}",
            "timestamp": timestamp,
            "blockchain_tx": tx_hash,
            "blockchain_hash": log_hash,
            "verify_link": blockchain_link,
            "atm_id": "ATM-001",
            "verified": "https://sepolia.etherscan.io/verify"
        }
        
        # Convert to readable format for QR
        qr_text = f"""
        ATM TRANSACTION RECEIPT
        =======================
        Type: {txn_type}
        Amount: ${amount}
        Time: {timestamp}
        Blockchain TX: {tx_hash[:20]}...
        Verify: {blockchain_link}
        """
        
        # Generate QR code
        qr = qrcode.QRCode(
            version=1,  # Size (1 = smallest)
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(blockchain_link)  # Store the blockchain link
        qr.make(fit=True)
        
        # Create image
        qr_image = qr.make_image(fill_color="black", back_color="white")
        
        # Save with timestamp
        filename = f"receipt_{txn_type}_{amount}_{timestamp[:19].replace(':', '-')}.png"
        qr_image.save(filename)
        
        print(f"\n  📱 QR Code saved: {filename}")
        print(f"  🔗 Scan to view on blockchain: {blockchain_link[:50]}...")
        
        return filename

    def export_audit_report(self):
        """Export all verified transactions to CSV and PDF"""
        import csv
        from datetime import datetime
        
        if not self.local_transactions:
            print("\n❌ No transactions to export")
            return
        
        # Create filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_filename = f"audit_report_{timestamp}.csv"
        
        # Write to CSV
        with open(csv_filename, 'w', newline='') as csvfile:
            fieldnames = ['type', 'account', 'user_id', 'name', 'amount', 'old_balance',
                        'new_balance', 'timestamp', 'status', 'blockchain_tx',
                        'blockchain_hash', 'ipfs_cid', 'verified']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            writer.writeheader()
            
            # Verify each transaction and add to report
            verified_count = 0
            tampered_count = 0
            
            for txn in self.local_transactions:
                # Check if transaction is verified on blockchain
                stored_hash = txn.get("blockchain_hash")
                if stored_hash:
                    is_verified = self.blockchain.verify_transaction(stored_hash)
                    txn['verified'] = "YES" if is_verified else "NO (Tampered or Old)"
                    if is_verified:
                        verified_count += 1
                    else:
                        tampered_count += 1
                else:
                    txn['verified'] = "NO (No blockchain record)"
                    tampered_count += 1
                
                writer.writerow(txn)
        
        print(f"\n✅ Audit report exported: {csv_filename}")
        print(f"   Total transactions: {len(self.local_transactions)}")
        print(f"   ✅ Verified on blockchain: {verified_count}")
        print(f"   ❌ Not verified: {tampered_count}")
        
        return csv_filename
    def logout(self):
        self.current_account = None
        self.current_user = None
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
                    print("6. Verify All Transactions")
                    print("7. Export Audit Report (CSV)")
                    print("8. Show QR for Last Transaction")
                    print("9. Logout")
                    
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
                    elif action == "6":
                        print("\n" + "="*50)
                        print("VERIFYING ALL TRANSACTIONS")
                        print("="*50)
                        
                        user_txns = [t for t in atm.local_transactions if t.get("account") == atm.current_account]
                        
                        if not user_txns:
                            print("No transactions to verify")
                        else:
                            all_valid = True
                            for i, txn in enumerate(user_txns):
                                valid, msg = atm.verify_integrity(txn)
                                print(f"\nTransaction {i+1}: {msg}")
                                if not valid:
                                    all_valid = False
                            
                            if all_valid:
                                print("\n✅ ALL TRANSACTIONS ARE AUTHENTIC!")
                            else:
                                print("\n❌ SOME TRANSACTIONS HAVE BEEN TAMPERED!")
                    elif action == "7":
                        atm.export_audit_report()

                    elif action == "8":
                        user_txns = [t for t in atm.local_transactions if t.get("account") == atm.current_account]
                        if user_txns:
                            last_txn = user_txns[-1]
                            atm.generate_qr_receipt(last_txn)
                        else:
                            print("No transactions found")

                    elif action == "9":
                        atm.logout()
                        break

if __name__ == "__main__":
    main()