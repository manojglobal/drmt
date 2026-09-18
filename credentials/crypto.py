import hashlib
import secrets
import base64
import os
import bcrypt
import pyotp
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

def hash_password(password: str) -> str:
    # Creates a secure bcrypt hash of a plaintext password.
    salt = bcrypt.gensalt(rounds=14)
    return bcrypt.hashpw(password.encode(), salt).decode()

def verify_password(password: str, hash_str: str) -> bool:
    # Checks if a password matches the stored bcrypt hash.
    try:
        return bcrypt.checkpw(password.encode(), hash_str.encode())
    except ValueError:
        return False

def generate_session_token() -> str:
    # Creates a random, secure token for user sessions.
    return secrets.token_hex(32)

def sha256_hex(data: str) -> str:
    # Generates a SHA-256 hash for secure data checking.
    return hashlib.sha256(data.encode()).hexdigest()

def md5_hex(data: str) -> str:
    # Generates an MD5 hash for legacy broker APIs.
    return hashlib.md5(data.encode()).hexdigest()

def totp_now(secret: str) -> str:
    # Generates a time-based one-time password (TOTP) for broker logins.
    if not secret:
        return ""
    # Strip spaces for MStock
    secret = secret.replace(" ", "")
    totp = pyotp.TOTP(secret)
    return totp.now()

def _get_cryptojs_key_iv(password: str, salt: bytes):
    """Generates 48 bytes of key+iv using MD5 like CryptoJS."""
    key = password.encode('utf-8')
    d = d_i = b''
    while len(d) < 48:
        d_i = hashlib.md5(d_i + key + salt).digest()
        d += d_i
    return d[:32], d[32:48]

def encrypt_password_alice(password: str, enc_key: str) -> str:
    # Encrypts broker passwords using AliceBlue's required encryption.
    if not enc_key or not password:
        return password
    
    salt = os.urandom(8)
    key, iv = _get_cryptojs_key_iv(enc_key, salt)
    
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded_data = pad(password.encode('utf-8'), AES.block_size)
    encrypted_bytes = cipher.encrypt(padded_data)
    
    final_payload = b"Salted__" + salt + encrypted_bytes
    return base64.b64encode(final_payload).decode('utf-8')

def decrypt_password_alice(encrypted: str, enc_key: str) -> str:
    # Decrypts broker passwords previously encrypted for AliceBlue.
    if not enc_key or not encrypted:
        return encrypted
        
    try:
        data = base64.b64decode(encrypted)
        if not data.startswith(b"Salted__"):
            return encrypted  # Not encrypted properly
            
        salt = data[8:16]
        encrypted_bytes = data[16:]
        
        key, iv = _get_cryptojs_key_iv(enc_key, salt)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        
        decrypted_padded = cipher.decrypt(encrypted_bytes)
        decrypted = unpad(decrypted_padded, AES.block_size)
        return decrypted.decode('utf-8')
    except Exception:
        return encrypted  # Return as-is on failure
