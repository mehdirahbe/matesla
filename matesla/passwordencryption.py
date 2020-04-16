#To encrypt Pw, FROM https://stackoverflow.com/questions/7014953/i-need-to-securely-store-a-username-and-password-in-python-what-are-my-options
from getpass import getpass
from pbkdf2 import PBKDF2
from Crypto.Cipher import AES
import os

### Settings ###
from mysite.settings import SECRET_KEY, saltSeed

passphrase=SECRET_KEY
PASSPHRASE_SIZE = len(SECRET_KEY) # 512-bit passphrase
KEY_SIZE = 32 # 256-bit key
BLOCK_SIZE = 16  # 16-bit blocks
IV_SIZE = 16 # 128-bits to initialise
SALT_SIZE = 16 # 128-bits of salt

### System Functions ###


def getSaltForKey(key):
    return PBKDF2(key, saltSeed).read(SALT_SIZE) # Salt is generated as the hash of the key with it's own salt acting like a seed value


def encrypt(plaintext, salt):
    #Pad plaintext, then encrypt it with a new, randomly initialised cipher.
    # Initialise Cipher Randomly
    initVector = os.urandom(IV_SIZE)
    # Prepare cipher key:
    key = PBKDF2(passphrase, salt).read(KEY_SIZE)
    cipher = AES.new(key, AES.MODE_CBC, initVector) # Create cipher
    utf8plaintext=plaintext.encode('utf8')
    resultBytes=initVector + cipher.encrypt(plaintext + '\0'*(BLOCK_SIZE - (len(utf8plaintext) % BLOCK_SIZE))) # Pad and encrypt
    return resultBytes.hex()


def decrypt(ciphertextStr, salt):
    ciphertext=bytes.fromhex(ciphertextStr)
    #Reconstruct the cipher object and decrypt.
    # Prepare cipher key:
    key = PBKDF2(passphrase, salt).read(KEY_SIZE)
    # Extract IV:
    initVector = ciphertext[:IV_SIZE]
    ciphertext = ciphertext[IV_SIZE:]
    cipher = AES.new(key, AES.MODE_CBC, initVector) # Reconstruct cipher (IV isn't needed for decryption so is set to zeros)
    return cipher.decrypt(ciphertext).decode('utf8').strip('\0') # Decrypt and depad

'''salt=getSaltForKey("brol")
crypted=encrypt("pw",salt)
back=decrypt(crypted,salt)
print(type(crypted.hex()))
print(type(back))'''
