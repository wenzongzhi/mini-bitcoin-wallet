import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import app_settings
from app_settings import (
    ApplicationSettingsStore,
    BackendSettings,
    GeneralSettings,
    SettingsError,
    SettingsValidationError,
    StorageSettings,
    UnsupportedSettingsVersionError,
    create_backend_factory,
    default_application_settings_file,
    default_settings,
    detect_legacy_wallet_data_dir,
    unencrypted_http_warning,
    wallet_data_dir_from_selected_file,
)
from wallet import default_wallet_cache_file, default_wallet_file


def test_new_settings_file_uses_complete_version_2_defaults() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        settings = ApplicationSettingsStore(path).load()

        assert settings == default_settings()
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "version": 2,
            "active_wallets": {"mainnet": None, "testnet4": None},
            "general": {
                "display_unit": "BTC",
                "fiat_currency": "USD",
                "theme": "system",
                "hide_balance": False,
            },
            "network": {
                "mainnet": {
                    "backend_mode": "default",
                    "custom_esplora_url": None,
                },
                "testnet4": {
                    "backend_mode": "default",
                    "custom_esplora_url": None,
                },
            },
            "storage": {"wallet_data_dir": None},
        }


def test_load_or_create_uses_initial_only_for_first_creation() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        store = ApplicationSettingsStore(path)
        legacy = Path(directory).resolve() / "legacy"

        created = store.load_or_create(default_settings(legacy))
        loaded = store.load_or_create(default_settings(Path(directory).resolve()))

        assert created.storage.wallet_data_dir == legacy
        assert loaded == created


def test_round_trip_and_atomic_preferences_preserve_wallets_and_other_network() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        store = ApplicationSettingsStore(path)
        store.set_active_wallet("mainnet", "Personal")
        store.set_active_wallet("testnet4", "Testing")
        first = store.load()
        store.apply_preferences(
            general=first.general,
            network="testnet4",
            backend=BackendSettings("custom", "http://localhost:3006/api"),
            storage=first.storage,
        )
        stale_dialog = store.load()
        store.set_active_wallet("mainnet", "NewSelection")
        saved = store.apply_preferences(
            general=GeneralSettings("sats", "CNY", "dark", True),
            network="mainnet",
            backend=BackendSettings("custom", "https://node.example/api"),
            storage=StorageSettings(Path(directory).resolve()),
        )
        loaded = ApplicationSettingsStore(path).load()

        assert loaded == saved
        assert loaded.active_wallets == {
            "mainnet": "NewSelection",
            "testnet4": "Testing",
        }
        assert loaded.network["testnet4"] == stale_dialog.network["testnet4"]
        assert loaded.general == GeneralSettings("sats", "CNY", "dark", True)
        assert loaded.storage.wallet_data_dir == Path(directory).resolve()
        assert list(path.parent.glob(".settings.json.*.tmp")) == []


@pytest.mark.parametrize("version", [1, 3, None, "2"])
def test_unsupported_versions_are_not_migrated(version) -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        path.write_text(json.dumps({"version": version}), encoding="utf-8")

        with pytest.raises(UnsupportedSettingsVersionError, match="Version 2"):
            ApplicationSettingsStore(path).load()

        assert json.loads(path.read_text(encoding="utf-8")) == {"version": version}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["general"].update(display_unit="Sats"),
        lambda doc: doc["general"].update(fiat_currency="GBP"),
        lambda doc: doc["general"].update(theme="blue"),
        lambda doc: doc["general"].update(hide_balance=1),
        lambda doc: doc["network"]["mainnet"].update(
            backend_mode="custom", custom_esplora_url=None
        ),
        lambda doc: doc["storage"].update(wallet_data_dir="relative/path"),
        lambda doc: doc.pop("storage"),
        lambda doc: doc.update(extra=True),
    ],
)
def test_invalid_v2_values_are_rejected_without_fallback(mutate) -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        ApplicationSettingsStore(path).load()
        document = json.loads(path.read_text(encoding="utf-8"))
        mutate(document)
        path.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(SettingsValidationError):
            ApplicationSettingsStore(path).load()


def test_invalid_update_does_not_change_existing_file() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        store = ApplicationSettingsStore(path)
        store.set_active_wallet("mainnet", "Personal")
        original = path.read_bytes()

        with pytest.raises(SettingsValidationError):
            store.apply_preferences(
                general=GeneralSettings(),
                network="signet",
                backend=BackendSettings(),
                storage=StorageSettings(),
            )

        assert path.read_bytes() == original


def test_failed_atomic_replace_preserves_original_and_removes_temporary_file(
    monkeypatch,
) -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        store = ApplicationSettingsStore(path)
        store.load()
        original = path.read_bytes()

        def fail_replace(source, destination):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(app_settings.os, "replace", fail_replace)
        with pytest.raises(SettingsError, match="Cannot save"):
            store.set_active_wallet("mainnet", "Personal")

        assert path.read_bytes() == original
        assert list(path.parent.glob(".settings.json.*.tmp")) == []


def test_settings_snapshots_are_immutable_and_validate_wallet_names() -> None:
    settings = default_settings()
    with pytest.raises(TypeError):
        settings.active_wallets["mainnet"] = "Wallet"  # type: ignore[index]
    with pytest.raises(TypeError):
        settings.network["mainnet"] = BackendSettings()  # type: ignore[index]
    with TemporaryDirectory() as directory:
        store = ApplicationSettingsStore(Path(directory) / "settings.json")
        with pytest.raises(SettingsValidationError, match="non-empty"):
            store.set_active_wallet("mainnet", "  ")


def test_default_settings_file_uses_mini_wallet_config_directory(monkeypatch) -> None:
    expected = Path("X:/mini-config")
    monkeypatch.setattr(app_settings, "user_config_path", lambda *args, **kwargs: expected)
    assert default_application_settings_file() == expected / "settings.json"


@pytest.mark.parametrize(
    ("network", "wallet_name", "cache_name"),
    [
        ("mainnet", "wallets.json", "wallet_cache.json"),
        ("testnet4", "wallets_testnet4.json", "wallet_cache_testnet4.json"),
    ],
)
def test_platform_default_wallet_and_cache_paths_are_used_directly(
    monkeypatch,
    tmp_path: Path,
    network: str,
    wallet_name: str,
    cache_name: str,
) -> None:
    data_directory = (tmp_path / "bitcoin-tool-data").resolve()
    monkeypatch.setenv("BITCOIN_TOOL_DATADIR", str(data_directory))

    assert default_wallet_file(network=network) == data_directory / wallet_name
    assert default_wallet_cache_file(network=network) == data_directory / cache_name


def test_legacy_detection_is_non_destructive_and_corresponding_default_wins(
    monkeypatch,
) -> None:
    with TemporaryDirectory() as platform, TemporaryDirectory() as legacy:
        monkeypatch.setenv("BITCOIN_TOOL_DATADIR", platform)
        legacy_path = Path(legacy)
        legacy_wallet = default_wallet_file(legacy_path, "testnet4")
        legacy_wallet.write_text("do not read or change this", encoding="utf-8")
        before = legacy_wallet.read_bytes()

        assert detect_legacy_wallet_data_dir(
            legacy_path, "testnet4"
        ) == legacy_path.resolve()
        assert legacy_wallet.read_bytes() == before
        default_wallet_file(Path(platform), "mainnet").write_text(
            "other network", encoding="utf-8"
        )
        assert detect_legacy_wallet_data_dir(
            legacy_path, "testnet4"
        ) == legacy_path.resolve()
        default_wallet_file(Path(platform), "testnet4").write_text(
            "platform wins", encoding="utf-8"
        )
        assert detect_legacy_wallet_data_dir(legacy_path, "testnet4") is None
        assert legacy_wallet.read_bytes() == before


def test_legacy_detection_ignores_nonstandard_wallet_filename(monkeypatch) -> None:
    with TemporaryDirectory() as platform, TemporaryDirectory() as legacy:
        monkeypatch.setenv("BITCOIN_TOOL_DATADIR", platform)
        Path(legacy, "wallet_old.json").write_text("ignored", encoding="utf-8")
        assert detect_legacy_wallet_data_dir(legacy, "mainnet") is None


@pytest.mark.parametrize(
    ("network", "filename"),
    [("mainnet", "wallets.json"), ("testnet4", "wallets_testnet4.json")],
)
def test_selected_standard_wallet_file_returns_parent(network, filename) -> None:
    with TemporaryDirectory() as directory:
        wallet_file = Path(directory) / filename
        wallet_file.write_text("{}", encoding="utf-8")
        assert wallet_data_dir_from_selected_file(wallet_file, network) == Path(
            directory
        ).resolve()


def test_selected_wallet_file_rejects_wrong_name_missing_file_and_directory() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        wrong = root / "wallet_old.json"
        wrong.write_text("{}", encoding="utf-8")
        named_directory = root / "wallets.json"
        named_directory.mkdir()

        with pytest.raises(SettingsValidationError, match="must be named"):
            wallet_data_dir_from_selected_file(wrong, "mainnet")
        with pytest.raises(SettingsValidationError, match="not a file"):
            wallet_data_dir_from_selected_file(named_directory, "mainnet")
        named_directory.rmdir()
        with pytest.raises(SettingsValidationError, match="does not exist"):
            wallet_data_dir_from_selected_file(named_directory, "mainnet")


@pytest.mark.parametrize(
    "url",
    [
        "https://node.example/api",
        "http://192.168.2.100:3006/api",
        "http://10.0.0.5/api",
        "http://localhost:3006/api",
        "http://umbrel.local:3006/api",
    ],
)
def test_http_https_and_lan_backend_urls_are_accepted(url) -> None:
    assert BackendSettings("custom", url).custom_esplora_url == url


def test_only_public_http_backend_receives_warning() -> None:
    assert unencrypted_http_warning("https://example.com/api") is None
    assert unencrypted_http_warning("http://localhost:3006/api") is None
    assert unencrypted_http_warning("http://192.168.1.4/api") is None
    assert "Unencrypted HTTP" in unencrypted_http_warning("http://example.com/api")


def test_backend_factory_reads_latest_setting_for_every_backend(monkeypatch) -> None:
    created = []

    class FakeBackend:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(app_settings, "EsploraBackend", FakeBackend)
    with TemporaryDirectory() as directory:
        store = ApplicationSettingsStore(Path(directory) / "settings.json")
        factory = create_backend_factory(store)
        factory("mainnet")
        settings = store.load()
        store.apply_preferences(
            general=settings.general,
            network="mainnet",
            backend=BackendSettings("custom", "https://new.example/api"),
            storage=settings.storage,
        )
        factory("mainnet")

    assert created == [
        {"network": "mainnet"},
        {"base_url": "https://new.example/api", "network": "mainnet"},
    ]


def test_settings_document_contains_no_wallet_secrets() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        ApplicationSettingsStore(path).load()
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("mnemonic", "seed", "private_key", "xprv", "password"):
            assert forbidden not in text
