# ATM-with-blockchain-logging-fingerprint-authentication
Raspberry Pi-based smart ATM prototype implementing multi-factor authentication (PIN + fingerprint) with blockchain-based transaction logging on Ethereum Sepolia testnet. All transactions are hashed and stored immutably for tamper-proof auditing.

This branch also includes a secure SQL user store (`secure_users.db`) managed from Python, with AES-256 encryption for sensitive user information and seeded mock users for development.
