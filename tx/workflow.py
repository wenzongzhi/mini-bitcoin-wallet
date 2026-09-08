"""Wallet funding, signing, reservation, and broadcast state workflows."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import uuid

from wallet import (
    WalletError,
    WalletSigningSession,
    get_new_address,
    get_wallet_address_book,
)
from wallet.wallet_cache import (
    load_wallet_cache,
    locked_cache_file,
    save_wallet_cache,
    utc_now,
)

from .builder import transaction_output_metadata
from .codec import (
    deserialize_transaction_hex,
    serialize_transaction_hex,
    transaction_metrics,
)
from .coin_selection import (
    DUST_THRESHOLDS,
    fee_for_vsize,
    parse_fee_rate,
    select_all_coins,
    select_coins,
)
from .document import (
    DOCUMENT_VERSION,
    FUNDED_FORMAT,
    SIGNED_FORMAT,
    validate_funded_document,
    validate_signed_document,
)
from .errors import TransactionError
from .model import Prevout, Transaction, TxInput, TxOutput
from .script import address_to_script_pubkey, classify_script_pubkey
from .signer import sign_transaction
from .verifier import verify_all_inputs


RESERVATION_TTL_SECONDS = 3600


def _parse_timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise TransactionError(f"wallet cache {field} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TransactionError(f"wallet cache {field} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise TransactionError(f"wallet cache {field} timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _cleanup_reservations(wallet_cache: dict, now: datetime) -> dict:
    reservations = wallet_cache.setdefault("reserved_outpoints", {})
    if not isinstance(reservations, dict):
        raise TransactionError("wallet cache reservations are invalid")
    cutoff = now - timedelta(seconds=RESERVATION_TTL_SECONDS)
    return {
        outpoint: reservation
        for outpoint, reservation in reservations.items()
        if isinstance(reservation, dict)
        and _parse_timestamp(reservation.get("reserved_at"), "reservation") >= cutoff
    }


def _release_draft(cache_file: Path, wallet_name: str, draft_id: str) -> None:
    with locked_cache_file(cache_file):
        cache = load_wallet_cache(cache_file)
        wallet_cache = cache.get("wallets", {}).get(wallet_name)
        if not isinstance(wallet_cache, dict):
            return
        reservations = wallet_cache.get("reserved_outpoints", {})
        if isinstance(reservations, dict):
            wallet_cache["reserved_outpoints"] = {
                outpoint: item
                for outpoint, item in reservations.items()
                if not isinstance(item, dict) or item.get("draft_id") != draft_id
            }
            save_wallet_cache(cache, cache_file)


def fund_transaction(
    tx_template: Transaction,
    wallet_name: str,
    wallet_file: Path,
    cache_file: Path,
    network: str,
    address_type: str,
    fee_rate_value: str | int | Decimal,
    *,
    min_confirmations: int = 1,
    include_outpoints: list[str] | None = None,
    exclude_outpoints: set[str] | None = None,
    max_fee_sats: int | None = None,
    max_cache_age_seconds: int = 300,
    utxo_source: str = "local cache",
) -> dict:
    if not tx_template.outputs:
        raise TransactionError("transaction template has no outputs")
    if any(classify_script_pubkey(output.script_pubkey) is None for output in tx_template.outputs):
        raise TransactionError("transaction contains an unsupported destination output")
    if max_cache_age_seconds < 0:
        raise TransactionError("max cache age must not be negative")
    if max_fee_sats is not None and max_fee_sats < 0:
        raise TransactionError("max fee must not be negative")
    address_type = address_type.lower()
    if address_type not in {"p2pkh", "p2wpkh"}:
        raise TransactionError("funding supports only p2pkh and p2wpkh")
    fee_rate = parse_fee_rate(fee_rate_value)
    draft_id = uuid.uuid4().hex
    mandatory = [item.outpoint for item in tx_template.inputs]
    requested_includes = list(dict.fromkeys([*mandatory, *(include_outpoints or [])]))
    now = datetime.now(timezone.utc)

    with locked_cache_file(cache_file):
        cache = load_wallet_cache(cache_file)
        wallet_cache = cache.get("wallets", {}).get(wallet_name)
        if not isinstance(wallet_cache, dict):
            raise TransactionError(f'wallet "{wallet_name}" has no synced UTXO cache')
        synced_at = _parse_timestamp(wallet_cache.get("synced_at"), "sync")
        cache_age = max(int((now - synced_at).total_seconds()), 0)
        if cache_age > max_cache_age_seconds:
            raise TransactionError(
                f"wallet cache is stale ({cache_age} seconds old); run syncwallet or increase max cache age"
            )
        active_reservations = _cleanup_reservations(wallet_cache, now)
        pending_spent = wallet_cache.get("pending_spent_outpoints", {})
        if not isinstance(pending_spent, dict):
            raise TransactionError("wallet pending-spent outpoints are invalid")
        selection = select_coins(
            wallet_cache.get("utxos", []),
            tx_template.outputs,
            address_type,
            fee_rate,
            min_confirmations=min_confirmations,
            include_outpoints=requested_includes,
            exclude_outpoints=exclude_outpoints,
            reserved_outpoints=set(active_reservations) | set(pending_spent),
            max_fee_sats=max_fee_sats,
        )
        reserved_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for selected in selection["selected"]:
            active_reservations[selected["outpoint"]] = {
                "draft_id": draft_id,
                "reserved_at": reserved_at,
            }
        wallet_cache["reserved_outpoints"] = active_reservations
        save_wallet_cache(cache, cache_file)

    try:
        original_sequences = {item.outpoint: item.sequence for item in tx_template.inputs}
        funded_inputs = [
            TxInput(
                selected["txid"],
                selected["vout"],
                original_sequences.get(selected["outpoint"], 0xFFFFFFFD),
            )
            for selected in selection["selected"]
        ]
        funded_outputs = [deepcopy(output) for output in tx_template.outputs]
        change_position = None
        change_entry = None
        if selection["change_sats"]:
            try:
                change_result = get_new_address(
                    wallet_name,
                    wallet_file=wallet_file,
                    change=True,
                    address_type=address_type,
                    network=network,
                )
            except WalletError as exc:
                raise TransactionError(str(exc)) from exc
            address_book = get_wallet_address_book(
                wallet_name,
                wallet_file=wallet_file,
                address_type=address_type,
                network=network,
            )
            matches = [
                item
                for item in address_book["addresses"]
                if item["path"] == change_result["derivation_path"]
            ]
            if len(matches) != 1 or matches[0]["branch"] != 1:
                raise TransactionError("issued change address is not present in wallet")
            change_entry = matches[0]
            funded_outputs.append(
                TxOutput(selection["change_sats"], bytes.fromhex(change_entry["script_pubkey"]))
            )
            change_position = len(funded_outputs) - 1

        funded_tx = Transaction(
            tx_template.version,
            funded_inputs,
            funded_outputs,
            tx_template.locktime,
        )
        input_metadata = [
            {
                "txid": item["txid"],
                "vout": item["vout"],
                "value": item["value"],
                "script_pubkey": item["script_pubkey"],
                "address": item["address"],
                "address_type": item["address_type"].lower(),
                "derivation_path": item["path"],
                "branch": item["branch"],
                "index": item["index"],
                "account_id": item["account_id"],
            }
            for item in selection["selected"]
        ]
        output_metadata = transaction_output_metadata(funded_tx, network)
        for item in output_metadata:
            item["is_change"] = item["index"] == change_position
            if item["is_change"] and change_entry is not None:
                item["derivation_path"] = change_entry["path"]
                item["account_id"] = change_entry["account_id"]
        document = {
            "format": FUNDED_FORMAT,
            "version": DOCUMENT_VERSION,
            "network": network,
            "wallet_name": wallet_name,
            "draft_id": draft_id,
            "created_at": utc_now(),
            "utxo_source": utxo_source,
            "cache_file": str(cache_file),
            "unsigned_tx_hex": serialize_transaction_hex(funded_tx, include_witness=False),
            "inputs": input_metadata,
            "outputs": output_metadata,
            "total_input_sats": selection["total_input_sats"],
            "destination_total_sats": selection["destination_total_sats"],
            "estimated_fee_sats": selection["estimated_fee_sats"],
            "estimated_vsize": selection["estimated_vsize"],
            "requested_fee_rate_sat_vb": format(fee_rate, "f"),
            "max_fee_sats": max_fee_sats,
            "change_position": change_position,
        }
        validate_funded_document(document, network, wallet_name)
        return document
    except Exception:
        _release_draft(cache_file, wallet_name, draft_id)
        raise


def fund_all_transaction(
    destination_address: str,
    wallet_name: str,
    cache_file: Path,
    network: str,
    address_type: str,
    fee_rate_value: str | int | Decimal,
    *,
    min_confirmations: int = 1,
    exclude_outpoints: set[str] | None = None,
    max_fee_sats: int | None = None,
    max_cache_age_seconds: int = 300,
    utxo_source: str = "local cache",
) -> dict:
    """Fund a one-output transaction with every eligible wallet UTXO and no change."""
    if max_cache_age_seconds < 0:
        raise TransactionError("max cache age must not be negative")
    if max_fee_sats is not None and (
        isinstance(max_fee_sats, bool)
        or not isinstance(max_fee_sats, int)
        or max_fee_sats < 0
    ):
        raise TransactionError("max fee must be a non-negative integer")
    address_type = address_type.lower()
    if address_type not in {"p2pkh", "p2wpkh"}:
        raise TransactionError("funding supports only p2pkh and p2wpkh")
    destination_script = address_to_script_pubkey(destination_address, network)
    fee_rate = parse_fee_rate(fee_rate_value)
    draft_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)

    with locked_cache_file(cache_file):
        cache = load_wallet_cache(cache_file)
        wallet_cache = cache.get("wallets", {}).get(wallet_name)
        if not isinstance(wallet_cache, dict):
            raise TransactionError(f'wallet "{wallet_name}" has no synced UTXO cache')
        synced_at = _parse_timestamp(wallet_cache.get("synced_at"), "sync")
        cache_age = max(int((now - synced_at).total_seconds()), 0)
        if cache_age > max_cache_age_seconds:
            raise TransactionError(
                f"wallet cache is stale ({cache_age} seconds old); run syncwallet or increase max cache age"
            )
        active_reservations = _cleanup_reservations(wallet_cache, now)
        pending_spent = wallet_cache.get("pending_spent_outpoints", {})
        if not isinstance(pending_spent, dict):
            raise TransactionError("wallet pending-spent outpoints are invalid")
        selection = select_all_coins(
            wallet_cache.get("utxos", []),
            destination_script,
            address_type,
            fee_rate,
            min_confirmations=min_confirmations,
            exclude_outpoints=exclude_outpoints,
            reserved_outpoints=set(active_reservations) | set(pending_spent),
            max_fee_sats=max_fee_sats,
        )
        reserved_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for selected in selection["selected"]:
            active_reservations[selected["outpoint"]] = {
                "draft_id": draft_id,
                "reserved_at": reserved_at,
            }
        wallet_cache["reserved_outpoints"] = active_reservations
        save_wallet_cache(cache, cache_file)

    try:
        funded_tx = Transaction(
            2,
            [TxInput(item["txid"], item["vout"]) for item in selection["selected"]],
            [TxOutput(selection["destination_total_sats"], destination_script)],
            0,
        )
        input_metadata = [
            {
                "txid": item["txid"],
                "vout": item["vout"],
                "value": item["value"],
                "script_pubkey": item["script_pubkey"],
                "address": item["address"],
                "address_type": item["address_type"].lower(),
                "derivation_path": item["path"],
                "branch": item["branch"],
                "index": item["index"],
                "account_id": item["account_id"],
            }
            for item in selection["selected"]
        ]
        output_metadata = transaction_output_metadata(funded_tx, network)
        output_metadata[0]["is_change"] = False
        document = {
            "format": FUNDED_FORMAT,
            "version": DOCUMENT_VERSION,
            "network": network,
            "wallet_name": wallet_name,
            "draft_id": draft_id,
            "created_at": utc_now(),
            "utxo_source": utxo_source,
            "cache_file": str(cache_file),
            "unsigned_tx_hex": serialize_transaction_hex(funded_tx, include_witness=False),
            "inputs": input_metadata,
            "outputs": output_metadata,
            "total_input_sats": selection["total_input_sats"],
            "destination_total_sats": selection["destination_total_sats"],
            "estimated_fee_sats": selection["estimated_fee_sats"],
            "estimated_vsize": selection["estimated_vsize"],
            "requested_fee_rate_sat_vb": format(fee_rate, "f"),
            "max_fee_sats": max_fee_sats,
            "change_position": None,
            "send_all": True,
        }
        validate_funded_document(document, network, wallet_name)
        return document
    except Exception:
        _release_draft(cache_file, wallet_name, draft_id)
        raise


def _validate_prevouts_against_wallet_and_cache(
    prevouts: list[Prevout],
    outputs: list[dict],
    wallet_name: str,
    wallet_file: Path,
    cache_file: Path,
    network: str,
    draft_id: str,
) -> None:
    address_book = get_wallet_address_book(
        wallet_name,
        wallet_file=wallet_file,
        network=network,
    )
    wallet_entries = {item["path"]: item for item in address_book["addresses"]}
    with locked_cache_file(cache_file):
        cache = load_wallet_cache(cache_file)
        wallet_cache = cache.get("wallets", {}).get(wallet_name)
        if not isinstance(wallet_cache, dict):
            raise TransactionError("wallet UTXO cache is missing")
        cached_utxos = {
            f"{item.get('txid')}:{item.get('vout')}": item
            for item in wallet_cache.get("utxos", [])
            if isinstance(item, dict)
        }
        reservations = wallet_cache.get("reserved_outpoints", {})
        if not isinstance(reservations, dict):
            raise TransactionError("wallet UTXO reservations are invalid")
    for prevout in prevouts:
        entry = wallet_entries.get(prevout.derivation_path)
        cached = cached_utxos.get(prevout.outpoint)
        if not isinstance(entry, dict) or not isinstance(cached, dict):
            raise TransactionError(f'prevout "{prevout.outpoint}" is not owned and cached by wallet')
        if (
            entry.get("address") != prevout.address
            or entry.get("script_pubkey") != prevout.script_pubkey.hex()
            or entry.get("account_id") != prevout.account_id
            or str(entry.get("address_type", "")).lower() != prevout.address_type
            or cached.get("value") != prevout.value
            or cached.get("script_pubkey") != prevout.script_pubkey.hex()
            or cached.get("path") != prevout.derivation_path
            or cached.get("address") != prevout.address
            or cached.get("account_id") != prevout.account_id
        ):
            raise TransactionError(f'prevout "{prevout.outpoint}" metadata does not match wallet')
        reservation = reservations.get(prevout.outpoint)
        if not isinstance(reservation, dict) or reservation.get("draft_id") != draft_id:
            raise TransactionError(
                f'prevout "{prevout.outpoint}" is not reserved by this transaction draft'
            )
    input_type = prevouts[0].address_type
    for output in outputs:
        if not output.get("is_change"):
            continue
        entry = wallet_entries.get(output.get("derivation_path"))
        if (
            not isinstance(entry, dict)
            or entry.get("branch") != 1
            or entry.get("address") != output.get("address")
            or entry.get("script_pubkey") != output.get("script_pubkey")
            or entry.get("account_id") != output.get("account_id")
            or str(entry.get("address_type", "")).lower() != input_type
        ):
            raise TransactionError("funded transaction change output does not belong to wallet")


def sign_funded_transaction(
    document: dict,
    wallet_name: str,
    password: str | None,
    wallet_file: Path,
    cache_file: Path,
    network: str,
    *,
    max_fee_sats: int | None = None,
    final_fee_limit_message: bool = False,
) -> dict:
    tx, prevouts = validate_funded_document(document, network, wallet_name)
    _validate_prevouts_against_wallet_and_cache(
        prevouts,
        document["outputs"],
        wallet_name,
        wallet_file,
        cache_file,
        network,
        document["draft_id"],
    )
    requested_rate = parse_fee_rate(document.get("requested_fee_rate_sat_vb"))
    effective_max_fee = max_fee_sats if max_fee_sats is not None else document.get("max_fee_sats")
    if effective_max_fee is not None and (
        isinstance(effective_max_fee, bool)
        or not isinstance(effective_max_fee, int)
        or effective_max_fee < 0
    ):
        raise TransactionError("max fee is invalid")
    change_position = document.get("change_position")

    with WalletSigningSession(
        wallet_name,
        password,
        wallet_file,
        network,
    ) as session:
        for _ in range(3):
            signing_result = sign_transaction(tx, prevouts, session)
            minimum_fee = fee_for_vsize(signing_result["vsize"], requested_rate)
            if signing_result["fee_sats"] >= minimum_fee:
                break
            deficit = minimum_fee - signing_result["fee_sats"]
            if not isinstance(change_position, int) or not 0 <= change_position < len(tx.outputs):
                raise TransactionError("signed transaction fee is below requested rate and no change is available")
            new_change = tx.outputs[change_position].value - deficit
            input_type = prevouts[0].address_type
            if new_change < DUST_THRESHOLDS[input_type]:
                raise TransactionError("change would become dust while correcting signed fee")
            tx.outputs[change_position].value = new_change
        else:
            raise TransactionError("could not reach requested fee rate after signing")

    if effective_max_fee is not None and signing_result["fee_sats"] > effective_max_fee:
        try:
            _release_draft(cache_file, wallet_name, document["draft_id"])
        except (TransactionError, WalletError):
            pass
        if final_fee_limit_message:
            raise TransactionError(
                f"final fee {signing_result['fee_sats']} sats exceeds max fee "
                f"{effective_max_fee} sats; transaction was not broadcast"
            )
        raise TransactionError(
            f'signed fee {signing_result["fee_sats"]} exceeds max fee {effective_max_fee}'
        )
    output_metadata = transaction_output_metadata(tx, network)
    funded_outputs = document["outputs"]
    for item in output_metadata:
        original = funded_outputs[item["index"]]
        item["is_change"] = original["is_change"]
        if original["is_change"]:
            item["derivation_path"] = original.get("derivation_path")
            item["account_id"] = original.get("account_id")
    signed_document = {
        "format": SIGNED_FORMAT,
        "version": DOCUMENT_VERSION,
        "network": network,
        "wallet_name": wallet_name,
        "draft_id": document["draft_id"],
        "signed_at": utc_now(),
        "hex": serialize_transaction_hex(tx),
        "complete": True,
        "inputs": document["inputs"],
        "outputs": output_metadata,
        "change_position": change_position,
        "requested_fee_rate_sat_vb": format(requested_rate, "f"),
        "max_fee_sats": effective_max_fee,
        **signing_result,
    }
    return signed_document


def record_successful_broadcast(
    signed_document: dict,
    cache_file: Path,
    backend_url: str,
) -> None:
    wallet_name = signed_document.get("wallet_name")
    draft_id = signed_document.get("draft_id")
    with locked_cache_file(cache_file):
        cache = load_wallet_cache(cache_file)
        wallet_cache = cache.get("wallets", {}).get(wallet_name)
        if not isinstance(wallet_cache, dict):
            raise TransactionError("wallet cache is missing after broadcast")
        reservations = wallet_cache.get("reserved_outpoints", {})
        if isinstance(reservations, dict):
            wallet_cache["reserved_outpoints"] = {
                outpoint: item
                for outpoint, item in reservations.items()
                if not isinstance(item, dict) or item.get("draft_id") != draft_id
            }
        pending = wallet_cache.setdefault("pending_transactions", [])
        if not isinstance(pending, list):
            raise TransactionError("wallet pending transaction cache is invalid")
        pending_entry = {
                "txid": signed_document["txid"],
                "wtxid": signed_document["wtxid"],
                "raw_tx_hex": signed_document["hex"],
                "broadcast_at": utc_now(),
                "network": signed_document["network"],
                "backend": backend_url,
                "inputs": [f"{item['txid']}:{item['vout']}" for item in signed_document["inputs"]],
                "change_addresses": [
                    item["address"]
                    for item in signed_document["outputs"]
                    if item.get("is_change")
                ],
                "status": "broadcast",
            }
        wallet_cache["pending_transactions"] = [
            item
            for item in pending
            if not isinstance(item, dict) or item.get("txid") != signed_document["txid"]
        ]
        wallet_cache["pending_transactions"].append(pending_entry)
        pending_spent = wallet_cache.setdefault("pending_spent_outpoints", {})
        if not isinstance(pending_spent, dict):
            raise TransactionError("wallet pending-spent outpoints are invalid")
        for item in signed_document["inputs"]:
            pending_spent[f"{item['txid']}:{item['vout']}"] = {
                "spending_txid": signed_document["txid"],
                "broadcast_at": pending_entry["broadcast_at"],
            }
        save_wallet_cache(cache, cache_file)


def broadcast_signed_transaction(
    document: dict,
    network: str,
    backend,
    *,
    cache_file: Path | None = None,
    max_fee_sats: int | None = None,
) -> dict:
    """Run local preflight checks, broadcast, then persist public pending state."""
    tx, prevouts = validate_signed_document(document, network)
    verification = verify_all_inputs(tx, prevouts)
    if not verification or not all(item["valid"] for item in verification):
        raise TransactionError("local transaction signature verification failed")

    metrics = transaction_metrics(tx)
    for field in ("txid", "wtxid", "size", "stripped_size", "weight", "vsize"):
        if field in document and document[field] != metrics[field]:
            raise TransactionError(f'signed transaction field "{field}" does not match raw transaction')
    input_total = sum(prevout.value for prevout in prevouts)
    output_total = sum(output.value for output in tx.outputs)
    fee = input_total - output_total
    if fee < 0:
        raise TransactionError("transaction outputs exceed known input value")
    if document.get("fee_sats") != fee:
        raise TransactionError("signed transaction fee metadata does not match raw transaction")
    max_fee = document.get("max_fee_sats")
    if max_fee_sats is not None:
        if isinstance(max_fee_sats, bool) or not isinstance(max_fee_sats, int) or max_fee_sats < 0:
            raise TransactionError("max fee is invalid")
        max_fee = min(max_fee, max_fee_sats) if max_fee is not None else max_fee_sats
    if max_fee is not None and fee > max_fee:
        raise TransactionError(f"transaction fee {fee} exceeds max fee {max_fee}")

    if getattr(backend, "network", None) != network:
        raise TransactionError("broadcast backend network does not match transaction network")
    backend.verify_network()
    remote_txid = backend.broadcast_transaction(document["hex"])
    if remote_txid != metrics["txid"]:
        raise TransactionError(
            "broadcast backend returned a transaction id that does not match local serialization"
        )
    cache_warning = None
    if cache_file is not None and document.get("wallet_name"):
        try:
            record_successful_broadcast(document, cache_file, backend.base_url)
        except (TransactionError, WalletError) as exc:
            cache_warning = str(exc)
    return {
        **metrics,
        "fee_sats": fee,
        "verification": verification,
        "backend": backend.base_url,
        "cache_warning": cache_warning,
    }
