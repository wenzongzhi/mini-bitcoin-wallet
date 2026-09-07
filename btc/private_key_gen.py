"""
Copyright 2026 温中志 (Wen Zhongzhi)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import secrets
from ecdsa import SigningKey, SECP256k1
SECP256K1_N = int("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16)

def is_valid_privkey(privkey32: bytes) -> bool:
    if len(privkey32) != 32:
        return False
    k = int.from_bytes(privkey32, "big")
    return 1 <= k < SECP256K1_N

#Generate a 32-byte (256-bit) cryptographically secure unsigned random number for use as a private key
#return: original byte object, hexadecimal string
def generate_32bytes_private_key():
    while True:
        private_key_bytes = secrets.token_bytes(32) # Generate 32-byte encrypted secure random bytes (unsigned, with each byte taking a value from 0 to 255
        if is_valid_privkey(private_key_bytes):            
            private_key_hex = private_key_bytes.hex().upper() # Convert to hexadecimal string (uppercase, more in line with industry conventions; lowercase can be changed to hex() without calling upper())
            return private_key_bytes, private_key_hex
