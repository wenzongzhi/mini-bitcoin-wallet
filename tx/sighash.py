"""Legacy and BIP143 SIGHASH_ALL digest construction."""

import struct

from .codec import (
    double_sha256,
    encode_compact_size,
    serialize_outpoint,
    serialize_transaction,
    serialize_tx_output,
)
from .errors import TransactionError
from .model import Prevout, Transaction, TxInput
from .script import classify_script_pubkey, p2pkh_script_code


SIGHASH_ALL = 1


def bip143_sighash_all(tx: Transaction, input_index: int, prevout: Prevout) -> bytes:
    if not 0 <= input_index < len(tx.inputs):
        raise TransactionError("input index is out of range")
    if (
        prevout.address_type != "p2wpkh"
        or classify_script_pubkey(prevout.script_pubkey) != "p2wpkh"
    ):
        raise TransactionError("BIP143 signing requires a P2WPKH prevout")
    current_input = tx.inputs[input_index]
    if current_input.txid != prevout.txid or current_input.vout != prevout.vout:
        raise TransactionError("prevout does not match transaction input")

    hash_prevouts = double_sha256(b"".join(serialize_outpoint(item) for item in tx.inputs))
    hash_sequence = double_sha256(
        b"".join(struct.pack("<I", item.sequence) for item in tx.inputs)
    )
    hash_outputs = double_sha256(b"".join(serialize_tx_output(item) for item in tx.outputs))
    script_code = p2pkh_script_code(prevout.script_pubkey[2:])
    preimage = (
        struct.pack("<i", tx.version)
        + hash_prevouts
        + hash_sequence
        + serialize_outpoint(current_input)
        + encode_compact_size(len(script_code))
        + script_code
        + struct.pack("<Q", prevout.value)
        + struct.pack("<I", current_input.sequence)
        + hash_outputs
        + struct.pack("<I", tx.locktime)
        + struct.pack("<I", SIGHASH_ALL)
    )
    return double_sha256(preimage)


def legacy_sighash_all(tx: Transaction, input_index: int, prevout: Prevout) -> bytes:
    if not 0 <= input_index < len(tx.inputs):
        raise TransactionError("input index is out of range")
    if (
        prevout.address_type != "p2pkh"
        or classify_script_pubkey(prevout.script_pubkey) != "p2pkh"
    ):
        raise TransactionError("legacy signing requires a P2PKH prevout")
    current_input = tx.inputs[input_index]
    if current_input.txid != prevout.txid or current_input.vout != prevout.vout:
        raise TransactionError("prevout does not match transaction input")
    signing_inputs = [
        TxInput(
            item.txid,
            item.vout,
            item.sequence,
            prevout.script_pubkey if index == input_index else b"",
        )
        for index, item in enumerate(tx.inputs)
    ]
    signing_tx = Transaction(tx.version, signing_inputs, list(tx.outputs), tx.locktime)
    return double_sha256(
        serialize_transaction(signing_tx, include_witness=False)
        + struct.pack("<I", SIGHASH_ALL)
    )
