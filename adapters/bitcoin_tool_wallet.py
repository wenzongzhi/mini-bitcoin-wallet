"""Adapter from the desktop application's ports to bitcoin-tool functions."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from btc.chainparams import NETWORK_MAINNET, get_chain_params
from network import EsploraBackend, EsploraError
from wallet import (
    WalletError,
    create_wallet as bitcoin_tool_create_wallet,
    get_cached_balance,
    get_new_address,
    get_wallet_address_book,
    list_cached_transactions,
    mnemonic_from_entropy_hex,
    sync_wallet,
)
from wallet.wallet_cache import read_wallet_cache_entry

from wallet_core.models import (
    BitcoinAmount,
    SendPreview,
    TransactionDirection,
    TransactionSummary,
    WalletCreation,
    WalletSnapshot,
    WalletSummary,
)
from wallet_core.ports import WalletService
from app_settings import ApplicationSettingsStore


class BitcoinToolWalletService(WalletService):
    """Persistent wallet service backed directly by the copied bitcoin-tool API.

    Wallet data is intentionally kept in explicit project paths so the desktop
    application and bitcoin-tool use the same JSON schema. Private mnemonic data
    is encrypted by bitcoin-tool before it reaches ``wallets.json``.
    """

    ADDRESS_TYPE = "p2wpkh"
    IMPORT_SCAN_SIZE = 20

    def __init__(
        self,
        wallet_file: Path,
        cache_file: Path,
        network: str = NETWORK_MAINNET,
        backend_factory: Callable[[str], EsploraBackend] | None = None,
        settings_file: Path | None = None,
    ):
        # Validate once at the composition boundary, including when no wallet
        # file exists yet.
        get_chain_params(network)
        self.wallet_file = Path(wallet_file)
        self.cache_file = Path(cache_file)
        self.network = network
        self.settings = ApplicationSettingsStore(
            settings_file or self.wallet_file.parent / "settings.json"
        )
        self.settings.ensure_exists()
        self._backend_factory = backend_factory or (
            lambda selected_network: EsploraBackend(network=selected_network)
        )
        self._wallet_name = self._resolve_active_wallet()

    def _read_wallet_records(self) -> dict:
        """Read public wallet metadata without decrypting secret material."""

        try:
            with self.wallet_file.open("r", encoding="utf-8") as file:
                wallets = json.load(file)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f'Cannot read wallet file "{self.wallet_file}": {exc}') from exc
        if not isinstance(wallets, dict):
            raise ValueError(f'Invalid wallet file "{self.wallet_file}".')
        return wallets

    def list_wallets(self) -> tuple[WalletSummary, ...]:
        summaries = []
        for name, wallet in self._read_wallet_records().items():
            if not isinstance(name, str) or not isinstance(wallet, dict):
                raise ValueError(f'Invalid wallet file "{self.wallet_file}".')
            if wallet.get("network") != self.network:
                continue
            encrypted = wallet.get("encrypted")
            if not isinstance(encrypted, bool):
                raise ValueError(f'Invalid wallet metadata for "{name}".')
            fingerprint = wallet.get("master_fingerprint")
            if not isinstance(fingerprint, str):
                fingerprint = None
            summaries.append(
                WalletSummary(
                    name=name,
                    network=self.network,
                    encrypted=encrypted,
                    master_fingerprint=fingerprint,
                )
            )
        return tuple(summaries)

    def _resolve_active_wallet(self) -> str | None:
        wallets = self.list_wallets()
        wallet_names = {wallet.name for wallet in wallets}
        configured_name = self.settings.active_wallet(self.network)
        selected_name = (
            configured_name
            if configured_name in wallet_names
            else wallets[0].name if wallets else None
        )
        if selected_name != configured_name:
            self.settings.set_active_wallet(self.network, selected_name)
        return selected_name

    def select_wallet(self, name: str) -> WalletSnapshot:
        if name not in {wallet.name for wallet in self.list_wallets()}:
            raise ValueError(f'Wallet "{name}" does not exist on {self.network}.')
        self._wallet_name = name
        self.settings.set_active_wallet(self.network, name)
        return self.snapshot()

    def snapshot(self) -> WalletSnapshot:
        if self._wallet_name is None:
            return WalletSnapshot(
                name="No Wallet",
                balance=BitcoinAmount(0),
                receive_address="",
                network=self.network,
                transactions=(),
                is_initialized=False,
            )
        try:
            address = self._current_receive_address()
            balance = self._cached_balance()
            transactions = self._cached_transactions()
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        return WalletSnapshot(
            name=self._wallet_name,
            balance=BitcoinAmount(balance),
            receive_address=address,
            network=self.network,
            transactions=transactions,
            is_initialized=True,
        )

    def _current_receive_address(self) -> str:
        address_book = get_wallet_address_book(
            self._wallet_name,
            wallet_file=self.wallet_file,
            address_type=self.ADDRESS_TYPE,
            network=self.network,
        )
        receive_entries = [
            entry for entry in address_book["addresses"] if entry.get("branch") == 0
        ]
        if not receive_entries:
            result = get_new_address(
                self._wallet_name,
                wallet_file=self.wallet_file,
                address_type=self.ADDRESS_TYPE,
                network=self.network,
            )
            return result["address"]

        # Imported wallets pre-issue a discovery pool. The receive address is
        # the first address after the highest used index, not the last address
        # in that pool. With no activity this deliberately resolves to index 0.
        target_index = self._next_receive_index_from_cache()
        matching_entry = next(
            (entry for entry in receive_entries if entry.get("index") == target_index),
            None,
        )
        if matching_entry is not None:
            return matching_entry["address"]
        while max(entry["index"] for entry in receive_entries) < target_index:
            result = get_new_address(
                self._wallet_name,
                wallet_file=self.wallet_file,
                address_type=self.ADDRESS_TYPE,
                network=self.network,
            )
            if result["index"] == target_index:
                return result["address"]
        raise WalletError("cannot resolve the next receive address")

    def _next_receive_index_from_cache(self) -> int:
        if not self.cache_file.exists():
            return 0
        try:
            wallet_cache = read_wallet_cache_entry(self._wallet_name, self.cache_file)
        except WalletError as exc:
            if "has no synced cache" in str(exc):
                return 0
            raise
        used_indexes = [
            entry.get("index")
            for entry in wallet_cache.get("addresses", [])
            if isinstance(entry, dict)
            and entry.get("branch") == 0
            and str(entry.get("address_type", "")).lower() == self.ADDRESS_TYPE
            and entry.get("used") is True
            and isinstance(entry.get("index"), int)
        ]
        return max(used_indexes, default=-1) + 1

    def _ensure_import_scan_pool(self) -> None:
        """Issue receive and change indexes 0..19 before discovery sync."""

        address_book = get_wallet_address_book(
            self._wallet_name,
            wallet_file=self.wallet_file,
            address_type=self.ADDRESS_TYPE,
            network=self.network,
        )
        counts = {
            branch: sum(
                1 for entry in address_book["addresses"] if entry.get("branch") == branch
            )
            for branch in (0, 1)
        }
        for branch in (0, 1):
            while counts[branch] < self.IMPORT_SCAN_SIZE:
                get_new_address(
                    self._wallet_name,
                    wallet_file=self.wallet_file,
                    change=branch == 1,
                    address_type=self.ADDRESS_TYPE,
                    network=self.network,
                )
                counts[branch] += 1

    def _scan_imported_wallet(self) -> None:
        self._ensure_import_scan_pool()
        sync_wallet(
            self._wallet_name,
            wallet_file=self.wallet_file,
            cache_file=self.cache_file,
            backend=self._backend_factory(self.network),
            include_transactions=True,
            network=self.network,
        )

    def _cached_balance(self) -> int:
        if not self.cache_file.exists():
            return 0
        try:
            result = get_cached_balance(
                self._wallet_name,
                cache_file=self.cache_file,
                network=self.network,
            )
        except WalletError as exc:
            if "has no synced cache" in str(exc):
                return 0
            raise
        total = result["balance"].get("total")
        if isinstance(total, bool) or not isinstance(total, int):
            raise WalletError("wallet cache total balance is invalid")
        return total

    def _cached_transactions(self) -> tuple[TransactionSummary, ...]:
        if not self.cache_file.exists():
            return ()
        try:
            result = list_cached_transactions(
                self._wallet_name,
                cache_file=self.cache_file,
                network=self.network,
            )
        except WalletError as exc:
            if "has no synced cache" in str(exc):
                return ()
            raise
        summaries = []
        for transaction in result["transactions"]:
            net_sats = transaction.get("net")
            txid = transaction.get("txid")
            if isinstance(net_sats, bool) or not isinstance(net_sats, int):
                continue
            if not isinstance(txid, str):
                continue
            direction = {
                "receive": TransactionDirection.INCOMING,
                "send": TransactionDirection.OUTGOING,
                "self": TransactionDirection.SELF,
            }.get(transaction.get("direction"))
            if direction is None:
                direction = (
                    TransactionDirection.INCOMING
                    if net_sats >= 0
                    else TransactionDirection.OUTGOING
                )
            received = self._non_negative_sats(transaction.get("received"))
            sent = self._non_negative_sats(transaction.get("sent"))
            fee = self._non_negative_sats(transaction.get("fee"))
            confirmations = transaction.get("confirmations", 0)
            if isinstance(confirmations, bool) or not isinstance(confirmations, int):
                confirmations = 0
            status_data = transaction.get("status", {})
            if not isinstance(status_data, dict):
                status_data = {}
            block_height = status_data.get("block_height")
            if isinstance(block_height, bool) or not isinstance(block_height, int):
                block_height = None
            block_timestamp = status_data.get("block_time")
            block_time = None
            if isinstance(block_timestamp, int) and not isinstance(block_timestamp, bool):
                try:
                    block_time = datetime.fromtimestamp(block_timestamp, timezone.utc)
                except (OverflowError, OSError, ValueError):
                    block_time = None
            summaries.append(
                TransactionSummary(
                    txid=txid,
                    amount=BitcoinAmount(net_sats),
                    direction=direction,
                    received=BitcoinAmount(received),
                    sent=BitcoinAmount(sent),
                    fee=BitcoinAmount(fee),
                    confirmed=transaction.get("confirmed") is True,
                    confirmations=max(confirmations, 0),
                    block_height=block_height,
                    block_time=block_time,
                    addresses=self._string_tuple(transaction.get("addresses")),
                    account_ids=self._string_tuple(transaction.get("account_ids")),
                    address_types=self._string_tuple(transaction.get("address_types")),
                    explorer_url=self._transaction_explorer_url(txid),
                )
            )
        return tuple(summaries)

    @staticmethod
    def _non_negative_sats(value) -> int:
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else 0
        )

    @staticmethod
    def _string_tuple(value) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        return tuple(item for item in value if isinstance(item, str))

    def _transaction_explorer_url(self, txid: str) -> str:
        prefix = (
            "https://mempool.space/testnet4/tx"
            if self.network == "testnet4"
            else "https://mempool.space/tx"
        )
        return f"{prefix}/{txid}"

    def create_wallet(
        self, name: str, password: str, mnemonic: str | None = None
    ) -> WalletCreation:
        imported = mnemonic is not None
        # Match bitcoin-tool's default strength: 256 bits / 24 BIP39 words.
        words = mnemonic or mnemonic_from_entropy_hex(secrets.token_hex(32))
        try:
            bitcoin_tool_create_wallet(
                name,
                password=password,
                mnemonic=words,
                wallet_file=self.wallet_file,
                network=self.network,
            )
            self._wallet_name = name
            self.settings.set_active_wallet(self.network, name)
            if imported:
                self._scan_imported_wallet()
        except (WalletError, EsploraError) as exc:
            if self._wallet_name == name and imported:
                raise ValueError(
                    "Wallet was imported, but its address scan could not complete: "
                    f"{exc}"
                ) from exc
            raise ValueError(str(exc)) from exc
        return WalletCreation(self.snapshot(), words, imported)

    def rename_wallet(self, name: str) -> WalletSnapshot:
        raise ValueError("Encrypted wallets cannot be renamed safely.")

    def preview_send(
        self,
        destination: str,
        amount: BitcoinAmount | None,
        fee_rate_sat_vb: int,
        *,
        send_all: bool = False,
    ) -> SendPreview:
        raise ValueError("Transaction funding will be connected in the next business feature.")
