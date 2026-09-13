"""Canonical cached transaction accounting shared by wallet platform services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .wallet import WalletError
from .wallet_cache import load_wallet_cache, locked_cache_file, save_wallet_cache


@dataclass(frozen=True, slots=True)
class AccountedTransaction:
    txid: str
    direction: str
    received_sats: int
    sent_sats: int
    net_sats: int
    fee_sats: int
    confirmed: bool
    confirmations: int
    block_height: int | None
    block_time: datetime | None
    addresses: tuple[str, ...]
    account_ids: tuple[str, ...]
    address_types: tuple[str, ...]


def transaction_from_cache(value: dict) -> AccountedTransaction | None:
    """Validate and normalize one transaction from the public wallet cache."""

    if not isinstance(value, dict):
        return None
    txid = value.get("txid")
    net_sats = value.get("net")
    if not isinstance(txid, str) or len(txid) != 64:
        return None
    if isinstance(net_sats, bool) or not isinstance(net_sats, int):
        return None
    direction = value.get("direction")
    if direction not in {"receive", "send", "self"}:
        direction = "receive" if net_sats >= 0 else "send"
    status = value.get("status", {})
    if not isinstance(status, dict):
        status = {}
    block_height = _optional_integer(status.get("block_height"))
    block_time = _timestamp(status.get("block_time"))
    confirmations = _non_negative_integer(value.get("confirmations"))
    return AccountedTransaction(
        txid=txid,
        direction=direction,
        received_sats=_non_negative_integer(value.get("received")),
        sent_sats=_non_negative_integer(value.get("sent")),
        net_sats=net_sats,
        fee_sats=_non_negative_integer(value.get("fee")),
        confirmed=value.get("confirmed") is True,
        confirmations=confirmations,
        block_height=block_height,
        block_time=block_time,
        addresses=_string_tuple(value.get("addresses")),
        account_ids=_string_tuple(value.get("account_ids")),
        address_types=_string_tuple(value.get("address_types")),
    )


def pending_summary_from_signed(
    signed: dict,
    owned_address_entries: dict[str, dict],
) -> dict:
    """Account a locally signed transaction using the synchronized schema."""

    inputs = signed["inputs"]
    outputs = signed["outputs"]
    sent = sum(item["value"] for item in inputs)
    owned_outputs = [
        item for item in outputs if item.get("address") in owned_address_entries
    ]
    received = sum(item["value"] for item in owned_outputs)
    net = received - sent
    if sent and received:
        direction = "self" if net == 0 else ("receive" if net > 0 else "send")
    else:
        direction = "send" if sent else "receive"

    involved_addresses = {
        item["address"] for item in inputs if item.get("address")
    }
    involved_addresses.update(item["address"] for item in owned_outputs)
    involved_entries = [
        owned_address_entries[address]
        for address in involved_addresses
        if address in owned_address_entries
    ]
    return {
        "txid": signed["txid"],
        "direction": direction,
        "received": received,
        "sent": sent,
        "net": net,
        "fee": signed["fee_sats"],
        "status": {"confirmed": False},
        "confirmed": False,
        "confirmations": 0,
        "addresses": sorted(involved_addresses),
        "account_ids": sorted({item["account_id"] for item in involved_entries}),
        "address_types": sorted(
            {item["address_type"] for item in involved_entries}
        ),
    }


def upsert_cached_transaction(
    wallet_name: str,
    summary: dict,
    cache_file,
) -> None:
    """Place a pending summary first while preserving every other transaction."""

    with locked_cache_file(cache_file):
        cache = load_wallet_cache(cache_file)
        wallet_cache = cache.get("wallets", {}).get(wallet_name)
        if not isinstance(wallet_cache, dict):
            raise WalletError("wallet cache is missing after broadcast")
        transactions = wallet_cache.get("transactions", [])
        if not isinstance(transactions, list):
            raise WalletError("wallet cache transaction list is invalid")
        wallet_cache["transactions"] = [
            summary,
            *(
                transaction
                for transaction in transactions
                if not isinstance(transaction, dict)
                or transaction.get("txid") != summary["txid"]
            ),
        ]
        save_wallet_cache(cache, cache_file)


def _non_negative_integer(value) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else 0
    )


def _optional_integer(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _timestamp(value) -> datetime | None:
    timestamp = _optional_integer(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _string_tuple(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))
