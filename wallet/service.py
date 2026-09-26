"""High-level wallet platform service.

Consumers use this module instead of parsing wallet/cache JSON or orchestrating
address discovery.  Low-level modules remain reusable implementation details.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import secrets
from threading import RLock
from typing import Callable
import warnings

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from btc.chainparams import NETWORK_MAINNET, get_chain_params
from network import EsploraBackend, EsploraError

from .transaction_accounting import (
    AccountedTransaction,
    calculate_wallet_balance,
    transaction_from_cache,
)
from .wallet import (
    BTC_CHANGE_BRANCH,
    BTC_RECEIVE_BRANCH,
    DEFAULT_ADDRESS_TYPE,
    PBKDF2_ITERATIONS,
    WalletError,
    _derive_key,
    _load_wallets,
    _locked_wallet_file,
    _normalize_address_type,
    _read_mnemonic,
    _require_current_wallet,
    _save_wallets,
    _validate_mnemonic,
    _validate_wallet_name,
    create_wallet as create_wallet_record,
    commit_discovered_addresses,
    derive_wallet_address_candidate,
    get_mnemonic as read_wallet_mnemonic,
    get_new_address,
    get_wallet_address_book,
    mnemonic_from_entropy_hex,
    normalize_mnemonic,
)
from .wallet_cache import (
    load_wallet_cache,
    locked_cache_file,
    save_wallet_cache,
)
from .wallet_sync import sync_wallet as synchronize_wallet_record


GAP_LIMIT = 20
DEFAULT_DISCOVERY_MAX_ADDRESSES = 10_000


class WalletImportError(WalletError):
    """A wallet import failed and the new wallet was rolled back."""


class WalletImportCleanupError(WalletImportError):
    """A wallet import failed and its rollback may be incomplete."""


class WalletCacheWarning(RuntimeWarning):
    """An authoritative wallet mutation succeeded but cache maintenance failed."""


@dataclass(frozen=True, slots=True)
class BranchDiscovery:
    branch: int
    scanned_count: int
    used_indexes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class WalletDiscovery:
    receive: BranchDiscovery
    change: BranchDiscovery


@dataclass(frozen=True, slots=True)
class WalletMetadata:
    name: str
    network: str
    encrypted: bool
    master_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class WalletState:
    metadata: WalletMetadata
    authoritative_balance_sats: int
    confirmed_balance_sats: int
    unconfirmed_chain_balance_sats: int
    pending_delta_sats: int
    effective_balance_sats: int
    available_balance_sats: int
    receive_address: str
    transactions: tuple[AccountedTransaction, ...]
    synced_at: datetime | None
    pending_txids: tuple[str, ...]

    @property
    def balance_sats(self) -> int:
        """Compatibility alias for clients that previously displayed total."""

        return self.effective_balance_sats


@dataclass(frozen=True, slots=True)
class WalletCreationResult:
    state: WalletState
    generated_mnemonic: str | None
    discovery: WalletDiscovery | None = None


@dataclass(frozen=True, slots=True)
class WalletAccountEnablement:
    """Result of making one deterministic wallet account available to a client."""

    address_type: str
    state: WalletState
    discovery: WalletDiscovery | None = None


@dataclass(frozen=True, slots=True)
class TransactionStatus:
    txid: str
    confirmed: bool
    block_height: int | None = None
    block_time: datetime | None = None


class WalletService:
    """Own wallet storage, discovery, synchronization, and lifecycle operations."""

    def __init__(
        self,
        wallet_file: Path,
        cache_file: Path,
        network: str = NETWORK_MAINNET,
        backend_factory: Callable[[str], EsploraBackend] | None = None,
        *,
        address_type: str = DEFAULT_ADDRESS_TYPE,
        gap_limit: int = GAP_LIMIT,
        discovery_max_addresses: int = DEFAULT_DISCOVERY_MAX_ADDRESSES,
        import_scan_size: int | None = None,
    ) -> None:
        get_chain_params(network)
        if import_scan_size is not None:
            gap_limit = import_scan_size
        if isinstance(gap_limit, bool) or not isinstance(gap_limit, int) or gap_limit <= 0:
            raise WalletError("wallet discovery gap limit must be positive")
        if (
            isinstance(discovery_max_addresses, bool)
            or not isinstance(discovery_max_addresses, int)
            or discovery_max_addresses < gap_limit
        ):
            raise WalletError(
                "wallet discovery safety limit must be an integer greater than "
                "or equal to the gap limit"
            )
        self.wallet_file = Path(wallet_file)
        self.cache_file = Path(cache_file)
        self.network = network
        if not isinstance(address_type, str):
            raise WalletError("wallet account address type must be a string")
        self.address_type = _normalize_address_type(address_type)
        self.gap_limit = gap_limit
        self.import_scan_size = gap_limit
        self.discovery_max_addresses = discovery_max_addresses
        self.backend_factory = backend_factory or (
            lambda selected_network: EsploraBackend(network=selected_network)
        )
        self.operation_lock = RLock()

    def list_wallets(self) -> tuple[WalletMetadata, ...]:
        """Return validated non-secret metadata for this service's network."""

        with _locked_wallet_file(self.wallet_file):
            records = _load_wallets(self.wallet_file)
        wallets = []
        for name, wallet in records.items():
            if not isinstance(name, str) or not isinstance(wallet, dict):
                raise WalletError(f'invalid wallet file "{self.wallet_file}"')
            if wallet.get("network") != self.network:
                continue
            _require_current_wallet(wallet, self.network)
            encrypted = wallet.get("encrypted")
            if not isinstance(encrypted, bool):
                raise WalletError(f'invalid wallet metadata for "{name}"')
            fingerprint = wallet.get("master_fingerprint")
            wallets.append(
                WalletMetadata(
                    name=name,
                    network=self.network,
                    encrypted=encrypted,
                    master_fingerprint=(
                        fingerprint if isinstance(fingerprint, str) else None
                    ),
                )
            )
        return tuple(wallets)

    def create_wallet(
        self,
        name: str,
        password: str | None,
        *,
        address_type: str | None = None,
    ) -> WalletCreationResult:
        """Create a generated wallet and issue its first receive address."""

        selected_type = self.resolve_address_type(address_type)
        with self.operation_lock:
            mnemonic = mnemonic_from_entropy_hex(secrets.token_hex(32))
            create_wallet_record(
                name,
                password=password,
                mnemonic=mnemonic,
                wallet_file=self.wallet_file,
                network=self.network,
            )
            cache_usable = self._maintain_cache(
                lambda: self._remove_cache_entry(name),
                f'created wallet "{name}" but could not clear stale cache data',
            )
            get_new_address(
                name,
                wallet_file=self.wallet_file,
                address_type=selected_type,
                network=self.network,
            )
            state = self._state_after_cache_maintenance(
                name,
                cache_usable,
                f'wallet "{name}" was created but its cache state is invalid',
                address_type=selected_type,
            )
            return WalletCreationResult(state, mnemonic)

    def import_wallet(
        self,
        name: str,
        password: str | None,
        mnemonic: str,
        *,
        address_type: str | None = None,
    ) -> WalletCreationResult:
        """Import, discover, and sync with rollback on caught failures."""

        selected_type = self.resolve_address_type(address_type)
        normalized_mnemonic = normalize_mnemonic(mnemonic)
        _validate_mnemonic(normalized_mnemonic)
        with self.operation_lock:
            record_created = False
            create_wallet_record(
                name,
                password=password,
                mnemonic=normalized_mnemonic,
                wallet_file=self.wallet_file,
                network=self.network,
            )
            record_created = True
            try:
                self._remove_cache_entry(name)
                discovery = self._discover_imported_wallet(name, selected_type)
                state = self.sync_wallet(name, address_type=selected_type)
            except Exception as import_error:
                safe_error = _redact_recovery_words(
                    str(import_error),
                    mnemonic,
                    normalized_mnemonic,
                )
                if record_created:
                    cleanup_failures = self._rollback_import(name)
                    if cleanup_failures:
                        failed_targets = " and ".join(cleanup_failures)
                        raise WalletImportCleanupError(
                            f'wallet "{name}" import failed and cleanup may be '
                            f'incomplete for {failed_targets}'
                        ) from None
                raise WalletImportError(
                    f'wallet "{name}" import failed and was rolled back: '
                    f'{safe_error}'
                ) from None
            return WalletCreationResult(state, None, discovery)

    def _discover_imported_wallet(
        self,
        name: str,
        address_type: str | None = None,
    ) -> WalletDiscovery:
        """Import-specific hook around deterministic account discovery."""

        return self._discover_account_addresses(name, address_type)

    def _discover_account_addresses(
        self,
        name: str,
        address_type: str | None = None,
    ) -> WalletDiscovery:
        """Discover account history cheaply, then persist both branches once."""

        selected_type = self.resolve_address_type(address_type)
        backend = self.backend_factory(self.network)
        if getattr(backend, "network", None) != self.network:
            raise WalletError("wallet discovery backend network does not match wallet")
        receive, receive_usage = self._discover_branch(
            name,
            BTC_RECEIVE_BRANCH,
            backend,
            selected_type,
        )
        change, change_usage = self._discover_branch(
            name,
            BTC_CHANGE_BRANCH,
            backend,
            selected_type,
        )
        commit_discovered_addresses(
            name,
            receive_usage,
            change_usage,
            wallet_file=self.wallet_file,
            address_type=selected_type,
            network=self.network,
        )
        return WalletDiscovery(receive, change)

    def _discover_branch(
        self,
        name: str,
        branch: int,
        backend,
        address_type: str | None = None,
    ) -> tuple[BranchDiscovery, dict[int, bool]]:
        """Find the last-used index using address statistics only."""

        selected_type = self.resolve_address_type(address_type)
        usage: dict[int, bool] = {}
        consecutive_unused = 0
        index = 0
        while consecutive_unused < self.gap_limit:
            if index >= self.discovery_max_addresses:
                raise WalletError(
                    f"wallet discovery incomplete for branch {branch}: safety "
                    f"limit {self.discovery_max_addresses} reached before a gap "
                    f"of {self.gap_limit}"
                )
            candidate = derive_wallet_address_candidate(
                name,
                index,
                wallet_file=self.wallet_file,
                change=branch == BTC_CHANGE_BRANCH,
                address_type=selected_type,
                network=self.network,
            )
            try:
                address_data = backend.get_address(candidate["address"])
            except Exception as exc:
                raise WalletError(
                    f"wallet discovery failed at branch {branch} index {index}: {exc}"
                ) from exc
            used = _address_stats_have_history(address_data)
            usage[index] = used
            consecutive_unused = 0 if used else consecutive_unused + 1
            index += 1

        used_indexes = tuple(position for position, used in usage.items() if used)
        last_used = max(used_indexes, default=-1)
        committed_usage = {
            position: usage[position] for position in range(last_used + 1)
        }
        return BranchDiscovery(branch, index, used_indexes), committed_usage

    def enable_account(
        self,
        name: str,
        address_type: str,
    ) -> WalletAccountEnablement:
        """Discover an empty account once and return its cached wallet state.

        Account activation is a product preference, so the Platform does not
        persist it.  A non-empty address book proves that the account was
        already enabled and makes this operation idempotent.  Full wallet
        synchronization remains a separate operation.
        """

        selected_type = self.resolve_address_type(address_type)
        self._metadata(name)
        with self.operation_lock:
            address_book = get_wallet_address_book(
                name,
                wallet_file=self.wallet_file,
                address_type=selected_type,
                network=self.network,
            )
            discovery = None
            if not address_book["addresses"]:
                discovery = self._discover_account_addresses(name, selected_type)
            state = self.get_wallet_state(name, address_type=selected_type)
        return WalletAccountEnablement(selected_type, state, discovery)

    def get_wallet_state(
        self,
        name: str,
        *,
        address_type: str | None = None,
    ) -> WalletState:
        """Return aggregate cached accounting plus one account's receive address."""

        selected_type = self.resolve_address_type(address_type)
        metadata = self._metadata(name)
        receive_address = self.get_receive_address(
            name,
            address_type=selected_type,
        )
        if not self.cache_file.exists():
            return self._empty_wallet_state(metadata, receive_address)
        with locked_cache_file(self.cache_file):
            cache = load_wallet_cache(self.cache_file)
            wallet_cache = cache.get("wallets", {}).get(name)
        if not isinstance(wallet_cache, dict) or not self._cache_entry_matches_wallet(
            name,
            wallet_cache,
        ):
            return self._empty_wallet_state(metadata, receive_address)
        balance = calculate_wallet_balance(wallet_cache)
        transaction_values = wallet_cache.get("transactions", [])
        if not isinstance(transaction_values, list):
            raise WalletError("wallet cache transaction list is invalid")
        transactions = tuple(
            transaction
            for transaction in (
                transaction_from_cache(value) for value in transaction_values
            )
            if transaction is not None
        )
        pending = wallet_cache.get("pending_transactions", [])
        pending_txids = (
            tuple(
                item["txid"]
                for item in pending
                if isinstance(item, dict)
                and isinstance(item.get("txid"), str)
                and len(item["txid"]) == 64
            )
            if isinstance(pending, list)
            else ()
        )
        synced_at = _parse_timestamp(wallet_cache.get("synced_at"))
        return WalletState(
            metadata,
            balance.authoritative_sats,
            balance.confirmed_sats,
            balance.unconfirmed_chain_sats,
            balance.pending_delta_sats,
            balance.effective_sats,
            balance.available_sats,
            receive_address,
            transactions,
            synced_at,
            pending_txids,
        )

    def get_receive_address(
        self,
        name: str,
        *,
        address_type: str | None = None,
    ) -> str:
        """Return index zero or the first receive address after the last used one."""

        selected_type = self.resolve_address_type(address_type)
        self._metadata(name)
        address_book = get_wallet_address_book(
            name,
            wallet_file=self.wallet_file,
            address_type=selected_type,
            network=self.network,
        )
        receive_entries = [
            entry for entry in address_book["addresses"] if entry.get("branch") == 0
        ]
        if not receive_entries:
            return get_new_address(
                name,
                wallet_file=self.wallet_file,
                address_type=selected_type,
                network=self.network,
            )["address"]
        target_index = self._next_receive_index(receive_entries)
        matching = next(
            (entry for entry in receive_entries if entry.get("index") == target_index),
            None,
        )
        if matching is not None:
            return matching["address"]
        highest_index = max(entry["index"] for entry in receive_entries)
        while highest_index < target_index:
            created = get_new_address(
                name,
                wallet_file=self.wallet_file,
                address_type=selected_type,
                network=self.network,
            )
            highest_index = created["index"]
            if highest_index == target_index:
                return created["address"]
        raise WalletError("cannot resolve the next receive address")

    def sync_wallet(
        self,
        name: str,
        *,
        include_transactions: bool = True,
        address_type: str | None = None,
    ) -> WalletState:
        """Synchronize all issued accounts and return one account's receive view.

        ``address_type`` selects only the receive address in the returned state;
        balances, transactions, and UTXOs remain aggregated across every issued
        account. UTXO-only scans preserve the previously cached history.
        """

        selected_type = self.resolve_address_type(address_type)
        self._metadata(name)
        with self.operation_lock:
            self._discard_stale_cache_entry(name)
            cached_history = (
                None if include_transactions else self._read_cached_history(name)
            )
            synchronize_wallet_record(
                name,
                wallet_file=self.wallet_file,
                cache_file=self.cache_file,
                backend=self.backend_factory(self.network),
                include_transactions=include_transactions,
                network=self.network,
            )
            if cached_history is not None:
                self._restore_cached_history(name, cached_history)
            return self.get_wallet_state(name, address_type=selected_type)

    def get_mnemonic(self, name: str, password: str | None) -> str:
        return read_wallet_mnemonic(
            name,
            password=password,
            wallet_file=self.wallet_file,
            network=self.network,
        )["mnemonic"]

    def rename_wallet(
        self,
        name: str,
        new_name: str,
        password: str | None,
        *,
        address_type: str | None = None,
    ) -> WalletState:
        """Rename authoritative wallet data, then best-effort migrate its cache."""

        selected_type = self.resolve_address_type(address_type)
        _validate_wallet_name(new_name)
        if new_name == name:
            return self.get_wallet_state(name, address_type=selected_type)
        with self.operation_lock:
            with _locked_wallet_file(self.wallet_file):
                wallets = _load_wallets(self.wallet_file)
                wallet = wallets.get(name)
                if not isinstance(wallet, dict):
                    raise WalletError(f'wallet "{name}" does not exist')
                if new_name in wallets:
                    raise WalletError(f'wallet "{new_name}" already exists')
                _require_current_wallet(wallet, self.network)
                renamed = deepcopy(wallet)
                if wallet.get("encrypted") is True:
                    mnemonic = _read_mnemonic(name, wallet, password, self.network)
                    renamed["encryption"] = _encrypt_mnemonic(
                        new_name, mnemonic, password
                    )
                wallets.pop(name)
                wallets[new_name] = renamed
                _save_wallets(wallets, self.wallet_file)
            cache_usable = self._maintain_cache(
                lambda: self._rename_cache_entry(name, new_name),
                f'wallet "{name}" was renamed to "{new_name}" but its cache '
                "could not be migrated",
            )
        return self._state_after_cache_maintenance(
            new_name,
            cache_usable,
            f'wallet "{new_name}" was renamed but its cache state is invalid',
            address_type=selected_type,
        )

    def change_password(
        self,
        name: str,
        current_password: str | None,
        new_password: str | None,
        *,
        address_type: str | None = None,
    ) -> WalletState:
        """Re-encrypt the mnemonic while preserving all deterministic state."""

        selected_type = self.resolve_address_type(address_type)
        if new_password == "":
            raise WalletError("new password must not be empty")
        with self.operation_lock, _locked_wallet_file(self.wallet_file):
            wallets = _load_wallets(self.wallet_file)
            wallet = wallets.get(name)
            if not isinstance(wallet, dict):
                raise WalletError(f'wallet "{name}" does not exist')
            mnemonic = _read_mnemonic(name, wallet, current_password, self.network)
            _validate_mnemonic(mnemonic)
            updated = deepcopy(wallet)
            if new_password is None:
                updated["encrypted"] = False
                updated["mnemonic"] = mnemonic
                updated.pop("encryption", None)
            else:
                updated["encrypted"] = True
                updated["encryption"] = _encrypt_mnemonic(
                    name, mnemonic, new_password
                )
                updated.pop("mnemonic", None)
            wallets[name] = updated
            _save_wallets(wallets, self.wallet_file)
        return self.get_wallet_state(name, address_type=selected_type)

    def remove_wallet(self, name: str, password: str | None) -> None:
        """Remove authoritative wallet data, then best-effort discard its cache."""

        with self.operation_lock:
            with _locked_wallet_file(self.wallet_file):
                wallets = _load_wallets(self.wallet_file)
                wallet = wallets.get(name)
                if not isinstance(wallet, dict):
                    raise WalletError(f'wallet "{name}" does not exist')
                _require_current_wallet(wallet, self.network)
                if wallet.get("encrypted") is True:
                    _read_mnemonic(name, wallet, password, self.network)
                wallets.pop(name)
                _save_wallets(wallets, self.wallet_file)
            self._maintain_cache(
                lambda: self._remove_cache_entry(name),
                f'wallet "{name}" was removed but its cache could not be cleaned',
            )

    def transaction_status(self, txid: str) -> TransactionStatus:
        """Query one transaction without scanning wallet addresses."""

        try:
            status = self.backend_factory(self.network).get_transaction_status(txid)
        except (AttributeError, EsploraError) as exc:
            raise WalletError(str(exc)) from exc
        confirmed = status.get("confirmed")
        if not isinstance(confirmed, bool):
            raise WalletError("backend returned an invalid transaction status")
        block_timestamp = status.get("block_time")
        block_time = None
        if isinstance(block_timestamp, int) and not isinstance(block_timestamp, bool):
            try:
                block_time = datetime.fromtimestamp(block_timestamp, timezone.utc)
            except (OverflowError, OSError, ValueError):
                block_time = None
        block_height = status.get("block_height")
        if isinstance(block_height, bool) or not isinstance(block_height, int):
            block_height = None
        return TransactionStatus(txid, confirmed, block_height, block_time)

    def resolve_address_type(self, address_type: str | None = None) -> str:
        """Return one canonical account type without mutating service context."""

        selected_type = self.address_type if address_type is None else address_type
        if not isinstance(selected_type, str):
            raise WalletError("wallet account address type must be a string")
        return _normalize_address_type(selected_type)

    def _metadata(self, name: str) -> WalletMetadata:
        match = next((wallet for wallet in self.list_wallets() if wallet.name == name), None)
        if match is None:
            raise WalletError(f'wallet "{name}" does not exist on {self.network}')
        return match

    @staticmethod
    def _next_receive_index(receive_entries: list[dict]) -> int:
        """Derive the next receive index from authoritative address metadata."""

        used_indexes = [
            entry.get("index")
            for entry in receive_entries
            if isinstance(entry, dict)
            and entry.get("used") is True
            and isinstance(entry.get("index"), int)
        ]
        return max(used_indexes, default=-1) + 1

    def _read_cached_history(self, name: str) -> tuple[list[dict], bool] | None:
        if not self.cache_file.exists():
            return None
        with locked_cache_file(self.cache_file):
            cache = load_wallet_cache(self.cache_file)
            wallet_cache = cache.get("wallets", {}).get(name)
            if not isinstance(wallet_cache, dict):
                return None
            transactions = wallet_cache.get("transactions", [])
            if not isinstance(transactions, list):
                raise WalletError("wallet cache transaction list is invalid")
            return (
                deepcopy(transactions),
                wallet_cache.get("transactions_complete") is True,
            )

    def _restore_cached_history(
        self,
        name: str,
        history: tuple[list[dict], bool],
    ) -> None:
        transactions, complete = history
        with locked_cache_file(self.cache_file):
            cache = load_wallet_cache(self.cache_file)
            wallet_cache = cache.get("wallets", {}).get(name)
            if not isinstance(wallet_cache, dict):
                raise WalletError("wallet cache is missing after synchronization")
            wallet_cache["transactions"] = transactions
            wallet_cache["transactions_complete"] = complete
            save_wallet_cache(cache, self.cache_file)

    def _rename_cache_entry(self, name: str, new_name: str) -> None:
        if not self.cache_file.exists():
            return
        with locked_cache_file(self.cache_file):
            cache = load_wallet_cache(self.cache_file)
            wallets = cache["wallets"]
            entry = wallets.pop(name, None)
            stale_entry = wallets.pop(new_name, None)
            if isinstance(entry, dict):
                entry["wallet_name"] = new_name
                wallets[new_name] = entry
            if isinstance(entry, dict) or stale_entry is not None:
                save_wallet_cache(cache, self.cache_file)

    def _remove_cache_entry(self, name: str) -> None:
        if not self.cache_file.exists():
            return
        with locked_cache_file(self.cache_file):
            cache = load_wallet_cache(self.cache_file)
            if cache["wallets"].pop(name, None) is not None:
                save_wallet_cache(cache, self.cache_file)

    def _discard_stale_cache_entry(self, name: str) -> None:
        """Drop same-name cache state that belongs to another wallet identity."""

        if not self.cache_file.exists():
            return
        authoritative = self._authoritative_address_identity(name)
        with locked_cache_file(self.cache_file):
            cache = load_wallet_cache(self.cache_file)
            wallet_cache = cache.get("wallets", {}).get(name)
            if not isinstance(wallet_cache, dict):
                return
            if self._cache_entry_matches_addresses(
                wallet_cache,
                authoritative,
                expected_name=name,
            ):
                return
            cache["wallets"].pop(name, None)
            save_wallet_cache(cache, self.cache_file)

    def _rollback_import(self, name: str) -> tuple[str, ...]:
        """Remove every persistent artifact created by a failed import."""

        failed_targets = []
        try:
            with _locked_wallet_file(self.wallet_file):
                wallets = _load_wallets(self.wallet_file)
                if wallets.pop(name, None) is not None:
                    _save_wallets(wallets, self.wallet_file)
        except Exception:
            failed_targets.append("authoritative wallet data")

        try:
            self._remove_cache_entry(name)
        except Exception:
            failed_targets.append("wallet cache data")
        return tuple(failed_targets)

    def _maintain_cache(
        self,
        operation: Callable[[], None],
        warning_message: str,
    ) -> bool:
        """Run cache maintenance without negating an authoritative mutation.

        Returning ``False`` tells callers not to use the affected entry.  The
        complete cache is intentionally preserved because other wallets may
        have active payment reservations or pending transaction summaries.
        """

        try:
            operation()
            return True
        except Exception as cache_error:
            warnings.warn(
                f"{warning_message}; stale cache data will be ignored until "
                f"the next successful synchronization: {cache_error}",
                WalletCacheWarning,
                stacklevel=2,
            )
            return False

    def _state_without_cache(
        self,
        name: str,
        *,
        address_type: str | None = None,
    ) -> WalletState:
        """Return authoritative identity/address state without reading cache."""

        metadata = self._metadata(name)
        receive_address = self.get_receive_address(
            name,
            address_type=address_type,
        )
        return self._empty_wallet_state(metadata, receive_address)

    def _state_after_cache_maintenance(
        self,
        name: str,
        cache_usable: bool,
        warning_message: str,
        *,
        address_type: str | None = None,
    ) -> WalletState:
        """Return lifecycle success even when its derived cache is unusable."""

        if cache_usable:
            try:
                return self.get_wallet_state(name, address_type=address_type)
            except WalletError as cache_error:
                warnings.warn(
                    f"{warning_message}; it will be rebuilt by synchronization: "
                    f"{cache_error}",
                    WalletCacheWarning,
                    stacklevel=2,
                )
        return self._state_without_cache(name, address_type=address_type)

    @staticmethod
    def _empty_wallet_state(
        metadata: WalletMetadata,
        receive_address: str,
    ) -> WalletState:
        return WalletState(
            metadata,
            0,
            0,
            0,
            0,
            0,
            0,
            receive_address,
            (),
            None,
            (),
        )

    def _cache_entry_matches_wallet(self, name: str, wallet_cache: dict) -> bool:
        """Reject cache entries that belong to an older wallet with this name."""

        return self._cache_entry_matches_addresses(
            wallet_cache,
            self._authoritative_address_identity(name),
            expected_name=name,
        )

    def _authoritative_address_identity(self, name: str) -> dict[str, dict]:
        """Index the stable address fields that identify one deterministic wallet."""

        address_book = get_wallet_address_book(
            name,
            wallet_file=self.wallet_file,
            network=self.network,
        )
        return {
            entry.get("address"): entry
            for entry in address_book["addresses"]
            if isinstance(entry, dict) and isinstance(entry.get("address"), str)
        }

    @staticmethod
    def _cache_entry_matches_addresses(
        wallet_cache: dict,
        authoritative: dict[str, dict],
        *,
        expected_name: str,
    ) -> bool:
        """Return whether a cache entry is a subset of one wallet address book."""

        if wallet_cache.get("wallet_name") != expected_name:
            return False
        cached_addresses = wallet_cache.get("addresses")
        if not isinstance(cached_addresses, list) or not cached_addresses:
            return False
        for cached in cached_addresses:
            if not isinstance(cached, dict):
                return False
            address = cached.get("address")
            stored = authoritative.get(address)
            if stored is None or any(
                cached.get(field) != stored.get(field)
                for field in ("branch", "index", "account_id", "address_type")
            ):
                return False
        return True


def _encrypt_mnemonic(name: str, mnemonic: str, password: str | None) -> dict:
    if not password:
        raise WalletError("password is required for encrypted wallet data")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key = _derive_key(password, salt, PBKDF2_ITERATIONS)
    ciphertext = AESGCM(key).encrypt(
        nonce,
        mnemonic.encode("utf-8"),
        name.encode("utf-8"),
    )
    return {
        "cipher": "AES-256-GCM",
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
    }


def _parse_timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _redact_recovery_words(message: str, *secret_values: str) -> str:
    """Remove caller-provided recovery words from a surfaced error message."""

    safe_message = message or "wallet import failed"
    secrets_to_redact = {
        secret
        for value in secret_values
        for secret in (value, " ".join(value.split()))
        if secret
    }
    for secret in sorted(secrets_to_redact, key=len, reverse=True):
        safe_message = safe_message.replace(secret, "[recovery words redacted]")
    return safe_message


def _address_stats_have_history(address_data: object) -> bool:
    """Return whether Esplora reports confirmed or mempool address history."""

    if not isinstance(address_data, dict):
        raise WalletError("wallet discovery backend returned invalid address data")

    transaction_counts = []
    for field in ("chain_stats", "mempool_stats"):
        statistics = address_data.get(field)
        if not isinstance(statistics, dict):
            raise WalletError(
                f'wallet discovery backend returned invalid "{field}" data'
            )
        transaction_count = statistics.get("tx_count")
        if (
            isinstance(transaction_count, bool)
            or not isinstance(transaction_count, int)
            or transaction_count < 0
        ):
            raise WalletError(
                f'wallet discovery backend returned invalid "{field}.tx_count"'
            )
        transaction_counts.append(transaction_count)

    return any(count > 0 for count in transaction_counts)
