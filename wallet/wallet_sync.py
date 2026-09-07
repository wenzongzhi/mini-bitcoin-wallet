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

from pathlib import Path

from network.esplora_backend import EsploraBackend
from btc.chainparams import NETWORK_MAINNET

from .wallet import (
    WalletError,
    get_wallet_address_book,
    mark_wallet_addresses_used,
)
from .wallet_cache import (
    CACHE_VERSION,
    default_wallet_cache_file,
    load_wallet_cache,
    locked_cache_file,
    read_wallet_cache_entry,
    save_wallet_cache,
    utc_now,
)


def _stats_value(stats: dict, key: str) -> int:
    value = stats.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _address_is_used(address_data: dict, utxos: list[dict], transactions: list[dict]) -> bool:
    chain_stats = address_data.get("chain_stats", {})
    mempool_stats = address_data.get("mempool_stats", {})
    if not isinstance(chain_stats, dict):
        chain_stats = {}
    if not isinstance(mempool_stats, dict):
        mempool_stats = {}
    return any(
        _stats_value(stats, "tx_count") > 0
        or _stats_value(stats, "funded_txo_count") > 0
        or _stats_value(stats, "spent_txo_count") > 0
        for stats in (chain_stats, mempool_stats)
    ) or bool(utxos) or bool(transactions)


def _confirmations(status: dict, tip_height: int | None) -> int:
    if not isinstance(status, dict) or not status.get("confirmed"):
        return 0
    block_height = status.get("block_height")
    if not isinstance(block_height, int) or tip_height is None:
        return 0
    return max(tip_height - block_height + 1, 0)


def _normalize_utxo(utxo: dict, address_entry: dict, tip_height: int | None) -> dict:
    if not isinstance(utxo, dict):
        raise WalletError("invalid UTXO response")
    try:
        txid = utxo["txid"]
        vout = int(utxo["vout"])
        value = int(utxo["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WalletError("invalid UTXO response") from exc
    status = utxo.get("status", {})
    if not isinstance(txid, str) or len(txid) != 64:
        raise WalletError("invalid UTXO response")
    if not isinstance(status, dict):
        status = {}
    return {
        "txid": txid,
        "vout": vout,
        "value": value,
        "status": status,
        "confirmed": bool(status.get("confirmed")),
        "confirmations": _confirmations(status, tip_height),
        "address": address_entry["address"],
        "path": address_entry["path"],
        "branch": address_entry["branch"],
        "index": address_entry["index"],
        "script_pubkey": address_entry["script_pubkey"],
        "account_id": address_entry["account_id"],
        "address_type": address_entry["address_type"],
    }


def _transaction_addresses(tx: dict, address_map: dict[str, dict]) -> set[str]:
    addresses = set()
    for output in tx.get("vout", []):
        if not isinstance(output, dict):
            continue
        address = output.get("scriptpubkey_address")
        if isinstance(address, str) and address in address_map:
            addresses.add(address)
    for input_entry in tx.get("vin", []):
        if not isinstance(input_entry, dict):
            continue
        previous_output = input_entry.get("prevout")
        if not isinstance(previous_output, dict):
            continue
        address = previous_output.get("scriptpubkey_address")
        if isinstance(address, str) and address in address_map:
            addresses.add(address)
    return addresses


def _summarize_transaction(tx: dict, address_map: dict[str, dict], tip_height: int | None) -> dict | None:
    if not isinstance(tx, dict):
        return None
    txid = tx.get("txid")
    if not isinstance(txid, str) or len(txid) != 64:
        return None

    received = 0
    sent = 0
    for output in tx.get("vout", []):
        if not isinstance(output, dict):
            continue
        address = output.get("scriptpubkey_address")
        if isinstance(address, str) and address in address_map:
            value = output.get("value", 0)
            if isinstance(value, int) and not isinstance(value, bool):
                received += value

    for input_entry in tx.get("vin", []):
        if not isinstance(input_entry, dict):
            continue
        previous_output = input_entry.get("prevout")
        if not isinstance(previous_output, dict):
            continue
        address = previous_output.get("scriptpubkey_address")
        if isinstance(address, str) and address in address_map:
            value = previous_output.get("value", 0)
            if isinstance(value, int) and not isinstance(value, bool):
                sent += value

    involved_addresses = sorted(_transaction_addresses(tx, address_map))
    if not involved_addresses:
        return None
    involved_entries = [address_map[address] for address in involved_addresses]
    net = received - sent
    if sent and received:
        direction = "self" if net == 0 else ("receive" if net > 0 else "send")
    elif sent:
        direction = "send"
    else:
        direction = "receive"

    status = tx.get("status", {})
    if not isinstance(status, dict):
        status = {}
    fee = tx.get("fee", 0)
    if not isinstance(fee, int) or isinstance(fee, bool):
        fee = 0
    return {
        "txid": txid,
        "direction": direction,
        "received": received,
        "sent": sent,
        "net": net,
        "fee": fee,
        "status": status,
        "confirmed": bool(status.get("confirmed")),
        "confirmations": _confirmations(status, tip_height),
        "addresses": involved_addresses,
        "account_ids": sorted({entry["account_id"] for entry in involved_entries}),
        "address_types": sorted(
            {entry["address_type"] for entry in involved_entries}
        ),
    }


def _balance_from_utxos(utxos: list[dict]) -> dict:
    confirmed = 0
    unconfirmed = 0
    for utxo in utxos:
        value = utxo.get("value", 0)
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        if utxo.get("confirmed"):
            confirmed += value
        else:
            unconfirmed += value
    return {
        "confirmed": confirmed,
        "unconfirmed": unconfirmed,
        "total": confirmed + unconfirmed,
    }


def sync_wallet(
    wallet_name: str,
    wallet_file: Path | None = None,
    cache_file: Path | None = None,
    backend: EsploraBackend | None = None,
    include_transactions: bool = True,
    network: str = NETWORK_MAINNET,
) -> dict:
    backend = backend or EsploraBackend(network=network)
    if getattr(backend, "network", None) != network:
        raise WalletError(
            f'wallet network is "{network}", but backend network is '
            f'"{getattr(backend, "network", None)}"'
        )
    backend.verify_network()
    cache_path = cache_file or default_wallet_cache_file(network=network)
    address_book = get_wallet_address_book(
        wallet_name,
        wallet_file,
        network=network,
    )
    address_entries = address_book["addresses"]
    address_map = {entry["address"]: entry for entry in address_entries}

    tip_height = backend.get_tip_height()
    tip_hash = backend.get_tip_hash()
    synced_at = utc_now()
    address_caches = []
    all_utxos = []
    tx_by_id = {}
    used_addresses = set()

    for entry in address_entries:
        address = entry["address"]
        address_data = backend.get_address(address)
        utxos = backend.get_address_utxos(address)
        transactions = (
            backend.get_address_transactions(address) if include_transactions else []
        )
        if _address_is_used(address_data, utxos, transactions):
            used_addresses.add(address)

        normalized_utxos = [
            _normalize_utxo(utxo, entry, tip_height)
            for utxo in utxos
        ]
        all_utxos.extend(normalized_utxos)
        for tx in transactions:
            if isinstance(tx, dict) and isinstance(tx.get("txid"), str):
                tx_by_id.setdefault(tx["txid"], tx)

        address_caches.append(
            {
                "address": address,
                "account_id": entry["account_id"],
                "address_type": entry["address_type"],
                "path": entry["path"],
                "branch": entry["branch"],
                "index": entry["index"],
                "script_pubkey": entry["script_pubkey"],
                "used": address in used_addresses,
                "chain_stats": address_data.get("chain_stats", {}),
                "mempool_stats": address_data.get("mempool_stats", {}),
                "utxo_count": len(normalized_utxos),
                "transaction_count": len(transactions),
            }
        )

    changed_used_count = mark_wallet_addresses_used(
        wallet_name,
        used_addresses,
        wallet_file,
        network,
    )
    transactions = [
        summary
        for summary in (
            _summarize_transaction(tx, address_map, tip_height)
            for tx in tx_by_id.values()
        )
        if summary is not None
    ]
    transactions.sort(
        key=lambda tx: (
            tx["status"].get("block_height", 2**31),
            tx["txid"],
        ),
        reverse=True,
    )
    balance = _balance_from_utxos(all_utxos)
    wallet_cache = {
        "wallet_name": wallet_name,
        "synced_at": synced_at,
        "backend": {
            "type": "esplora",
            "base_url": getattr(backend, "base_url", ""),
        },
        "tip": {
            "height": tip_height,
            "hash": tip_hash,
        },
        "address_count": len(address_entries),
        "used_address_count": len(used_addresses),
        "changed_used_count": changed_used_count,
        "balance": balance,
        "addresses": address_caches,
        "utxos": all_utxos,
        "transactions": transactions,
        "transactions_complete": False,
    }

    with locked_cache_file(cache_path):
        cache = load_wallet_cache(cache_path)
        previous = cache.get("wallets", {}).get(wallet_name, {})
        if isinstance(previous, dict):
            reservations = previous.get("reserved_outpoints", {})
            pending = previous.get("pending_transactions", [])
            pending_spent = previous.get("pending_spent_outpoints", {})
            if isinstance(reservations, dict):
                wallet_cache["reserved_outpoints"] = reservations
            if isinstance(pending, list):
                confirmed_txids = {
                    tx["txid"] for tx in transactions if tx.get("confirmed") is True
                }
                wallet_cache["pending_transactions"] = [
                    item
                    for item in pending
                    if isinstance(item, dict)
                    and item.get("txid") not in confirmed_txids
                ]
                active_pending_txids = {
                    item.get("txid")
                    for item in wallet_cache["pending_transactions"]
                    if isinstance(item, dict)
                }
                if isinstance(pending_spent, dict):
                    wallet_cache["pending_spent_outpoints"] = {
                        outpoint: item
                        for outpoint, item in pending_spent.items()
                        if isinstance(item, dict)
                        and item.get("spending_txid") in active_pending_txids
                    }
        cache["version"] = CACHE_VERSION
        cache.setdefault("wallets", {})[wallet_name] = wallet_cache
        save_wallet_cache(cache, cache_path)

    return {
        **wallet_cache,
        "cache_file": str(cache_path),
    }


def get_cached_balance(
    wallet_name: str,
    cache_file: Path | None = None,
    network: str = NETWORK_MAINNET,
) -> dict:
    cache_path = cache_file or default_wallet_cache_file(network=network)
    wallet_cache = read_wallet_cache_entry(wallet_name, cache_path)
    balance = wallet_cache.get("balance")
    if not isinstance(balance, dict):
        raise WalletError("wallet cache balance is invalid")
    return {
        "wallet_name": wallet_name,
        "synced_at": wallet_cache.get("synced_at"),
        "tip": wallet_cache.get("tip", {}),
        "balance": balance,
        "cache_file": str(cache_path),
    }


def list_cached_unspent(
    wallet_name: str,
    cache_file: Path | None = None,
    network: str = NETWORK_MAINNET,
) -> dict:
    cache_path = cache_file or default_wallet_cache_file(network=network)
    wallet_cache = read_wallet_cache_entry(wallet_name, cache_path)
    utxos = wallet_cache.get("utxos", [])
    if not isinstance(utxos, list):
        raise WalletError("wallet cache UTXO list is invalid")
    return {
        "wallet_name": wallet_name,
        "synced_at": wallet_cache.get("synced_at"),
        "utxos": utxos,
        "cache_file": str(cache_path),
    }


def list_cached_transactions(
    wallet_name: str,
    cache_file: Path | None = None,
    network: str = NETWORK_MAINNET,
) -> dict:
    cache_path = cache_file or default_wallet_cache_file(network=network)
    wallet_cache = read_wallet_cache_entry(wallet_name, cache_path)
    transactions = wallet_cache.get("transactions", [])
    if not isinstance(transactions, list):
        raise WalletError("wallet cache transaction list is invalid")
    return {
        "wallet_name": wallet_name,
        "synced_at": wallet_cache.get("synced_at"),
        "transactions": transactions,
        "transactions_complete": wallet_cache.get("transactions_complete", False),
        "cache_file": str(cache_path),
    }
