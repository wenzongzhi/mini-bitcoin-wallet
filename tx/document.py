"""Versioned funded and signed transaction document handling."""

import json
import os
import re
import tempfile
from pathlib import Path

from .builder import transaction_output_metadata
from .codec import (
    deserialize_transaction_hex,
    serialize_transaction_hex,
    transaction_metrics,
)
from .errors import TransactionError
from .model import Prevout, Transaction
from .script import address_to_script_pubkey, classify_script_pubkey


FUNDED_FORMAT = "bitcoin-tool-funded-transaction"
SIGNED_FORMAT = "bitcoin-tool-signed-transaction"
DOCUMENT_VERSION = 1
MAX_DOCUMENT_SIZE = 10 * 1024 * 1024


def load_json_document(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_DOCUMENT_SIZE:
            raise TransactionError("transaction document is too large")
        with path.open("r", encoding="utf-8") as file:
            document = json.load(file)
    except TransactionError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError(f'cannot read transaction document "{path}": {exc}') from exc
    if not isinstance(document, dict):
        raise TransactionError("transaction document must be a JSON object")
    return document


def save_json_document(document: dict, path: Path) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            json.dump(document, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise TransactionError(f'cannot write transaction document "{path}": {exc}') from exc


def _prevout_from_metadata(metadata: dict) -> Prevout:
    if not isinstance(metadata, dict):
        raise TransactionError("funded transaction input metadata is invalid")
    try:
        return Prevout(
            txid=metadata["txid"],
            vout=metadata["vout"],
            value=metadata["value"],
            script_pubkey=bytes.fromhex(metadata["script_pubkey"]),
            address=metadata["address"],
            address_type=str(metadata["address_type"]).lower(),
            derivation_path=metadata["derivation_path"],
            account_id=metadata.get("account_id"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TransactionError("funded transaction input metadata is invalid") from exc


def validate_funded_document(
    document: dict,
    network: str,
    wallet_name: str | None = None,
) -> tuple[Transaction, list[Prevout]]:
    if not isinstance(document, dict):
        raise TransactionError("funded transaction document must be an object")
    if document.get("format") != FUNDED_FORMAT or document.get("version") != DOCUMENT_VERSION:
        raise TransactionError("unsupported funded transaction document format")
    if document.get("network") != network:
        raise TransactionError("funded transaction network does not match CLI network")
    if wallet_name is not None and document.get("wallet_name") != wallet_name:
        raise TransactionError("funded transaction wallet name does not match")
    if not re.fullmatch(r"[0-9a-f]{32}", str(document.get("draft_id", ""))):
        raise TransactionError("funded transaction draft id is invalid")
    tx = deserialize_transaction_hex(document.get("unsigned_tx_hex"))
    inputs = document.get("inputs")
    outputs = document.get("outputs")
    if not isinstance(inputs, list) or not isinstance(outputs, list):
        raise TransactionError("funded transaction metadata is incomplete")
    prevouts = [_prevout_from_metadata(item) for item in inputs]
    if len(prevouts) != len(tx.inputs):
        raise TransactionError("funded transaction input count mismatch")
    for tx_input, prevout in zip(tx.inputs, prevouts):
        if tx_input.txid != prevout.txid or tx_input.vout != prevout.vout:
            raise TransactionError("funded transaction input order mismatch")
        if tx_input.script_sig or tx_input.witness:
            raise TransactionError("funded transaction must be unsigned")
        if (
            classify_script_pubkey(prevout.script_pubkey) != prevout.address_type
            or address_to_script_pubkey(prevout.address, network) != prevout.script_pubkey
        ):
            raise TransactionError("funded transaction prevout address and script do not match")
    expected_outputs = transaction_output_metadata(tx, network)
    if len(outputs) != len(expected_outputs):
        raise TransactionError("funded transaction output count mismatch")
    for expected, metadata in zip(expected_outputs, outputs):
        if not isinstance(metadata, dict):
            raise TransactionError("funded transaction output metadata is invalid")
        if (
            metadata.get("index") != expected["index"]
            or metadata.get("value") != expected["value"]
            or metadata.get("script_pubkey") != expected["script_pubkey"]
            or metadata.get("address") != expected["address"]
            or not isinstance(metadata.get("is_change"), bool)
        ):
            raise TransactionError("funded transaction output metadata does not match raw transaction")
    change_position = document.get("change_position")
    change_indexes = [item["index"] for item in outputs if item.get("is_change")]
    if change_position is None:
        if change_indexes:
            raise TransactionError("funded transaction change metadata is inconsistent")
    elif (
        isinstance(change_position, bool)
        or not isinstance(change_position, int)
        or change_indexes != [change_position]
    ):
        raise TransactionError("funded transaction change position is invalid")
    total_input = sum(prevout.value for prevout in prevouts)
    total_output = sum(output.value for output in tx.outputs)
    destination_total = sum(
        item["value"] for item in outputs if item.get("is_change") is False
    )
    if (
        document.get("total_input_sats") != total_input
        or document.get("destination_total_sats") != destination_total
        or document.get("estimated_fee_sats") != total_input - total_output
        or total_input < total_output
    ):
        raise TransactionError("funded transaction amount metadata is inconsistent")
    if serialize_transaction_hex(tx, include_witness=False) != document["unsigned_tx_hex"].lower():
        raise TransactionError("funded transaction serialization is not canonical")
    return tx, prevouts


def validate_signed_document(
    document: dict,
    network: str,
) -> tuple[Transaction, list[Prevout]]:
    if not isinstance(document, dict):
        raise TransactionError("signed transaction document must be an object")
    if document.get("format") != SIGNED_FORMAT or document.get("version") != DOCUMENT_VERSION:
        raise TransactionError("unsupported signed transaction document format")
    if document.get("network") != network:
        raise TransactionError("signed transaction network does not match CLI network")
    if not isinstance(document.get("wallet_name"), str) or not document["wallet_name"]:
        raise TransactionError("signed transaction wallet name is invalid")
    if not re.fullmatch(r"[0-9a-f]{32}", str(document.get("draft_id", ""))):
        raise TransactionError("signed transaction draft id is invalid")
    if document.get("complete") is not True:
        raise TransactionError("signed transaction document is incomplete")
    raw_hex = document.get("hex")
    tx = deserialize_transaction_hex(raw_hex)
    prevouts = [_prevout_from_metadata(item) for item in document.get("inputs", [])]
    if len(prevouts) != len(tx.inputs):
        raise TransactionError("signed transaction prevout count mismatch")
    for tx_input, prevout in zip(tx.inputs, prevouts):
        if tx_input.txid != prevout.txid or tx_input.vout != prevout.vout:
            raise TransactionError("signed transaction input order mismatch")
        if (
            classify_script_pubkey(prevout.script_pubkey) != prevout.address_type
            or address_to_script_pubkey(prevout.address, network) != prevout.script_pubkey
        ):
            raise TransactionError("signed transaction prevout address and script do not match")
    outputs = document.get("outputs")
    expected_outputs = transaction_output_metadata(tx, network)
    if not isinstance(outputs, list) or len(outputs) != len(expected_outputs):
        raise TransactionError("signed transaction output metadata is incomplete")
    for expected, metadata in zip(expected_outputs, outputs):
        if (
            not isinstance(metadata, dict)
            or not isinstance(metadata.get("is_change"), bool)
            or any(
                metadata.get(field) != expected[field]
                for field in ("index", "value", "script_pubkey", "address")
            )
        ):
            raise TransactionError(
                "signed transaction output metadata does not match raw transaction"
            )
    change_position = document.get("change_position")
    change_indexes = [item["index"] for item in outputs if item.get("is_change") is True]
    if change_position is None:
        if change_indexes:
            raise TransactionError("signed transaction change metadata is inconsistent")
    elif (
        isinstance(change_position, bool)
        or not isinstance(change_position, int)
        or change_indexes != [change_position]
    ):
        raise TransactionError("signed transaction change position is invalid")
    fee = sum(prevout.value for prevout in prevouts) - sum(output.value for output in tx.outputs)
    if fee < 0 or document.get("fee_sats") != fee:
        raise TransactionError("signed transaction fee metadata is inconsistent")
    metrics = transaction_metrics(tx)
    for field in ("txid", "wtxid", "size", "stripped_size", "weight", "vsize"):
        if document.get(field) != metrics[field]:
            raise TransactionError(f'signed transaction field "{field}" is inconsistent')
    if serialize_transaction_hex(tx) != raw_hex.lower():
        raise TransactionError("signed transaction serialization is not canonical")
    return tx, prevouts
