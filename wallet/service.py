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
    mnemonic: str
    imported: bool
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
        self.address_type = address_type
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

    def create_wallet(self, name: str, password: str | None) -> WalletCreationResult:
        """Create a generated wallet and issue its first receive address."""

        mnemonic = mnemonic_from_entropy_hex(secrets.token_hex(32))
        return self._create(name, password, mnemonic, imported=False)

    def import_wallet(
        self,
        name: str,
        password: str | None,
        mnemonic: str,
    ) -> WalletCreationResult:
        """Create a wallet from mnemonic and scan receive/change discovery pools."""

        return self._create(
            name,
            password,
            normalize_mnemonic(mnemonic),
            imported=True,
        )

    def _create(
        self,
        name: str,
        password: str | None,
        mnemonic: str,
        *,
        imported: bool,
    ) -> WalletCreationResult:
        with self.operation_lock:
            discovery = None
            create_wallet_record(
                name,
                password=password,
                mnemonic=mnemonic,
                wallet_file=self.wallet_file,
                network=self.network,
            )
            if imported:
                discovery = self._discover_imported_wallet(name)
                state = self.sync_wallet(name)
            else:
                get_new_address(
                    name,
                    wallet_file=self.wallet_file,
                    address_type=self.address_type,
                    network=self.network,
                )
                state = self.get_wallet_state(name)
            return WalletCreationResult(state, mnemonic, imported, discovery)

    def _discover_imported_wallet(self, name: str) -> WalletDiscovery:
        """Discover address history cheaply, then persist both branches once."""

        backend = self.backend_factory(self.network)
        if getattr(backend, "network", None) != self.network:
            raise WalletError("wallet discovery backend network does not match wallet")
        receive, receive_usage = self._discover_branch(
            name,
            BTC_RECEIVE_BRANCH,
            backend,
        )
        change, change_usage = self._discover_branch(
            name,
            BTC_CHANGE_BRANCH,
            backend,
        )
        commit_discovered_addresses(
            name,
            receive_usage,
            change_usage,
            wallet_file=self.wallet_file,
            address_type=self.address_type,
            network=self.network,
        )
        return WalletDiscovery(receive, change)

    def _discover_branch(
        self,
        name: str,
        branch: int,
        backend,
    ) -> tuple[BranchDiscovery, dict[int, bool]]:
        """Find the last-used index using address statistics only."""

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
                address_type=self.address_type,
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

    def get_wallet_state(self, name: str) -> WalletState:
        """Return display-neutral state from wallet metadata and public cache."""

        metadata = self._metadata(name)
        receive_address = self.get_receive_address(name)
        if not self.cache_file.exists():
            return WalletState(metadata, 0, 0, 0, 0, 0, 0, receive_address, (), None, ())
        with locked_cache_file(self.cache_file):
            cache = load_wallet_cache(self.cache_file)
            wallet_cache = cache.get("wallets", {}).get(name)
            if not isinstance(wallet_cache, dict):
                return WalletState(metadata, 0, 0, 0, 0, 0, 0, receive_address, (), None, ())
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

    def get_receive_address(self, name: str) -> str:
        """Return index zero or the first receive address after the last used one."""

        self._metadata(name)
        address_book = get_wallet_address_book(
            name,
            wallet_file=self.wallet_file,
            address_type=self.address_type,
            network=self.network,
        )
        receive_entries = [
            entry for entry in address_book["addresses"] if entry.get("branch") == 0
        ]
        if not receive_entries:
            return get_new_address(
                name,
                wallet_file=self.wallet_file,
                address_type=self.address_type,
                network=self.network,
            )["address"]
        target_index = self._next_receive_index(name)
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
                address_type=self.address_type,
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
    ) -> WalletState:
        """Synchronize a wallet while preserving history on UTXO-only scans."""

        self._metadata(name)
        with self.operation_lock:
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
            return self.get_wallet_state(name)

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
    ) -> WalletState:
        """Rename metadata, encryption AAD, and the matching cache entry."""

        _validate_wallet_name(new_name)
        with self.operation_lock:
            self._ensure_cache_name_available(new_name)
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
            self._rename_cache_entry(name, new_name)
        return self.get_wallet_state(new_name)

    def change_password(
        self,
        name: str,
        current_password: str | None,
        new_password: str | None,
    ) -> WalletState:
        """Re-encrypt the mnemonic while preserving all deterministic state."""

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
        return self.get_wallet_state(name)

    def remove_wallet(self, name: str, password: str | None) -> None:
        """Verify ownership, remove the wallet, and remove only its cache entry."""

        with self.operation_lock, _locked_wallet_file(self.wallet_file):
            wallets = _load_wallets(self.wallet_file)
            wallet = wallets.get(name)
            if not isinstance(wallet, dict):
                raise WalletError(f'wallet "{name}" does not exist')
            _require_current_wallet(wallet, self.network)
            if wallet.get("encrypted") is True:
                _read_mnemonic(name, wallet, password, self.network)
            wallets.pop(name)
            _save_wallets(wallets, self.wallet_file)
        self._remove_cache_entry(name)

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

    def _metadata(self, name: str) -> WalletMetadata:
        match = next((wallet for wallet in self.list_wallets() if wallet.name == name), None)
        if match is None:
            raise WalletError(f'wallet "{name}" does not exist on {self.network}')
        return match

    def _next_receive_index(self, name: str) -> int:
        if not self.cache_file.exists():
            return 0
        with locked_cache_file(self.cache_file):
            cache = load_wallet_cache(self.cache_file)
            wallet_cache = cache.get("wallets", {}).get(name)
            if not isinstance(wallet_cache, dict):
                return 0
            addresses = wallet_cache.get("addresses", [])
        used_indexes = [
            entry.get("index")
            for entry in addresses
            if isinstance(entry, dict)
            and entry.get("branch") == 0
            and str(entry.get("address_type", "")).lower() == self.address_type
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
            if new_name in wallets:
                raise WalletError(f'wallet cache for "{new_name}" already exists')
            entry = wallets.pop(name, None)
            if isinstance(entry, dict):
                entry["wallet_name"] = new_name
                wallets[new_name] = entry
                save_wallet_cache(cache, self.cache_file)

    def _ensure_cache_name_available(self, new_name: str) -> None:
        if not self.cache_file.exists():
            return
        with locked_cache_file(self.cache_file):
            cache = load_wallet_cache(self.cache_file)
            if new_name in cache["wallets"]:
                raise WalletError(f'wallet cache for "{new_name}" already exists')

    def _remove_cache_entry(self, name: str) -> None:
        if not self.cache_file.exists():
            return
        with locked_cache_file(self.cache_file):
            cache = load_wallet_cache(self.cache_file)
            if cache["wallets"].pop(name, None) is not None:
                save_wallet_cache(cache, self.cache_file)


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
