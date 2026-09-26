"""Platform contract tests for deterministic wallet account types."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from btc.chainparams import NETWORK_MAINNET, NETWORK_TESTNET4
from tx.service import PaymentService
from wallet import get_wallet_address_book
from wallet.service import WalletService
from wallet.wallet import derive_wallet_address_candidate


class AccountBackend:
    """Small deterministic Esplora substitute keyed by exact addresses."""

    def __init__(self, network: str) -> None:
        self.network = network
        self.base_url = "https://example.invalid/api"
        self.history_addresses: set[str] = set()
        self.balances: dict[str, int] = {}
        self.address_queries: list[str] = []

    def verify_network(self) -> None:
        return None

    def get_tip_height(self) -> int:
        return 100

    def get_tip_hash(self) -> str:
        return "00" * 32

    def get_address(self, address: str) -> dict:
        self.address_queries.append(address)
        used = address in self.history_addresses or self.balances.get(address, 0) > 0
        return {
            "chain_stats": {
                "tx_count": int(used),
                "funded_txo_count": int(used),
                "funded_txo_sum": self.balances.get(address, 0),
                "spent_txo_count": 0,
                "spent_txo_sum": 0,
            },
            "mempool_stats": {"tx_count": 0},
        }

    def get_address_utxos(self, address: str) -> list[dict]:
        value = self.balances.get(address, 0)
        if value <= 0:
            return []
        return [
            {
                "txid": self._txid(address),
                "vout": 0,
                "value": value,
                "status": {"confirmed": True, "block_height": 90},
            }
        ]

    def get_all_address_transactions(self, address: str) -> list[dict]:
        value = self.balances.get(address, 0)
        if value <= 0:
            return []
        return [
            {
                "txid": self._txid(address),
                "vin": [],
                "vout": [{"scriptpubkey_address": address, "value": value}],
                "fee": 0,
                "status": {
                    "confirmed": True,
                    "block_height": 90,
                    "block_time": 1_700_000_000,
                },
            }
        ]

    @staticmethod
    def _txid(address: str) -> str:
        return hashlib.sha256(address.encode("ascii")).hexdigest()


def _service(tmp_path: Path, network: str = NETWORK_MAINNET):
    suffix = "_testnet4" if network == NETWORK_TESTNET4 else ""
    backend = AccountBackend(network)
    service = WalletService(
        tmp_path / f"wallets{suffix}.json",
        tmp_path / f"wallet_cache{suffix}.json",
        network,
        lambda _network: backend,
        gap_limit=2,
    )
    return service, backend


def _candidate(
    service: WalletService,
    name: str,
    index: int,
    *,
    address_type: str,
    change: bool = False,
) -> dict:
    return derive_wallet_address_candidate(
        name,
        index,
        wallet_file=service.wallet_file,
        change=change,
        address_type=address_type,
        network=service.network,
    )


@pytest.mark.parametrize(
    ("network", "prefixes", "account_path"),
    (
        (NETWORK_MAINNET, ("1",), "m/44'/0'/0'/0/0"),
        (NETWORK_TESTNET4, ("m", "n"), "m/44'/1'/0'/0/0"),
    ),
)
def test_create_wallet_can_select_bip44_on_both_networks(
    tmp_path: Path,
    network: str,
    prefixes: tuple[str, ...],
    account_path: str,
) -> None:
    service, _backend = _service(tmp_path, network)

    created = service.create_wallet("legacy", "password", address_type="p2pkh")
    address_book = get_wallet_address_book(
        "legacy",
        service.wallet_file,
        address_type="p2pkh",
        network=network,
    )

    assert created.state.receive_address.startswith(prefixes)
    assert address_book["addresses"][0]["path"] == account_path
    assert address_book["addresses"][0]["address_type"] == "P2PKH"


def test_enable_account_discovers_empty_bip44_once(tmp_path: Path) -> None:
    service, backend = _service(tmp_path)
    service.create_wallet("wallet", "password")
    receive_one = _candidate(
        service,
        "wallet",
        1,
        address_type="p2pkh",
    )
    change_zero = _candidate(
        service,
        "wallet",
        0,
        address_type="p2pkh",
        change=True,
    )
    backend.history_addresses.update(
        {receive_one["address"], change_zero["address"]}
    )

    enabled = service.enable_account("wallet", "P2PKH")
    query_count = len(backend.address_queries)
    repeated = service.enable_account("wallet", "p2pkh")

    assert enabled.address_type == "p2pkh"
    assert enabled.discovery is not None
    assert enabled.discovery.receive.scanned_count == 4
    assert enabled.discovery.receive.used_indexes == (1,)
    assert enabled.discovery.change.scanned_count == 3
    assert enabled.discovery.change.used_indexes == (0,)
    assert enabled.state.receive_address == _candidate(
        service,
        "wallet",
        2,
        address_type="p2pkh",
    )["address"]
    assert repeated.discovery is None
    assert repeated.address_type == "p2pkh"
    assert repeated.state.receive_address == enabled.state.receive_address
    assert len(backend.address_queries) == query_count


def test_import_and_lifecycle_methods_keep_the_selected_account_type(
    tmp_path: Path,
) -> None:
    source, _source_backend = _service(tmp_path / "source")
    mnemonic = source.create_wallet("source", "password").generated_mnemonic
    target, _target_backend = _service(tmp_path / "target")

    imported = target.import_wallet(
        "imported",
        "password",
        mnemonic,
        address_type="p2pkh",
    )
    renamed = target.rename_wallet(
        "imported",
        "renamed",
        "password",
        address_type="p2pkh",
    )
    reencrypted = target.change_password(
        "renamed",
        "password",
        "new-password",
        address_type="p2pkh",
    )

    assert imported.discovery is not None
    assert imported.state.receive_address.startswith("1")
    assert renamed.receive_address == imported.state.receive_address
    assert reencrypted.receive_address == imported.state.receive_address


def test_sync_aggregates_accounts_and_cache_identity_accepts_both(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    native = service.create_wallet("wallet", "password").state.receive_address
    legacy = service.enable_account("wallet", "p2pkh").state.receive_address
    backend.balances.update({native: 70_000, legacy: 30_000})

    legacy_state = service.sync_wallet("wallet", address_type="p2pkh")
    native_state = service.get_wallet_state("wallet", address_type="p2wpkh")

    assert legacy_state.receive_address == _candidate(
        service,
        "wallet",
        1,
        address_type="p2pkh",
    )["address"]
    assert native_state.receive_address == _candidate(
        service,
        "wallet",
        1,
        address_type="p2wpkh",
    )["address"]
    assert legacy_state.authoritative_balance_sats == 100_000
    assert native_state.authoritative_balance_sats == 100_000
    assert len(legacy_state.transactions) == 2
    assert {item.address_types for item in legacy_state.transactions} == {
        ("P2PKH",),
        ("P2WPKH",),
    }
    cache = json.loads(service.cache_file.read_text(encoding="utf-8"))
    cached_types = {
        entry["address_type"]
        for entry in cache["wallets"]["wallet"]["addresses"]
    }
    assert cached_types == {
        "P2PKH",
        "P2WPKH",
    }


@pytest.mark.parametrize("network", (NETWORK_MAINNET, NETWORK_TESTNET4))
def test_payment_uses_only_requested_legacy_utxos_for_amount_and_max(
    tmp_path: Path,
    network: str,
) -> None:
    service, backend = _service(tmp_path, network)
    native = service.create_wallet("wallet", "password").state.receive_address
    legacy = service.enable_account("wallet", "p2pkh").state.receive_address
    backend.balances.update({native: 90_000, legacy: 50_000})
    destination = _candidate(
        service,
        "wallet",
        1,
        address_type="p2wpkh",
    )["address"]
    payments = PaymentService(service)

    draft = payments.prepare(
        "wallet",
        destination,
        20_000,
        2,
        address_type="p2pkh",
    )
    signed = payments.sign(draft.draft_id, "password")

    assert draft.address_type == "p2pkh"
    assert signed.address_type == "p2pkh"
    assert signed.amount_sats == 20_000
    payments.cancel(signed.payment_id)

    maximum = payments.prepare(
        "wallet",
        destination,
        None,
        2,
        send_all=True,
        address_type="p2pkh",
    )

    assert maximum.address_type == "p2pkh"
    assert maximum.amount_sats + maximum.estimated_fee_sats == 50_000
    payments.cancel(maximum.draft_id)
