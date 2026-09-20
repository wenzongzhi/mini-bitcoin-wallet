"""Versioned, non-secret settings for Mini Bitcoin Wallet.

The application settings file deliberately lives outside the bitcoin-tool
wallet data directory. It contains product preferences and references to
wallet storage, never wallet secrets or Platform wallet data.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import ipaddress
import json
import os
from pathlib import Path
import tempfile
from types import MappingProxyType
from typing import Callable, Mapping
from urllib.parse import SplitResult, urlsplit

from filelock import FileLock, Timeout
from platformdirs import user_config_path

from btc.chainparams import NETWORK_MAINNET, NETWORK_TESTNET4
from network import EsploraBackend
from wallet import default_wallet_file


SETTINGS_VERSION = 2
APPLICATION_NAME = "mini-bitcoin-wallet"
SUPPORTED_NETWORKS = (NETWORK_MAINNET, NETWORK_TESTNET4)
SUPPORTED_DISPLAY_UNITS = ("BTC", "sats")
SUPPORTED_FIAT_CURRENCIES = ("USD", "JPY", "CNY", "EUR")
SUPPORTED_THEMES = ("system", "light", "dark")
SUPPORTED_BACKEND_MODES = ("default", "custom")


class SettingsError(ValueError):
    """Base class for settings persistence and validation failures."""


class UnsupportedSettingsVersionError(SettingsError):
    """Raised when a settings document is not the current schema version."""


class SettingsValidationError(SettingsError):
    """Raised when a Version 2 settings value violates its schema."""


@dataclass(frozen=True, slots=True)
class GeneralSettings:
    """Presentation preferences that can be applied without restarting."""

    display_unit: str = "BTC"
    fiat_currency: str = "USD"
    theme: str = "system"
    hide_balance: bool = False

    def __post_init__(self) -> None:
        if self.display_unit not in SUPPORTED_DISPLAY_UNITS:
            raise SettingsValidationError(
                "Bitcoin unit must be either BTC or sats."
            )
        if self.fiat_currency not in SUPPORTED_FIAT_CURRENCIES:
            raise SettingsValidationError("Unsupported fiat currency.")
        if self.theme not in SUPPORTED_THEMES:
            raise SettingsValidationError("Theme must be system, light, or dark.")
        if type(self.hide_balance) is not bool:
            raise SettingsValidationError("Hide balance must be true or false.")


@dataclass(frozen=True, slots=True)
class BackendSettings:
    """Esplora selection for one Bitcoin network."""

    backend_mode: str = "default"
    custom_esplora_url: str | None = None

    def __post_init__(self) -> None:
        if self.backend_mode not in SUPPORTED_BACKEND_MODES:
            raise SettingsValidationError("Backend mode must be default or custom.")
        if self.custom_esplora_url is not None:
            _validate_esplora_url(self.custom_esplora_url)
        if self.backend_mode == "custom" and self.custom_esplora_url is None:
            raise SettingsValidationError(
                "A custom Esplora URL is required for a custom backend."
            )


@dataclass(frozen=True, slots=True)
class StorageSettings:
    """Application-wide wallet data directory selection.

    ``None`` means that bitcoin-tool's current default data directory is used.
    """

    wallet_data_dir: Path | None = None

    def __post_init__(self) -> None:
        value = self.wallet_data_dir
        if value is None:
            return
        if not isinstance(value, (str, os.PathLike)):
            raise SettingsValidationError(
                "Wallet data directory must be an absolute path or null."
            )
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise SettingsValidationError(
                "Wallet data directory must be an absolute path or null."
            )
        object.__setattr__(self, "wallet_data_dir", path.resolve(strict=False))


@dataclass(frozen=True, slots=True)
class ApplicationSettings:
    """An immutable, fully validated Version 2 settings snapshot."""

    version: int
    active_wallets: Mapping[str, str | None]
    general: GeneralSettings
    network: Mapping[str, BackendSettings]
    storage: StorageSettings

    def __post_init__(self) -> None:
        if self.version != SETTINGS_VERSION:
            raise UnsupportedSettingsVersionError(
                f"Unsupported settings version {self.version!r}; "
                f"Version {SETTINGS_VERSION} is required."
            )
        active_wallets = _validated_active_wallets(self.active_wallets)
        network = _validated_network_settings(self.network)
        if not isinstance(self.general, GeneralSettings):
            raise SettingsValidationError("Invalid general settings.")
        if not isinstance(self.storage, StorageSettings):
            raise SettingsValidationError("Invalid storage settings.")
        object.__setattr__(self, "active_wallets", MappingProxyType(active_wallets))
        object.__setattr__(self, "network", MappingProxyType(network))


def default_application_settings_file() -> Path:
    """Return Mini Bitcoin Wallet's stable, application-specific config file."""

    return user_config_path(APPLICATION_NAME, appauthor=False) / "settings.json"


def default_settings(
    wallet_data_dir: str | os.PathLike[str] | None = None,
) -> ApplicationSettings:
    """Return a fresh Version 2 settings snapshot."""

    return ApplicationSettings(
        version=SETTINGS_VERSION,
        active_wallets={network: None for network in SUPPORTED_NETWORKS},
        general=GeneralSettings(),
        network={network: BackendSettings() for network in SUPPORTED_NETWORKS},
        storage=StorageSettings(wallet_data_dir),
    )


def detect_legacy_wallet_data_dir(
    legacy_data_dir: str | os.PathLike[str],
    network: str,
) -> Path | None:
    """Locate this network's old mini-wallet data without touching it.

    The corresponding Platform wallet wins when it exists. Otherwise the
    legacy directory is selected only when it contains this network's standard
    wallet filename. Wallet contents are never read, copied, moved, merged, or
    deleted.
    """

    _validate_network(network)
    # Resolve through bitcoin-tool's public wallet package so its platformdirs
    # and BITCOIN_TOOL_DATADIR policy remain the single source of truth.
    if default_wallet_file(network=network).is_file():
        return None

    legacy_directory = Path(legacy_data_dir).expanduser().resolve(strict=False)
    legacy_wallet = default_wallet_file(legacy_directory, network)
    return legacy_directory if legacy_wallet.is_file() else None


def wallet_data_dir_from_selected_file(
    selected_file: str | os.PathLike[str],
    network: str,
) -> Path:
    """Validate a selected standard wallet file and return its parent directory."""

    _validate_network(network)
    path = Path(selected_file).expanduser().resolve(strict=False)
    expected_name = default_wallet_file(path.parent, network).name
    if path.name != expected_name:
        raise SettingsValidationError(
            f'The selected wallet file must be named "{expected_name}".'
        )
    if not path.exists():
        raise SettingsValidationError("The selected wallet file does not exist.")
    if not path.is_file():
        raise SettingsValidationError("The selected wallet path is not a file.")
    if not os.access(path.parent, os.R_OK) or not os.access(path, os.R_OK):
        raise SettingsValidationError("Cannot read the selected wallet file.")
    return path.parent


def unencrypted_http_warning(url: str) -> str | None:
    """Return a warning for public unencrypted HTTP endpoints.

    HTTP remains supported for loopback, private-network, link-local, and local
    hostnames because self-hosted Esplora installations commonly use it.
    """

    parsed = _validate_esplora_url(url)
    if parsed.scheme != "http" or _is_local_hostname(parsed.hostname or ""):
        return None
    return "Unencrypted HTTP backend. HTTPS is recommended for Internet endpoints."


def create_backend_factory(
    settings_store: "ApplicationSettingsStore",
) -> Callable[[str], EsploraBackend]:
    """Build a factory that reads the latest backend setting on every call."""

    def create_backend(network: str) -> EsploraBackend:
        backend = settings_store.backend(network)
        if backend.backend_mode == "default":
            return EsploraBackend(network=network)
        return EsploraBackend(
            base_url=backend.custom_esplora_url,
            network=network,
        )

    return create_backend


class ApplicationSettingsStore:
    """Read and atomically update Mini Bitcoin Wallet's Version 2 settings."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_application_settings_file()
        self.lock_path = Path(f"{self.path}.lock")

    def load_or_create(
        self,
        initial: ApplicationSettings | None = None,
    ) -> ApplicationSettings:
        """Load settings, atomically creating ``initial`` when absent."""

        if initial is not None and not isinstance(initial, ApplicationSettings):
            raise SettingsValidationError("Initial settings are invalid.")
        with self._locked():
            if self.path.exists():
                return self._read_unlocked()
            settings = initial or default_settings()
            self._write_unlocked(settings)
            return settings

    def load(self) -> ApplicationSettings:
        """Load settings, creating the Version 2 defaults when absent."""

        return self.load_or_create()

    def ensure_exists(self) -> None:
        """Compatibility helper for callers that only need file creation."""

        self.load_or_create()

    def general(self) -> GeneralSettings:
        return self.load().general

    def backend(self, network: str) -> BackendSettings:
        _validate_network(network)
        return self.load().network[network]

    def wallet_data_dir(self) -> Path | None:
        return self.load().storage.wallet_data_dir

    def active_wallet(self, network: str) -> str | None:
        _validate_network(network)
        return self.load().active_wallets[network]

    def set_active_wallet(
        self,
        network: str,
        wallet_name: str | None,
    ) -> ApplicationSettings:
        """Persist one active wallet without changing product preferences."""

        _validate_network(network)
        _validate_wallet_name(wallet_name)
        with self._locked():
            current = self._read_unlocked()
            active_wallets = dict(current.active_wallets)
            active_wallets[network] = wallet_name
            updated = replace(current, active_wallets=active_wallets)
            self._write_unlocked(updated)
            return updated

    def apply_preferences(
        self,
        *,
        general: GeneralSettings,
        network: str,
        backend: BackendSettings,
        storage: StorageSettings,
    ) -> ApplicationSettings:
        """Atomically save one dialog draft while preserving wallet selection.

        Only the current network's backend is replaced. The other network and
        both active-wallet values are reloaded under the file lock so a dialog
        opened earlier cannot overwrite a more recent wallet selection.
        """

        _validate_network(network)
        if not isinstance(general, GeneralSettings):
            raise SettingsValidationError("Invalid general settings.")
        if not isinstance(backend, BackendSettings):
            raise SettingsValidationError("Invalid backend settings.")
        if not isinstance(storage, StorageSettings):
            raise SettingsValidationError("Invalid storage settings.")

        with self._locked():
            current = self._read_unlocked()
            network_settings = dict(current.network)
            network_settings[network] = backend
            updated = replace(
                current,
                general=general,
                network=network_settings,
                storage=storage,
            )
            self._write_unlocked(updated)
            return updated

    def _read_unlocked(self) -> ApplicationSettings:
        try:
            with self.path.open("r", encoding="utf-8") as file:
                document = json.load(file)
        except FileNotFoundError:
            return default_settings()
        except (OSError, json.JSONDecodeError) as exc:
            raise SettingsError("Cannot read application settings.") from exc
        return _decode_settings(document)

    def _write_unlocked(self, settings: ApplicationSettings) -> None:
        if not isinstance(settings, ApplicationSettings):
            raise SettingsValidationError("Invalid application settings.")
        document = _encode_settings(settings)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                json.dump(document, file, indent=2, ensure_ascii=False)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise SettingsError("Cannot save application settings.") from exc

    @contextmanager
    def _locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock = FileLock(self.lock_path)
            with lock.acquire(timeout=10):
                yield
        except Timeout as exc:
            raise SettingsError("Application settings are busy.") from exc
        except OSError as exc:
            raise SettingsError("Cannot lock application settings.") from exc


def _validate_network(network: str) -> None:
    if network not in SUPPORTED_NETWORKS:
        raise SettingsValidationError(f"Unsupported settings network: {network}")


def _validate_wallet_name(wallet_name: str | None) -> None:
    if wallet_name is not None and (
        not isinstance(wallet_name, str) or not wallet_name.strip()
    ):
        raise SettingsValidationError(
            "Active wallet name must be a non-empty string or null."
        )


def _validated_active_wallets(
    values: Mapping[str, str | None],
) -> dict[str, str | None]:
    if not isinstance(values, Mapping) or set(values) != set(SUPPORTED_NETWORKS):
        raise SettingsValidationError("Invalid active wallet settings.")
    validated = dict(values)
    for wallet_name in validated.values():
        _validate_wallet_name(wallet_name)
    return validated


def _validated_network_settings(
    values: Mapping[str, BackendSettings],
) -> dict[str, BackendSettings]:
    if not isinstance(values, Mapping) or set(values) != set(SUPPORTED_NETWORKS):
        raise SettingsValidationError("Invalid network settings.")
    validated = dict(values)
    if not all(isinstance(value, BackendSettings) for value in validated.values()):
        raise SettingsValidationError("Invalid network settings.")
    return validated


def _validate_esplora_url(url: str) -> SplitResult:
    if not isinstance(url, str) or not url or url != url.strip():
        raise SettingsValidationError("Enter a valid HTTP or HTTPS backend URL.")
    try:
        parsed = urlsplit(url)
        # Accessing ``port`` also validates a malformed numeric port.
        parsed.port
    except ValueError as exc:
        raise SettingsValidationError(
            "Enter a valid HTTP or HTTPS backend URL."
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SettingsValidationError("Enter a valid HTTP or HTTPS backend URL.")
    return parsed


def _is_local_hostname(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".local") or "." not in normalized:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def _decode_settings(document: object) -> ApplicationSettings:
    if not isinstance(document, dict):
        raise SettingsValidationError("Application settings must be a JSON object.")

    version = document.get("version")
    if version != SETTINGS_VERSION:
        raise UnsupportedSettingsVersionError(
            f"Unsupported settings version {version!r}; "
            f"Version {SETTINGS_VERSION} is required."
        )
    _require_exact_keys(
        document,
        {"version", "active_wallets", "general", "network", "storage"},
        "application settings",
    )

    active_wallets = _require_object(document["active_wallets"], "active wallets")
    _require_exact_keys(active_wallets, set(SUPPORTED_NETWORKS), "active wallets")

    general_document = _require_object(document["general"], "general settings")
    _require_exact_keys(
        general_document,
        {"display_unit", "fiat_currency", "theme", "hide_balance"},
        "general settings",
    )
    general = GeneralSettings(
        display_unit=general_document["display_unit"],
        fiat_currency=general_document["fiat_currency"],
        theme=general_document["theme"],
        hide_balance=general_document["hide_balance"],
    )

    network_document = _require_object(document["network"], "network settings")
    _require_exact_keys(network_document, set(SUPPORTED_NETWORKS), "network settings")
    network: dict[str, BackendSettings] = {}
    for network_name in SUPPORTED_NETWORKS:
        backend_document = _require_object(
            network_document[network_name], f"{network_name} backend settings"
        )
        _require_exact_keys(
            backend_document,
            {"backend_mode", "custom_esplora_url"},
            f"{network_name} backend settings",
        )
        network[network_name] = BackendSettings(
            backend_mode=backend_document["backend_mode"],
            custom_esplora_url=backend_document["custom_esplora_url"],
        )

    storage_document = _require_object(document["storage"], "storage settings")
    _require_exact_keys(storage_document, {"wallet_data_dir"}, "storage settings")
    raw_data_dir = storage_document["wallet_data_dir"]
    if raw_data_dir is not None and not isinstance(raw_data_dir, str):
        raise SettingsValidationError("Invalid wallet data directory.")

    return ApplicationSettings(
        version=SETTINGS_VERSION,
        active_wallets=active_wallets,
        general=general,
        network=network,
        storage=StorageSettings(raw_data_dir),
    )


def _encode_settings(settings: ApplicationSettings) -> dict:
    return {
        "version": SETTINGS_VERSION,
        "active_wallets": {
            network: settings.active_wallets[network]
            for network in SUPPORTED_NETWORKS
        },
        "general": {
            "display_unit": settings.general.display_unit,
            "fiat_currency": settings.general.fiat_currency,
            "theme": settings.general.theme,
            "hide_balance": settings.general.hide_balance,
        },
        "network": {
            network: {
                "backend_mode": settings.network[network].backend_mode,
                "custom_esplora_url": settings.network[network].custom_esplora_url,
            }
            for network in SUPPORTED_NETWORKS
        },
        "storage": {
            "wallet_data_dir": (
                str(settings.storage.wallet_data_dir)
                if settings.storage.wallet_data_dir is not None
                else None
            )
        },
    }


def _require_object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise SettingsValidationError(f"Invalid {label}.")
    return value


def _require_exact_keys(value: Mapping, expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise SettingsValidationError(f"Invalid {label}.")
