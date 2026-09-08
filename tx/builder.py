"""Unsigned transaction construction helpers."""

from .codec import transaction_metrics
from .errors import TransactionError
from .model import Transaction, TxInput, TxOutput
from .script import (
    address_to_script_pubkey,
    classify_script_pubkey,
    script_pubkey_to_address,
)


def parse_outpoint(value: str) -> tuple[str, int]:
    if not isinstance(value, str) or ":" not in value:
        raise TransactionError('input must use "txid:vout" format')
    txid, vout_text = value.rsplit(":", 1)
    try:
        vout = int(vout_text, 10)
    except ValueError as exc:
        raise TransactionError("input vout must be an integer") from exc
    tx_input = TxInput(txid, vout)
    return tx_input.txid, tx_input.vout


def parse_output_spec(value: str) -> tuple[str, int]:
    if not isinstance(value, str) or ":" not in value:
        raise TransactionError('output must use "address:amount_sats" format')
    address, amount_text = value.rsplit(":", 1)
    try:
        amount = int(amount_text, 10)
    except ValueError as exc:
        raise TransactionError("output amount must be integer satoshis") from exc
    if amount <= 0:
        raise TransactionError("output amount must be greater than zero")
    return address, amount


def create_raw_transaction(
    inputs: list[dict],
    outputs: list[dict],
    network: str,
    *,
    locktime: int = 0,
    version: int = 2,
) -> Transaction:
    if not isinstance(inputs, list) or not isinstance(outputs, list):
        raise TransactionError("inputs and outputs must be lists")
    tx_inputs = []
    for item in inputs:
        if not isinstance(item, dict):
            raise TransactionError("each transaction input must be an object")
        try:
            tx_inputs.append(
                TxInput(item["txid"], item["vout"], item.get("sequence", 0xFFFFFFFD))
            )
        except KeyError as exc:
            raise TransactionError("transaction input requires txid and vout") from exc
    tx_outputs = []
    for item in outputs:
        if not isinstance(item, dict):
            raise TransactionError("each transaction output must be an object")
        address = item.get("address")
        amount = item.get("amount_sats")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise TransactionError("output amount_sats must be a positive integer")
        tx_outputs.append(TxOutput(amount, address_to_script_pubkey(address, network)))
    if not tx_outputs:
        raise TransactionError("at least one output is required")
    return Transaction(version, tx_inputs, tx_outputs, locktime)


def transaction_output_metadata(tx: Transaction, network: str) -> list[dict]:
    return [
        {
            "index": index,
            "value": output.value,
            "script_pubkey": output.script_pubkey.hex(),
            "address_type": classify_script_pubkey(output.script_pubkey),
            "address": script_pubkey_to_address(output.script_pubkey, network),
        }
        for index, output in enumerate(tx.outputs)
    ]


def decode_transaction(tx: Transaction, network: str) -> dict:
    metrics = transaction_metrics(tx)
    return {
        **metrics,
        "version": tx.version,
        "locktime": tx.locktime,
        "input_count": len(tx.inputs),
        "inputs": [
            {
                "index": index,
                "txid": item.txid,
                "vout": item.vout,
                "script_sig": item.script_sig.hex(),
                "sequence": item.sequence,
                "witness": [entry.hex() for entry in item.witness],
            }
            for index, item in enumerate(tx.inputs)
        ],
        "output_count": len(tx.outputs),
        "outputs": transaction_output_metadata(tx, network),
    }
