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


@dataclass(frozen=True, slots=True)
class WalletBalance:
    """Authoritative, optimistic, effective, and spendable wallet amounts."""

    confirmed_sats: int
    unconfirmed_chain_sats: int
    authoritative_sats: int
    pending_delta_sats: int
    effective_sats: int
    available_sats: int

    def cache_fields(self) -> dict[str, int]:
        return {
            "confirmed": self.confirmed_sats,
            "unconfirmed": self.unconfirmed_chain_sats,
            "total": self.authoritative_sats,
            "pending_delta": self.pending_delta_sats,
            "effective": self.effective_sats,
            "available": self.available_sats,
        }


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
    has_external_output = any(
        isinstance(item.get("value"), int)
        and not isinstance(item.get("value"), bool)
        and item["value"] > 0
        and item.get("address") not in owned_address_entries
        for item in outputs
    )
    direction = transaction_direction(sent, received, net, has_external_output)

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
        pending = wallet_cache.get("pending_transactions", [])
        if isinstance(pending, list):
            for item in pending:
                if isinstance(item, dict) and item.get("txid") == summary["txid"]:
                    item["wallet_delta_sats"] = summary["net"]
                    item["observed_in_sync"] = False
        wallet_cache.setdefault("balance", {}).update(
            calculate_wallet_balance(wallet_cache).cache_fields()
        )
        save_wallet_cache(cache, cache_file)


def calculate_wallet_balance(
    wallet_cache: dict,
    *,
    now: datetime | None = None,
    reservation_ttl_seconds: int | None = None,
) -> WalletBalance:
    """Calculate balances while every active draft keeps its inputs unavailable.

    The optional timing arguments remain for source compatibility but no
    longer expire reservations. Draft state changes require an explicit
    cancel/failure transition or successful broadcast.
    """

    del now, reservation_ttl_seconds

    chain_balance = wallet_cache.get("balance", {})
    if not isinstance(chain_balance, dict):
        chain_balance = {}
    if "confirmed" in chain_balance and "unconfirmed" in chain_balance:
        confirmed = _non_negative_integer(chain_balance.get("confirmed"))
        unconfirmed = _non_negative_integer(chain_balance.get("unconfirmed"))
    else:
        confirmed, unconfirmed = _chain_balance_from_utxos(wallet_cache)
    authoritative = confirmed + unconfirmed

    pending_delta = 0
    pending = wallet_cache.get("pending_transactions", [])
    if isinstance(pending, list):
        for item in pending:
            if not isinstance(item, dict) or item.get("observed_in_sync") is True:
                continue
            delta = item.get("wallet_delta_sats")
            if isinstance(delta, int) and not isinstance(delta, bool):
                pending_delta += delta

    blocked_outpoints = set()
    pending_spent = wallet_cache.get("pending_spent_outpoints", {})
    if isinstance(pending_spent, dict):
        blocked_outpoints.update(pending_spent)
    reservations = wallet_cache.get("reserved_outpoints", {})
    if isinstance(reservations, dict):
        blocked_outpoints.update(
            outpoint
            for outpoint, reservation in reservations.items()
            if isinstance(reservation, dict)
        )
    available = 0
    utxos = wallet_cache.get("utxos", [])
    if isinstance(utxos, list):
        for utxo in utxos:
            if not isinstance(utxo, dict) or utxo.get("confirmed") is not True:
                continue
            outpoint = f"{utxo.get('txid')}:{utxo.get('vout')}"
            value = utxo.get("value")
            if (
                outpoint not in blocked_outpoints
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
            ):
                available += value
    return WalletBalance(
        confirmed,
        unconfirmed,
        authoritative,
        pending_delta,
        authoritative + pending_delta,
        available,
    )


def transaction_direction(
    sent_sats: int,
    received_sats: int,
    net_sats: int,
    has_external_output: bool,
) -> str:
    """Classify confirmed and optimistic summaries with identical semantics."""

    if sent_sats and received_sats:
        if not has_external_output:
            return "self"
        return "receive" if net_sats > 0 else "send"
    return "send" if sent_sats else "receive"


def _chain_balance_from_utxos(wallet_cache: dict) -> tuple[int, int]:
    confirmed = 0
    unconfirmed = 0
    utxos = wallet_cache.get("utxos", [])
    if not isinstance(utxos, list):
        return 0, 0
    for utxo in utxos:
        if not isinstance(utxo, dict):
            continue
        value = utxo.get("value")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            continue
        if utxo.get("confirmed") is True:
            confirmed += value
        else:
            unconfirmed += value
    return confirmed, unconfirmed


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
