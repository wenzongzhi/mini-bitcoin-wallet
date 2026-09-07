"""Persistent, non-secret application preferences.

Wallet material belongs in ``wallets.json``. This store deliberately accepts
only the active wallet name for each supported network.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from filelock import FileLock, Timeout


SETTINGS_VERSION = 1
SUPPORTED_NETWORKS = ("mainnet", "testnet4")


def default_settings() -> dict:
    """Return a new settings document instead of sharing mutable state."""

    return {
        "version": SETTINGS_VERSION,
        "active_wallets": {network: None for network in SUPPORTED_NETWORKS},
    }


class ApplicationSettingsStore:
    """Read and atomically update the application-level ``settings.json``."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock_path = Path(f"{self.path}.lock")

    def ensure_exists(self) -> None:
        with self._locked():
            if not self.path.exists():
                self._write_unlocked(default_settings())

    def active_wallet(self, network: str) -> str | None:
        self._validate_network(network)
        with self._locked():
            return self._read_unlocked()["active_wallets"][network]

    def set_active_wallet(self, network: str, wallet_name: str | None) -> None:
        self._validate_network(network)
        if wallet_name is not None and (
            not isinstance(wallet_name, str) or not wallet_name
        ):
            raise ValueError("Active wallet name must be a non-empty string or null.")
        with self._locked():
            settings = self._read_unlocked()
            settings["active_wallets"][network] = wallet_name
            self._write_unlocked(settings)

    def _read_unlocked(self) -> dict:
        try:
            with self.path.open("r", encoding="utf-8") as file:
                settings = json.load(file)
        except FileNotFoundError:
            return default_settings()
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f'Cannot read settings file "{self.path}": {exc}') from exc

        if not isinstance(settings, dict) or settings.get("version") != SETTINGS_VERSION:
            raise ValueError(f'Invalid settings file "{self.path}".')
        active_wallets = settings.get("active_wallets")
        if not isinstance(active_wallets, dict):
            raise ValueError(f'Invalid settings file "{self.path}".')

        normalized = default_settings()
        for network in SUPPORTED_NETWORKS:
            wallet_name = active_wallets.get(network)
            if wallet_name is not None and not isinstance(wallet_name, str):
                raise ValueError(f'Invalid settings file "{self.path}".')
            normalized["active_wallets"][network] = wallet_name
        return normalized

    def _write_unlocked(self, settings: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
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
                json.dump(settings, file, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise ValueError(f'Cannot write settings file "{self.path}": {exc}') from exc

    @contextmanager
    def _locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(self.lock_path).acquire(timeout=10):
                yield
        except Timeout as exc:
            raise ValueError(f'Settings file is busy: "{self.path}".') from exc
        except OSError as exc:
            raise ValueError(f'Cannot lock settings file "{self.path}": {exc}') from exc

    @staticmethod
    def _validate_network(network: str) -> None:
        if network not in SUPPORTED_NETWORKS:
            raise ValueError(f"Unsupported settings network: {network}")
