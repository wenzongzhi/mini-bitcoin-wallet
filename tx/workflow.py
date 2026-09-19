"""Wallet funding, signing, reservation, and broadcast state workflows."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import uuid

from wallet import (
    WalletError,
    WalletSigningSession,
    get_wallet_address_book,
)
from wallet.wallet import (
    derive_next_change_address_candidate,
    derive_wallet_address_candidate,
    issue_change_address,
)
from wallet.wallet_cache import (
    load_wallet_cache,
    locked_cache_file,
    save_wallet_cache,
    utc_now,
)
from wallet.transaction_accounting import (
    calculate_wallet_balance,
    pending_summary_from_signed,
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
    """Validate UTXO reservations without expiring active payment state."""

    del now
    reservations = wallet_cache.setdefault("reserved_outpoints", {})
    if not isinstance(reservations, dict):
        raise TransactionError("wallet cache reservations are invalid")
    validated = {}
    for outpoint, reservation in reservations.items():
        if not isinstance(reservation, dict):
            raise TransactionError("wallet cache reservation entry is invalid")
        _parse_timestamp(reservation.get("reserved_at"), "reservation")
        if not isinstance(reservation.get("draft_id"), str):
            raise TransactionError("wallet cache reservation draft id is invalid")
        validated[outpoint] = reservation
    return validated


def _validated_reserved_change(wallet_cache: dict) -> dict | None:
    """Read the optional minimal change reservation from cache."""

    reservation = wallet_cache.get("reserved_change")
    if reservation is None:
        return None
    if not isinstance(reservation, dict):
        raise TransactionError("wallet change-address reservation is invalid")
    draft_id = reservation.get("draft_id")
    address_type = reservation.get("address_type")
    index = reservation.get("index")
    if not isinstance(draft_id, str) or not draft_id:
        raise TransactionError("wallet change-address reservation draft id is invalid")
    if str(address_type).lower() not in {"p2pkh", "p2wpkh"}:
        raise TransactionError("wallet change-address reservation type is invalid")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 2**31:
        raise TransactionError("wallet change-address reservation index is invalid")
    _parse_timestamp(reservation.get("reserved_at"), "change reservation")
    return reservation


def _active_draft_ids(wallet_cache: dict) -> set[str]:
    """Return draft ids inferred from the two reservation fields."""

    reservations = wallet_cache.get("reserved_outpoints", {})
    if not isinstance(reservations, dict):
        raise TransactionError("wallet cache reservations are invalid")
    draft_ids = {
        item["draft_id"]
        for item in reservations.values()
        if isinstance(item, dict) and isinstance(item.get("draft_id"), str)
    }
    reserved_change = _validated_reserved_change(wallet_cache)
    if reserved_change is not None:
        draft_ids.add(reserved_change["draft_id"])
    return draft_ids


def _require_no_active_draft(wallet_cache: dict) -> None:
    if _active_draft_ids(wallet_cache):
        raise TransactionError("wallet already has an active payment draft")


def _cancel_draft_in_wallet_cache(wallet_cache: dict, draft_id: str) -> bool:
    """Release one draft; an address already issued in wallets.json is untouched."""

    changed = False
    reservations = wallet_cache.get("reserved_outpoints", {})
    if not isinstance(reservations, dict):
        raise TransactionError("wallet cache reservations are invalid")
    retained = {
        outpoint: item
        for outpoint, item in reservations.items()
        if not isinstance(item, dict) or item.get("draft_id") != draft_id
    }
    if retained != reservations:
        changed = True
        wallet_cache["reserved_outpoints"] = retained
    reserved_change = wallet_cache.get("reserved_change")
    if isinstance(reserved_change, dict) and reserved_change.get("draft_id") == draft_id:
        wallet_cache.pop("reserved_change", None)
        changed = True
    return changed


def _release_draft(cache_file: Path, wallet_name: str, draft_id: str) -> None:
    with locked_cache_file(cache_file):
        cache = load_wallet_cache(cache_file)
        wallet_cache = cache.get("wallets", {}).get(wallet_name)
        if not isinstance(wallet_cache, dict):
            return
        if _cancel_draft_in_wallet_cache(wallet_cache, draft_id):
            save_wallet_cache(cache, cache_file)


def release_transaction_draft(
    cache_file: Path,
    wallet_name: str,
    draft_id: str,
) -> None:
    """Idempotently release a payment draft's UTXOs and RESERVED change."""

    _release_draft(cache_file, wallet_name, draft_id)


def cancel_transaction_draft(
    cache_file: Path,
    draft_id: str,
    wallet_name: str | None = None,
) -> bool:
    """Cancel a draft after restart, optionally locating its wallet by id."""

    with locked_cache_file(cache_file):
        cache = load_wallet_cache(cache_file)
        wallets = cache.get("wallets", {})
        if not isinstance(wallets, dict):
            raise TransactionError("wallet cache is invalid")
        candidates = [wallet_name] if wallet_name is not None else list(wallets)
        matches = []
        for candidate in candidates:
            wallet_cache = wallets.get(candidate)
            if not isinstance(wallet_cache, dict):
                continue
            reservations = wallet_cache.get("reserved_outpoints", {})
            if not isinstance(reservations, dict):
                raise TransactionError("wallet cache reservations are invalid")
            has_inputs = any(
                isinstance(item, dict) and item.get("draft_id") == draft_id
                for item in reservations.values()
            )
            reserved_change = wallet_cache.get("reserved_change")
            has_change = (
                isinstance(reserved_change, dict)
                and reserved_change.get("draft_id") == draft_id
            )
            if has_inputs or has_change:
                matches.append((candidate, wallet_cache))
        if len(matches) > 1:
            raise TransactionError("payment draft id is ambiguous across wallets")
        if not matches:
            return False
        _, wallet_cache = matches[0]
        changed = _cancel_draft_in_wallet_cache(wallet_cache, draft_id)
        if changed:
            save_wallet_cache(cache, cache_file)
        return changed


def _reserve_change_address(
    wallet_name: str,
    draft_id: str,
    wallet_file: Path,
    cache_file: Path,
    *,
    address_type: str,
    network: str,
) -> dict:
    """Reserve the current change index without writing it to wallets.json."""

    with locked_cache_file(cache_file):
        cache = load_wallet_cache(cache_file)
        wallet_cache = cache.get("wallets", {}).get(wallet_name)
        if not isinstance(wallet_cache, dict):
            raise TransactionError("wallet cache is missing while reserving change")
        if wallet_cache.get("reserved_change") is not None:
            _validated_reserved_change(wallet_cache)
            raise TransactionError("wallet change address is already reserved")
        reservations = wallet_cache.get("reserved_outpoints", {})
        if not isinstance(reservations, dict):
            raise TransactionError("wallet cache reservations are invalid")
        if not any(
            isinstance(item, dict) and item.get("draft_id") == draft_id
            for item in reservations.values()
        ):
            raise TransactionError("payment draft has no reserved inputs")
        try:
            candidate = derive_next_change_address_candidate(
                wallet_name,
                wallet_file=wallet_file,
                address_type=address_type,
                network=network,
            )
        except WalletError as exc:
            raise TransactionError(str(exc)) from exc
        wallet_cache["reserved_change"] = {
            "draft_id": draft_id,
            "address_type": candidate["address_type"],
            "index": candidate["index"],
            "reserved_at": utc_now(),
        }
        save_wallet_cache(cache, cache_file)
    return candidate


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
        wallet_cache["reserved_outpoints"] = active_reservations
        _require_no_active_draft(wallet_cache)
        pending_spent = wallet_cache.get("pending_spent_outpoints", {})
        if not isinstance(pending_spent, dict):
            raise TransactionError("wallet pending-spent outpoints are invalid")
        reserved_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
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
                change_entry = _reserve_change_address(
                    wallet_name,
                    draft_id,
                    wallet_file,
                    cache_file,
                    address_type=address_type,
                    network=network,
                )
            except WalletError as exc:
                raise TransactionError(str(exc)) from exc
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
            # Persist absolute paths because funded documents can be signed and
            # broadcast from a different working directory or after a restart.
            "cache_file": str(cache_file.resolve()),
            "wallet_file": str(wallet_file.resolve()),
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
        wallet_cache["reserved_outpoints"] = active_reservations
        _require_no_active_draft(wallet_cache)
        pending_spent = wallet_cache.get("pending_spent_outpoints", {})
        if not isinstance(pending_spent, dict):
            raise TransactionError("wallet pending-spent outpoints are invalid")
        reserved_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
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
        reserved_change = _validated_reserved_change(wallet_cache)
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
        entry = None
        if isinstance(reserved_change, dict):
            index = reserved_change.get("index")
            if (
                reserved_change.get("draft_id") != draft_id
                or isinstance(index, bool)
                or not isinstance(index, int)
                or str(reserved_change.get("address_type", "")).lower()
                != input_type
            ):
                raise TransactionError(
                    "funded transaction change reservation does not match draft"
                )
            try:
                entry = derive_wallet_address_candidate(
                    wallet_name,
                    index,
                    wallet_file=wallet_file,
                    change=True,
                    address_type=input_type,
                    network=network,
                )
            except WalletError as exc:
                raise TransactionError(str(exc)) from exc
        else:
            entry = next(
                (
                    candidate
                    for candidate in address_book["addresses"]
                    if candidate.get("branch") == 1
                    and candidate.get("address") == output.get("address")
                ),
                None,
            )
        if not isinstance(entry, dict) or not _change_output_matches(entry, output, input_type):
            raise TransactionError("funded transaction change output does not belong to wallet")


def _change_output_matches(entry: dict, output: dict, address_type: str) -> bool:
    return (
        entry.get("branch") == 1
        and entry.get("address") == output.get("address")
        and entry.get("script_pubkey") == output.get("script_pubkey")
        and entry.get("account_id") == output.get("account_id")
        and entry.get("path") == output.get("derivation_path")
        and str(entry.get("address_type", "")).lower() == address_type
    )


def _sign_funded_transaction(
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
        "wallet_file": document.get("wallet_file"),
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
    """Sign a draft and issue its RESERVED change address exactly once."""

    change_was_issued = False
    try:
        signed = _sign_funded_transaction(
            document,
            wallet_name,
            password,
            wallet_file,
            cache_file,
            network,
            max_fee_sats=max_fee_sats,
            final_fee_limit_message=final_fee_limit_message,
        )
        change_outputs = [
            output for output in signed["outputs"] if output.get("is_change")
        ]
        if change_outputs:
            output = change_outputs[0]
            address_type = signed["inputs"][0]["address_type"]
            with locked_cache_file(cache_file):
                cache = load_wallet_cache(cache_file)
                wallet_cache = cache.get("wallets", {}).get(wallet_name)
                if not isinstance(wallet_cache, dict):
                    raise TransactionError("wallet cache is missing after signing")
                reserved_change = _validated_reserved_change(wallet_cache)
                if reserved_change is None:
                    _validate_issued_change_output(
                        wallet_name,
                        output,
                        wallet_file,
                        address_type=address_type,
                        network=network,
                    )
                else:
                    index = reserved_change.get("index")
                    if (
                        reserved_change.get("draft_id") != signed["draft_id"]
                        or isinstance(index, bool)
                        or not isinstance(index, int)
                        or str(reserved_change.get("address_type", "")).lower()
                        != address_type
                    ):
                        raise TransactionError(
                            "wallet change-address reservation does not match signed draft"
                        )
                    candidate = derive_wallet_address_candidate(
                        wallet_name,
                        index,
                        wallet_file=wallet_file,
                        change=True,
                        address_type=address_type,
                        network=network,
                    )
                    if not _change_output_matches(candidate, output, address_type):
                        raise TransactionError(
                            "signed change output does not match its reservation"
                        )
                    issue_change_address(
                        wallet_name,
                        index,
                        wallet_file=wallet_file,
                        address_type=address_type,
                        network=network,
                    )
                    change_was_issued = True
                    wallet_cache.pop("reserved_change", None)
                    save_wallet_cache(cache, cache_file)
        return signed
    except Exception:
        draft_id = document.get("draft_id") if isinstance(document, dict) else None
        if isinstance(draft_id, str) and not change_was_issued:
            try:
                _release_draft(cache_file, wallet_name, draft_id)
            except (TransactionError, WalletError):
                pass
        raise


def _validate_issued_change_output(
    wallet_name: str,
    output: dict,
    wallet_file: Path,
    *,
    address_type: str,
    network: str,
) -> None:
    """Require a change output to exist in the already-issued address book."""

    address_book = get_wallet_address_book(
        wallet_name,
        wallet_file=wallet_file,
        address_type=address_type,
        network=network,
    )
    entry = next(
        (
            candidate
            for candidate in address_book["addresses"]
            if candidate.get("branch") == 1
            and candidate.get("address") == output.get("address")
        ),
        None,
    )
    if not isinstance(entry, dict) or not _change_output_matches(
        entry,
        output,
        address_type,
    ):
        raise TransactionError("change address has not been issued by this wallet")


def record_successful_broadcast(
    signed_document: dict,
    cache_file: Path,
    backend_url: str,
    wallet_file: Path | None = None,
) -> None:
    wallet_name = signed_document.get("wallet_name")
    draft_id = signed_document.get("draft_id")
    summary = None
    if wallet_file is not None:
        address_book = get_wallet_address_book(
            wallet_name,
            wallet_file=wallet_file,
            network=signed_document["network"],
        )
        owned = {entry["address"]: entry for entry in address_book["addresses"]}
        summary = pending_summary_from_signed(signed_document, owned)
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
        if summary is not None:
            pending_entry["wallet_delta_sats"] = summary["net"]
            pending_entry["observed_in_sync"] = False
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
        if summary is not None:
            transactions = wallet_cache.setdefault("transactions", [])
            if not isinstance(transactions, list):
                raise TransactionError("wallet transaction cache is invalid")
            wallet_cache["transactions"] = [
                summary,
                *(
                    transaction
                    for transaction in transactions
                    if not isinstance(transaction, dict)
                    or transaction.get("txid") != summary["txid"]
                ),
            ]
        wallet_cache.setdefault("balance", {}).update(
            calculate_wallet_balance(wallet_cache).cache_fields()
        )
        save_wallet_cache(cache, cache_file)


def broadcast_signed_transaction(
    document: dict,
    network: str,
    backend,
    *,
    cache_file: Path | None = None,
    wallet_file: Path | None = None,
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
    effective_wallet_file = wallet_file or (
        Path(document["wallet_file"])
        if isinstance(document.get("wallet_file"), str)
        else None
    )
    change_outputs = [
        output for output in document.get("outputs", []) if output.get("is_change")
    ]
    if cache_file is not None:
        with locked_cache_file(cache_file):
            cache = load_wallet_cache(cache_file)
            wallet_cache = cache.get("wallets", {}).get(document["wallet_name"])
            if not isinstance(wallet_cache, dict):
                raise TransactionError("wallet cache is missing before broadcast")
            reservations = wallet_cache.get("reserved_outpoints", {})
            if not isinstance(reservations, dict):
                raise TransactionError("wallet cache reservations are invalid")
            for item in document["inputs"]:
                outpoint = f"{item['txid']}:{item['vout']}"
                reservation = reservations.get(outpoint)
                if (
                    not isinstance(reservation, dict)
                    or reservation.get("draft_id") != document["draft_id"]
                ):
                    raise TransactionError(
                        f'input "{outpoint}" is not reserved by this signed draft'
                    )
    if change_outputs:
        if effective_wallet_file is None:
            raise TransactionError(
                "wallet file is required to validate issued change before broadcast"
            )
        try:
            _validate_issued_change_output(
                document["wallet_name"],
                change_outputs[0],
                effective_wallet_file,
                address_type=document["inputs"][0]["address_type"],
                network=network,
            )
        except WalletError as exc:
            raise TransactionError(str(exc)) from exc
    backend.verify_network()
    remote_txid = backend.broadcast_transaction(document["hex"])
    if remote_txid != metrics["txid"]:
        raise TransactionError(
            "broadcast backend returned a transaction id that does not match local serialization"
        )
    cache_warning = None
    if cache_file is not None and document.get("wallet_name"):
        try:
            record_successful_broadcast(
                document,
                cache_file,
                backend.base_url,
                effective_wallet_file,
            )
        except (TransactionError, WalletError) as exc:
            cache_warning = str(exc)
    return {
        **metrics,
        "fee_sats": fee,
        "verification": verification,
        "backend": backend.base_url,
        "cache_warning": cache_warning,
    }
