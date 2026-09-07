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
from ecdsa import MalformedPointError, SigningKey, SECP256k1, VerifyingKey
from ecdsa.ellipticcurve import INFINITY
from bech32 import CHARSET, bech32_encode, bech32_hrp_expand, bech32_polymod, convertbits

from .chainparams import NETWORK_MAINNET, get_chain_params

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BECH32M_CONST = 0x2BC830A3

def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()

def ripemd160(b: bytes) -> bytes:
    h = hashlib.new("ripemd160")
    h.update(b)
    return h.digest()

def hash160(b: bytes) -> bytes:
    return ripemd160(sha256(b))

def base58_encode(b: bytes) -> str:
    num = int.from_bytes(b, "big")
    res = ""
    while num > 0:
        num, rem = divmod(num, 58)
        res = BASE58_ALPHABET[rem] + res
    pad = 0
    for c in b:
        if c == 0:
            pad += 1
        else:
            break
    return "1" * pad + res

def base58check(version: bytes, payload: bytes) -> str:
    data = version + payload
    checksum = sha256(sha256(data))[:4]
    return base58_encode(data + checksum)

def privkey_to_pubkey(privkey32: bytes, compressed: bool = True) -> bytes:
    sk = SigningKey.from_string(privkey32, curve=SECP256k1)
    vk = sk.verifying_key
    x = vk.pubkey.point.x()
    y = vk.pubkey.point.y()
    x_bytes = x.to_bytes(32, "big")
    if not compressed:
        return b"\x04" + x_bytes + y.to_bytes(32, "big")
    prefix = b"\x02" if (y % 2 == 0) else b"\x03"
    return prefix + x_bytes


def privkey_to_compressed_pubkey(privkey32: bytes) -> bytes:
    return privkey_to_pubkey(privkey32, compressed=True)


def is_valid_public_key(pubkey: bytes) -> bool:
    if len(pubkey) == 33:
        valid_encodings = {"compressed"}
    elif len(pubkey) == 65:
        valid_encodings = {"uncompressed"}
    else:
        return False

    try:
        VerifyingKey.from_string(
            pubkey,
            curve=SECP256k1,
            validate_point=True,
            valid_encodings=valid_encodings,
        )
    except (MalformedPointError, ValueError):
        return False
    return True


def public_key_to_compressed(pubkey: bytes) -> bytes:
    try:
        vk = VerifyingKey.from_string(
            pubkey,
            curve=SECP256k1,
            validate_point=True,
            valid_encodings={"compressed", "uncompressed"},
        )
    except (MalformedPointError, ValueError) as exc:
        raise ValueError("invalid secp256k1 public key") from exc

    point = vk.pubkey.point
    prefix = b"\x02" if point.y() % 2 == 0 else b"\x03"
    return prefix + point.x().to_bytes(32, "big")


def tagged_hash(tag: str, data: bytes) -> bytes:
    tag_hash = sha256(tag.encode("ascii"))
    return sha256(tag_hash + tag_hash + data)


def bech32m_encode(hrp: str, data: list[int]) -> str:
    polymod = bech32_polymod(bech32_hrp_expand(hrp) + data + [0] * 6)
    checksum_value = polymod ^ BECH32M_CONST
    checksum = [(checksum_value >> (5 * (5 - i))) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[value] for value in data + checksum)


def p2wpkh_bech32_address(
    pubkey_compressed: bytes,
    network: str = NETWORK_MAINNET,
) -> str:
    h160 = hash160(pubkey_compressed)  # 20 bytes
    # witness version 0, program=20 bytes
    data = [0] + list(convertbits(h160, 8, 5, True))
    return bech32_encode(get_chain_params(network).bech32_hrp, data)


def p2wpkh_script_pubkey(pubkey_compressed: bytes) -> str:
    return (b"\x00\x14" + hash160(pubkey_compressed)).hex()


def p2pkh_script_pubkey(pubkey: bytes) -> str:
    return (b"\x76\xa9\x14" + hash160(pubkey) + b"\x88\xac").hex()


def p2sh_p2wpkh_script_pubkey(pubkey_compressed: bytes) -> str:
    redeem_script = b"\x00\x14" + hash160(pubkey_compressed)
    return (b"\xa9\x14" + hash160(redeem_script) + b"\x87").hex()


def p2tr_output_key(pubkey_compressed: bytes) -> bytes:
    if len(pubkey_compressed) != 33 or not is_valid_public_key(pubkey_compressed):
        raise ValueError("P2TR requires a compressed secp256k1 public key")

    internal_key = pubkey_compressed[1:]
    even_pubkey = b"\x02" + internal_key
    internal_point = VerifyingKey.from_string(
        even_pubkey,
        curve=SECP256k1,
        validate_point=True,
        valid_encodings={"compressed"},
    ).pubkey.point

    tweak = int.from_bytes(tagged_hash("TapTweak", internal_key), "big")
    if tweak >= SECP256k1.order:
        raise ValueError("invalid Taproot tweak")

    output_point = internal_point + tweak * SECP256k1.generator
    if output_point == INFINITY:
        raise ValueError("invalid Taproot output point")
    return output_point.x().to_bytes(32, "big")


def p2tr_address(
    pubkey_compressed: bytes,
    network: str = NETWORK_MAINNET,
) -> str:
    output_key = p2tr_output_key(pubkey_compressed)
    data = [1] + list(convertbits(output_key, 8, 5, True))
    return bech32m_encode(get_chain_params(network).bech32_hrp, data)


def p2tr_script_pubkey(pubkey_compressed: bytes) -> str:
    return (b"\x51\x20" + p2tr_output_key(pubkey_compressed)).hex()


def p2sh_p2wpkh_address(
    pubkey_compressed: bytes,
    network: str = NETWORK_MAINNET,
) -> str:
    h160 = hash160(pubkey_compressed)
    redeem_script = b"\x00\x14" + h160  # 0 <20-byte>
    script_hash = hash160(redeem_script)
    return base58check(get_chain_params(network).p2sh_version, script_hash)

def pubkey_to_p2pkh(pubkey: bytes, network: str = NETWORK_MAINNET) -> str:
    h160 = hash160(pubkey)
    payload = get_chain_params(network).p2pkh_version + h160
    checksum = sha256(sha256(payload))[:4]
    return base58_encode(payload + checksum)
