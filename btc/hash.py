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

import hashlib
from pathlib import Path

def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()

def dbl_sha256(b: bytes) -> bytes:
    return sha256(sha256(b))

def hash160(b: bytes) -> bytes:
    return hashlib.new("ripemd160", sha256(b)).digest()

def sha256_file(path: str) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.digest()

def dbl_sha256_file(path) -> bytes:
    first = sha256_file(path)
    return hashlib.sha256(first).digest()
    
def show(label: str, b: bytes):
    print(f"{label}:")
    print("  len =", len(b))
    print("  hex =", b.hex())
    print()

