"""Contract tests for the wallet-wide incoming activity probe."""

from __future__ import annotations

import json
from pathlib import Path

from wallet import get_new_address, get_wallet_address_book
from wallet.service import WalletReceiveActivity, WalletService


def empty_stats() -> dict:
    return {
        "tx_count": 0,
        "funded_txo_count": 0,
        "spent_txo_count": 0,
        "funded_txo_sum": 0,
        "spent_txo_sum": 0,
    }


class ActivityBackend:
    network = "mainnet"
    base_url = "https://example.invalid/api"

    def __init__(self) -> None:
        self.payloads = {}
        self.address_requests = []
        self.utxo_requests = []
        self.transaction_requests = []

    def verify_network(self):
        return None

    def get_tip_height(self):
        return 100

    def get_tip_hash(self):
        return "ab" * 32

    def get_address(self, address):
        self.address_requests.append(address)
        return self.payloads.get(
            address,
            {
                "chain_stats": empty_stats(),
                "mempool_stats": empty_stats(),
            },
        )

    def get_address_utxos(self, address):
        self.utxo_requests.append(address)
        return []

    def get_all_address_transactions(self, address):
        self.transaction_requests.append(address)
        return []


def create_service(tmp_path: Path, backend: ActivityBackend) -> WalletService:
    return WalletService(
        tmp_path / "wallets.json",
        tmp_path / "wallet_cache.json",
        backend_factory=lambda _network: backend,
    )


def test_probe_aggregates_all_account_receive_addresses_and_writes_nothing(
    tmp_path: Path,
) -> None:
    backend = ActivityBackend()
    service = create_service(tmp_path, backend)
    service.create_wallet("wallet", "password")
    service.enable_account("wallet", "p2pkh")
    get_new_address(
        "wallet",
        wallet_file=service.wallet_file,
        change=True,
        network=service.network,
    )
    address_book = get_wallet_address_book(
        "wallet",
        wallet_file=service.wallet_file,
        network=service.network,
    )
    receive_addresses = [
        entry["address"]
        for entry in address_book["addresses"]
        if entry["branch"] == 0
    ]
    change_addresses = {
        entry["address"]
        for entry in address_book["addresses"]
        if entry["branch"] == 1
    }
    backend.payloads[receive_addresses[0]] = {
        "chain_stats": {
            "tx_count": 2,
            "funded_txo_count": 2,
            "spent_txo_count": 1,
            "funded_txo_sum": 50_000,
            "spent_txo_sum": 10_000,
        },
        "mempool_stats": empty_stats(),
    }
    backend.payloads[receive_addresses[1]] = {
        "chain_stats": empty_stats(),
        "mempool_stats": {
            "tx_count": 1,
            "funded_txo_count": 1,
            "spent_txo_count": 0,
            "funded_txo_sum": 7_000,
            "spent_txo_sum": 0,
        },
    }
    backend.address_requests.clear()
    wallet_before = service.wallet_file.read_bytes()
    cache_before = (
        service.cache_file.read_bytes() if service.cache_file.exists() else None
    )

    activity = service.receive_activity("wallet")

    assert activity == WalletReceiveActivity(
        address_count=2,
        chain_tx_count=2,
        chain_funded_txo_count=2,
        chain_spent_txo_count=1,
        chain_funded_sats=50_000,
        chain_spent_sats=10_000,
        mempool_tx_count=1,
        mempool_funded_txo_count=1,
        mempool_spent_txo_count=0,
        mempool_funded_sats=7_000,
        mempool_spent_sats=0,
    )
    assert set(backend.address_requests) == set(receive_addresses)
    assert not set(backend.address_requests) & change_addresses
    assert backend.utxo_requests == []
    assert backend.transaction_requests == []
    assert service.wallet_file.read_bytes() == wallet_before
    assert (
        service.cache_file.read_bytes() if service.cache_file.exists() else None
    ) == cache_before


def test_full_sync_exposes_probe_baseline_without_an_extra_network_pass(
    tmp_path: Path,
) -> None:
    backend = ActivityBackend()
    service = create_service(tmp_path, backend)
    created = service.create_wallet("wallet", "password")
    backend.payloads[created.state.receive_address] = {
        "chain_stats": {
            **empty_stats(),
            "tx_count": 1,
            "funded_txo_count": 1,
            "funded_txo_sum": 25_000,
        },
        "mempool_stats": empty_stats(),
    }
    backend.address_requests.clear()

    state = service.sync_wallet("wallet")

    assert len(backend.address_requests) == 1
    assert state.receive_activity is not None
    assert state.receive_activity.address_count == 2
    assert state.receive_activity.chain_tx_count == 1
    assert state.receive_activity.chain_funded_sats == 25_000
    cache = json.loads(service.cache_file.read_text(encoding="utf-8"))
    assert "receive_activity" not in cache["wallets"]["wallet"]
