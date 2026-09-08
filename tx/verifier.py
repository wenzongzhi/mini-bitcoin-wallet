"""Local verification for supported wallet transaction inputs."""

from coincurve import PublicKey

from btc.btc_address_gen import hash160

from .errors import TransactionError
from .model import Prevout, Transaction
from .script import parse_pushes
from .sighash import SIGHASH_ALL, bip143_sighash_all, legacy_sighash_all


def verify_transaction_input(
    tx: Transaction,
    input_index: int,
    prevout: Prevout,
) -> bool:
    if not 0 <= input_index < len(tx.inputs):
        return False
    tx_input = tx.inputs[input_index]
    try:
        if prevout.address_type == "p2wpkh":
            if tx_input.script_sig or len(tx_input.witness) != 2:
                return False
            signature_with_type, public_key = tx_input.witness
            if (
                len(public_key) != 33
                or public_key[0] not in (2, 3)
                or len(signature_with_type) < 2
                or signature_with_type[-1] != SIGHASH_ALL
                or hash160(public_key) != prevout.script_pubkey[2:]
            ):
                return False
            digest = bip143_sighash_all(tx, input_index, prevout)
        elif prevout.address_type == "p2pkh":
            if tx_input.witness:
                return False
            items = parse_pushes(tx_input.script_sig)
            if len(items) != 2:
                return False
            signature_with_type, public_key = items
            if (
                len(public_key) != 33
                or public_key[0] not in (2, 3)
                or len(signature_with_type) < 2
                or signature_with_type[-1] != SIGHASH_ALL
                or hash160(public_key) != prevout.script_pubkey[3:23]
            ):
                return False
            digest = legacy_sighash_all(tx, input_index, prevout)
        else:
            return False
        return PublicKey(public_key).verify(
            signature_with_type[:-1],
            digest,
            hasher=None,
        )
    except (TransactionError, ValueError):
        return False


def verify_all_inputs(tx: Transaction, prevouts: list[Prevout]) -> list[dict]:
    if len(prevouts) != len(tx.inputs):
        raise TransactionError("prevout count does not match input count")
    results = []
    for index, prevout in enumerate(prevouts):
        valid = verify_transaction_input(tx, index, prevout)
        results.append(
            {
                "index": index,
                "outpoint": prevout.outpoint,
                "address_type": prevout.address_type,
                "valid": valid,
            }
        )
    return results
