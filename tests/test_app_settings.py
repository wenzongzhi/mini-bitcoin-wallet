import json
from tempfile import TemporaryDirectory
from pathlib import Path

from app_settings import ApplicationSettingsStore


def test_settings_updates_are_atomic_and_network_specific() -> None:
    with TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        store = ApplicationSettingsStore(settings_path)
        store.ensure_exists()

        store.set_active_wallet("mainnet", "Personal")
        store.set_active_wallet("testnet4", "Testing")

        assert json.loads(settings_path.read_text(encoding="utf-8")) == {
            "version": 1,
            "active_wallets": {
                "mainnet": "Personal",
                "testnet4": "Testing",
            },
        }
        assert list(settings_path.parent.glob(".settings.json.*.tmp")) == []


def test_settings_reject_unsupported_networks() -> None:
    with TemporaryDirectory() as directory:
        store = ApplicationSettingsStore(Path(directory) / "settings.json")

        try:
            store.set_active_wallet("signet", "Testing")
        except ValueError as exc:
            assert "Unsupported settings network" in str(exc)
        else:
            raise AssertionError("Unsupported network was accepted")
