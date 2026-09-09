"""Adapter from the desktop application's ports to bitcoin-tool functions."""

from __future__ import annotations

import json
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
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
from wallet.wallet_cache import (
    load_wallet_cache,
    locked_cache_file,
    read_wallet_cache_entry,
    save_wallet_cache,
)
from tx import (
    TransactionError,
    broadcast_signed_transaction,
    create_raw_transaction,
    fund_all_transaction,
    fund_transaction,
    sign_funded_transaction,
)

from wallet_core.models import (
    BitcoinAmount,
    BroadcastResult,
    SendPreview,
    TransactionDirection,
    TransactionStatus,
    TransactionSummary,
    WalletCreation,
    WalletSnapshot,
    WalletSummary,
    WithdrawalDraft,
    WithdrawalReview,
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
        self._operation_lock = RLock()
        self._funded_withdrawals: dict[str, dict] = {}
        self._prepared_withdrawals: dict[str, dict] = {}
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

    def synchronize_wallet(self, name: str) -> WalletSnapshot:
        """Synchronize one wallet without changing the user's active selection."""

        if name not in {wallet.name for wallet in self.list_wallets()}:
            raise ValueError(f'Wallet "{name}" does not exist on {self.network}.')
        with self._operation_lock:
            try:
                sync_wallet(
                    name,
                    wallet_file=self.wallet_file,
                    cache_file=self.cache_file,
                    backend=self._backend_factory(self.network),
                    include_transactions=True,
                    network=self.network,
                )
                return self._snapshot_for(name)
            except (WalletError, EsploraError) as exc:
                raise ValueError(str(exc)) from exc

    def _synchronize_spendable_state(self, wallet_name: str, backend) -> None:
        """Refresh UTXOs without discarding previously cached transaction rows.

        ``bitcoin-tool.sync_wallet(include_transactions=False)`` intentionally
        skips expensive history requests, but its resulting cache contains an
        empty transaction list.  The desktop withdrawal preflight needs fresh
        UTXOs while Home must keep rendering its last complete history.
        """

        cached_history = self._read_cached_history(wallet_name)
        sync_wallet(
            wallet_name,
            wallet_file=self.wallet_file,
            cache_file=self.cache_file,
            backend=backend,
            include_transactions=False,
            network=self.network,
        )
        if cached_history is not None:
            self._restore_cached_history(wallet_name, cached_history)

    def _read_cached_history(
        self, wallet_name: str
    ) -> tuple[list[dict], bool] | None:
        if not self.cache_file.exists():
            return None
        with locked_cache_file(self.cache_file):
            cache = load_wallet_cache(self.cache_file)
            wallet_cache = cache.get("wallets", {}).get(wallet_name)
            if not isinstance(wallet_cache, dict):
                return None
            transactions = wallet_cache.get("transactions", [])
            if not isinstance(transactions, list):
                raise WalletError("wallet cache transaction list is invalid")
            complete = wallet_cache.get("transactions_complete", False)
            return deepcopy(transactions), complete is True

    def _restore_cached_history(
        self,
        wallet_name: str,
        cached_history: tuple[list[dict], bool],
    ) -> None:
        transactions, complete = cached_history
        with locked_cache_file(self.cache_file):
            cache = load_wallet_cache(self.cache_file)
            wallet_cache = cache.get("wallets", {}).get(wallet_name)
            if not isinstance(wallet_cache, dict):
                raise WalletError("wallet cache is missing after synchronization")
            wallet_cache["transactions"] = transactions
            wallet_cache["transactions_complete"] = complete
            save_wallet_cache(cache, self.cache_file)

    def transaction_status(self, txid: str) -> TransactionStatus:
        """Fetch only one TXID status instead of rescanning every address."""

        backend = self._backend_factory(self.network)
        try:
            status = backend.get_transaction_status(txid)
        except (AttributeError, EsploraError) as exc:
            raise ValueError(str(exc)) from exc
        confirmed = status.get("confirmed")
        if not isinstance(confirmed, bool):
            raise ValueError("Backend returned an invalid transaction status.")
        block_height = status.get("block_height")
        if isinstance(block_height, bool) or not isinstance(block_height, int):
            block_height = None
        block_timestamp = status.get("block_time")
        block_time = None
        if isinstance(block_timestamp, int) and not isinstance(block_timestamp, bool):
            try:
                block_time = datetime.fromtimestamp(block_timestamp, timezone.utc)
            except (OverflowError, OSError, ValueError):
                block_time = None
        return TransactionStatus(
            txid=txid,
            confirmed=confirmed,
            block_height=block_height,
            block_time=block_time,
        )

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
        return self._snapshot_for(self._wallet_name)

    def _snapshot_for(self, wallet_name: str) -> WalletSnapshot:
        try:
            address = self._current_receive_address(wallet_name)
            balance = self._cached_balance(wallet_name)
            transactions = self._cached_transactions(wallet_name)
            synced_at, pending_txids = self._cached_sync_metadata(wallet_name)
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        return WalletSnapshot(
            name=wallet_name,
            balance=BitcoinAmount(balance),
            receive_address=address,
            network=self.network,
            transactions=transactions,
            is_initialized=True,
            synced_at=synced_at,
            pending_txids=pending_txids,
        )

    def _current_receive_address(self, wallet_name: str) -> str:
        address_book = get_wallet_address_book(
            wallet_name,
            wallet_file=self.wallet_file,
            address_type=self.ADDRESS_TYPE,
            network=self.network,
        )
        receive_entries = [
            entry for entry in address_book["addresses"] if entry.get("branch") == 0
        ]
        if not receive_entries:
            result = get_new_address(
                wallet_name,
                wallet_file=self.wallet_file,
                address_type=self.ADDRESS_TYPE,
                network=self.network,
            )
            return result["address"]

        # Imported wallets pre-issue a discovery pool. The receive address is
        # the first address after the highest used index, not the last address
        # in that pool. With no activity this deliberately resolves to index 0.
        target_index = self._next_receive_index_from_cache(wallet_name)
        matching_entry = next(
            (entry for entry in receive_entries if entry.get("index") == target_index),
            None,
        )
        if matching_entry is not None:
            return matching_entry["address"]
        while max(entry["index"] for entry in receive_entries) < target_index:
            result = get_new_address(
                wallet_name,
                wallet_file=self.wallet_file,
                address_type=self.ADDRESS_TYPE,
                network=self.network,
            )
            if result["index"] == target_index:
                return result["address"]
        raise WalletError("cannot resolve the next receive address")

    def _next_receive_index_from_cache(self, wallet_name: str) -> int:
        if not self.cache_file.exists():
            return 0
        try:
            wallet_cache = read_wallet_cache_entry(wallet_name, self.cache_file)
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

    def _cached_balance(self, wallet_name: str) -> int:
        if not self.cache_file.exists():
            return 0
        try:
            result = get_cached_balance(
                wallet_name,
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

    def _cached_transactions(self, wallet_name: str) -> tuple[TransactionSummary, ...]:
        if not self.cache_file.exists():
            return ()
        try:
            result = list_cached_transactions(
                wallet_name,
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

    def _cached_sync_metadata(
        self, wallet_name: str
    ) -> tuple[datetime | None, tuple[str, ...]]:
        if not self.cache_file.exists():
            return None, ()
        try:
            wallet_cache = read_wallet_cache_entry(wallet_name, self.cache_file)
        except WalletError as exc:
            if "has no synced cache" in str(exc):
                return None, ()
            raise
        synced_at = self._parse_cached_datetime(wallet_cache.get("synced_at"))
        pending = wallet_cache.get("pending_transactions", [])
        if not isinstance(pending, list):
            return synced_at, ()
        pending_txids = tuple(
            item["txid"]
            for item in pending
            if isinstance(item, dict)
            and isinstance(item.get("txid"), str)
            and len(item["txid"]) == 64
        )
        return synced_at, pending_txids

    @staticmethod
    def _parse_cached_datetime(value) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

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
        with self._operation_lock:
            return self._create_wallet(name, password, mnemonic)

    def _create_wallet(
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

    def prepare_withdrawal(
        self,
        destination: str,
        amount: BitcoinAmount | None,
        fee_rate_sat_vb: int,
        *,
        send_all: bool = False,
    ) -> WithdrawalDraft:
        with self._operation_lock:
            return self._prepare_withdrawal(
                destination,
                amount,
                fee_rate_sat_vb,
                send_all=send_all,
            )

    def _prepare_withdrawal(
        self,
        destination: str,
        amount: BitcoinAmount | None,
        fee_rate_sat_vb: int,
        *,
        send_all: bool = False,
    ) -> WithdrawalDraft:
        """Validate, synchronize, and fund before asking for wallet secrets."""

        if self._wallet_name is None:
            raise ValueError("Create or import a wallet before sending Bitcoin.")
        wallet_name = self._wallet_name
        backend = self._backend_factory(self.network)
        funded = None
        try:
            # Validate the network/address locally before making network calls.
            create_raw_transaction(
                [],
                [{"address": destination, "amount_sats": 1}],
                self.network,
            )
            self._synchronize_spendable_state(wallet_name, backend)
            if send_all:
                funded = fund_all_transaction(
                    destination,
                    wallet_name,
                    self.cache_file,
                    self.network,
                    self.ADDRESS_TYPE,
                    fee_rate_sat_vb,
                    utxo_source="fresh backend synchronization",
                )
            else:
                if amount is None:
                    raise ValueError("Enter an amount.")
                template = create_raw_transaction(
                    [],
                    [{"address": destination, "amount_sats": amount.sats}],
                    self.network,
                )
                funded = fund_transaction(
                    template,
                    wallet_name,
                    self.wallet_file,
                    self.cache_file,
                    self.network,
                    self.ADDRESS_TYPE,
                    fee_rate_sat_vb,
                    utxo_source="fresh backend synchronization",
                )
        except (TransactionError, WalletError, EsploraError) as exc:
            if funded is not None:
                self._release_withdrawal_reservations(
                    wallet_name, funded.get("draft_id")
                )
            raise ValueError(str(exc)) from exc

        draft_id = funded["draft_id"]
        destination_sats = sum(
            output["value"]
            for output in funded["outputs"]
            if output.get("is_change") is False
        )
        self._funded_withdrawals[draft_id] = {
            "document": funded,
            "destination": destination,
            "fee_rate_sat_vb": fee_rate_sat_vb,
            "send_all": send_all,
        }
        return WithdrawalDraft(
            draft_id=draft_id,
            wallet_name=wallet_name,
            network=self.network,
            destination=destination,
            amount=BitcoinAmount(destination_sats),
            estimated_fee=BitcoinAmount(funded["estimated_fee_sats"]),
            fee_rate_sat_vb=fee_rate_sat_vb,
            send_all=send_all,
        )

    def sign_withdrawal(
        self, draft_id: str, password: str | None
    ) -> WithdrawalReview:
        with self._operation_lock:
            return self._sign_withdrawal(draft_id, password)

    def _sign_withdrawal(
        self, draft_id: str, password: str | None
    ) -> WithdrawalReview:
        prepared = self._funded_withdrawals.get(draft_id)
        if prepared is None:
            raise ValueError("Withdrawal draft has expired or does not exist.")
        funded = prepared["document"]
        try:
            signed = sign_funded_transaction(
                funded,
                funded["wallet_name"],
                password,
                self.wallet_file,
                self.cache_file,
                self.network,
            )
        except (TransactionError, WalletError) as exc:
            self._funded_withdrawals.pop(draft_id, None)
            self._release_withdrawal_reservations(
                funded["wallet_name"], draft_id
            )
            raise ValueError(str(exc)) from exc

        self._funded_withdrawals.pop(draft_id, None)
        self._prepared_withdrawals[draft_id] = signed
        destination_sats = sum(
            output["value"]
            for output in signed["outputs"]
            if output.get("is_change") is False
        )
        return WithdrawalReview(
            review_id=draft_id,
            wallet_name=signed["wallet_name"],
            network=self.network,
            txid=signed["txid"],
            destination=prepared["destination"],
            amount=BitcoinAmount(destination_sats),
            fee=BitcoinAmount(signed["fee_sats"]),
            fee_rate_sat_vb=prepared["fee_rate_sat_vb"],
            send_all=prepared["send_all"],
        )

    def broadcast_withdrawal(self, review_id: str) -> BroadcastResult:
        with self._operation_lock:
            return self._broadcast_withdrawal(review_id)

    def _broadcast_withdrawal(self, review_id: str) -> BroadcastResult:
        signed = self._prepared_withdrawals.get(review_id)
        if signed is None:
            raise ValueError("Withdrawal review has expired or does not exist.")
        backend = self._backend_factory(self.network)
        try:
            result = broadcast_signed_transaction(
                signed,
                self.network,
                backend,
                cache_file=self.cache_file,
            )
        except (TransactionError, WalletError, EsploraError) as exc:
            raise ValueError(str(exc)) from exc

        cache_warning = result.get("cache_warning")
        if cache_warning is None:
            try:
                self._record_pending_transaction_summary(signed)
            except WalletError as exc:
                cache_warning = str(exc)
        self._prepared_withdrawals.pop(review_id, None)
        return BroadcastResult(
            txid=result["txid"],
            explorer_url=self._transaction_explorer_url(result["txid"]),
            cache_warning=cache_warning,
        )

    def _record_pending_transaction_summary(self, signed: dict) -> None:
        """Persist an optimistic Home row immediately after accepted broadcast."""

        wallet_name = signed["wallet_name"]
        address_book = get_wallet_address_book(
            wallet_name,
            wallet_file=self.wallet_file,
            network=self.network,
        )
        owned_addresses = {
            item["address"]: item for item in address_book["addresses"]
        }
        inputs = signed["inputs"]
        outputs = signed["outputs"]
        sent = sum(item["value"] for item in inputs)
        owned_outputs = [
            item for item in outputs if item.get("address") in owned_addresses
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
            owned_addresses[address]
            for address in involved_addresses
            if address in owned_addresses
        ]
        summary = {
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
            "account_ids": sorted(
                {item["account_id"] for item in involved_entries}
            ),
            "address_types": sorted(
                {item["address_type"] for item in involved_entries}
            ),
        }
        with locked_cache_file(self.cache_file):
            cache = load_wallet_cache(self.cache_file)
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
                    or transaction.get("txid") != signed["txid"]
                ),
            ]
            save_wallet_cache(cache, self.cache_file)

    def cancel_withdrawal(self, review_id: str) -> None:
        funded = self._funded_withdrawals.pop(review_id, None)
        signed = self._prepared_withdrawals.pop(review_id, None)
        document = signed or (funded["document"] if funded is not None else None)
        if document is not None:
            self._release_withdrawal_reservations(
                document["wallet_name"], document["draft_id"]
            )

    def _release_withdrawal_reservations(
        self, wallet_name: str, draft_id: str | None
    ) -> None:
        """Release the copied workflow's cache reservation after cancel/failure."""

        if not draft_id or not self.cache_file.exists():
            return
        try:
            with locked_cache_file(self.cache_file):
                cache = load_wallet_cache(self.cache_file)
                wallet_cache = cache.get("wallets", {}).get(wallet_name)
                if not isinstance(wallet_cache, dict):
                    return
                reservations = wallet_cache.get("reserved_outpoints", {})
                if not isinstance(reservations, dict):
                    return
                remaining = {
                    outpoint: reservation
                    for outpoint, reservation in reservations.items()
                    if not isinstance(reservation, dict)
                    or reservation.get("draft_id") != draft_id
                }
                if remaining != reservations:
                    wallet_cache["reserved_outpoints"] = remaining
                    save_wallet_cache(cache, self.cache_file)
        except WalletError:
            # The original transaction error remains more useful to the caller.
            return
