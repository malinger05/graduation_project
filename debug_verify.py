from web3 import Web3
import json
import os
from dotenv import load_dotenv

load_dotenv()

CONTRACT_ADDRESS = os.environ.get("CONTRACT_ADDRESS", "").strip()
ETH_PRIVATE_KEY = os.environ.get("ETH_PRIVATE_KEY", "").strip()
print(f"Using contract address: {CONTRACT_ADDRESS}")
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
    },
    {
        "inputs": [{"internalType": "uint256", "name": "index", "type": "uint256"}],
        "name": "getLog",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Connect
w3 = Web3(Web3.HTTPProvider('https://ethereum-sepolia.publicnode.com'))
contract = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=CONTRACT_ABI)

# Get count
count = contract.functions.getLogCount().call()
print(f"Total hashes stored: {count}")

# Get all hashes
print("\nHashes in contract:")
for i in range(count):
    hash_val = contract.functions.getLog(i).call()
    print(f"  {i}: {hash_val[:50]}...")

# Test verify on last hash
if count > 0:
    last_hash = contract.functions.getLog(count-1).call()
    print(f"\nLast hash: {last_hash[:50]}...")
    exists = contract.functions.verifyLog(last_hash).call()
    print(f"verifyLog('{last_hash[:30]}...') returns: {exists}")
else:
    print("\nNo hashes in contract! Make a new transaction first.")