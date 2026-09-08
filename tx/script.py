"""Bitcoin address validation and standard script helpers."""

import hashlib
import struct

from bech32 import bech32_decode, bech32_encode, convertbits

from btc.btc_address_gen import BASE58_ALPHABET, base58check
from btc.chainparams import CHAIN_PARAMS, get_chain_params

from .errors import TransactionError


OP_DUP = 0x76
OP_HASH160 = 0xA9
OP_EQUALVERIFY = 0x88
OP_CHECKSIG = 0xAC


def _base58check_decode(address: str) -> tuple[bytes, bytes]:
    if not isinstance(address, str) or not address:
        raise TransactionError("address is required")
    number = 0
    try:
        for character in address:
            number = number * 58 + BASE58_ALPHABET.index(character)
    except ValueError as exc:
        raise TransactionError("invalid Base58 address character") from exc
    encoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(address) - len(address.lstrip("1"))
    decoded = b"\x00" * leading_zeros + encoded
    if len(decoded) != 25:
        raise TransactionError("invalid Base58Check address length")
    payload, checksum = decoded[:-4], decoded[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != expected:
        raise TransactionError("invalid Base58Check checksum")
    return payload[:1], payload[1:]


def address_to_script_pubkey(address: str, network: str) -> bytes:
    try:
        params = get_chain_params(network)
    except ValueError as exc:
        raise TransactionError(str(exc)) from exc

    if not isinstance(address, str) or not address:
        raise TransactionError("address is required")
    if address.lower().startswith(("bc1", "tb1", "bcrt1")):
        hrp, data = bech32_decode(address)
        if hrp is None or data is None:
            raise TransactionError("invalid Bech32 address or checksum")
        if hrp != params.bech32_hrp:
            raise TransactionError(f'address does not belong to network "{network}"')
        if not data or data[0] != 0:
            raise TransactionError("only witness version 0 P2WPKH addresses are supported")
        program_values = convertbits(data[1:], 5, 8, False)
        if program_values is None or len(program_values) != 20:
            raise TransactionError("only 20-byte P2WPKH witness programs are supported")
        return b"\x00\x14" + bytes(program_values)

    version, payload = _base58check_decode(address)
    if version != params.p2pkh_version:
        known_p2pkh_versions = {item.p2pkh_version for item in CHAIN_PARAMS.values()}
        if version in known_p2pkh_versions:
            raise TransactionError(f'address does not belong to network "{network}"')
        raise TransactionError("only P2PKH and P2WPKH destination addresses are supported")
    if len(payload) != 20:
        raise TransactionError("invalid P2PKH payload length")
    return b"\x76\xa9\x14" + payload + b"\x88\xac"


def classify_script_pubkey(script_pubkey: bytes) -> str | None:
    if (
        len(script_pubkey) == 25
        and script_pubkey[:3] == b"\x76\xa9\x14"
        and script_pubkey[-2:] == b"\x88\xac"
    ):
        return "p2pkh"
    if len(script_pubkey) == 22 and script_pubkey[:2] == b"\x00\x14":
        return "p2wpkh"
    return None


def script_pubkey_to_address(script_pubkey: bytes, network: str) -> str | None:
    try:
        params = get_chain_params(network)
    except ValueError as exc:
        raise TransactionError(str(exc)) from exc
    address_type = classify_script_pubkey(script_pubkey)
    if address_type == "p2pkh":
        return base58check(params.p2pkh_version, script_pubkey[3:23])
    if address_type == "p2wpkh":
        data = [0] + list(convertbits(script_pubkey[2:], 8, 5, True))
        return bech32_encode(params.bech32_hrp, data)
    return None


def p2pkh_script_code(pubkey_hash: bytes) -> bytes:
    if not isinstance(pubkey_hash, bytes) or len(pubkey_hash) != 20:
        raise TransactionError("public key hash must be 20 bytes")
    return b"\x76\xa9\x14" + pubkey_hash + b"\x88\xac"


def push_data(data: bytes) -> bytes:
    if not isinstance(data, bytes):
        raise TransactionError("pushed script data must be bytes")
    length = len(data)
    if length <= 75:
        return bytes([length]) + data
    if length <= 0xFF:
        return b"\x4c" + bytes([length]) + data
    if length <= 0xFFFF:
        return b"\x4d" + struct.pack("<H", length) + data
    if length <= 0xFFFFFFFF:
        return b"\x4e" + struct.pack("<I", length) + data
    raise TransactionError("script push data is too large")


def parse_pushes(script: bytes) -> list[bytes]:
    if not isinstance(script, bytes):
        raise TransactionError("script must be bytes")
    offset = 0
    items = []
    while offset < len(script):
        opcode = script[offset]
        offset += 1
        if opcode <= 75:
            length = opcode
        elif opcode == 0x4C:
            if offset + 1 > len(script):
                raise TransactionError("truncated OP_PUSHDATA1")
            length = script[offset]
            offset += 1
        elif opcode == 0x4D:
            if offset + 2 > len(script):
                raise TransactionError("truncated OP_PUSHDATA2")
            length = struct.unpack("<H", script[offset : offset + 2])[0]
            offset += 2
        elif opcode == 0x4E:
            if offset + 4 > len(script):
                raise TransactionError("truncated OP_PUSHDATA4")
            length = struct.unpack("<I", script[offset : offset + 4])[0]
            offset += 4
        else:
            raise TransactionError("script contains a non-push opcode")
        if offset + length > len(script):
            raise TransactionError("truncated pushed script data")
        items.append(script[offset : offset + length])
        offset += length
    return items
