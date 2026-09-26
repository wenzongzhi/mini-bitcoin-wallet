import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import app_settings
from app_settings import (
    ActiveWalletSelection,
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
    default_wallet_data_dir,
    select_initial_wallet_data_dir,
    unencrypted_http_warning,
    wallet_data_dir_from_selected_file,
)
from wallet import default_wallet_cache_file, default_wallet_file
from wallet_core.models import AccountType


def _version_2_document(data_directory: Path) -> dict:
    """Return the complete predecessor schema accepted by the V3 migrator."""

    return {
        "version": 2,
        "active_wallets": {
            "mainnet": "Personal",
            "testnet4": None,
        },
        "general": {
            "display_unit": "sats",
            "fiat_currency": "CNY",
            "theme": "dark",
            "hide_balance": True,
        },
        "network": {
            "mainnet": {
                "backend_mode": "custom",
                "custom_esplora_url": "https://node.example/api",
            },
            "testnet4": {
                "backend_mode": "default",
                "custom_esplora_url": None,
            },
        },
        "storage": {"wallet_data_dir": str(data_directory.resolve())},
    }


def test_new_settings_file_uses_complete_version_3_defaults() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        settings = ApplicationSettingsStore(path).load()
        wallet_data_dir = path.parent.resolve()

        assert settings == default_settings(wallet_data_dir)
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "version": 3,
            "active_wallets": {
                "mainnet": {
                    "wallet_name": None,
                    "account_type": "p2wpkh",
                },
                "testnet4": {
                    "wallet_name": None,
                    "account_type": "p2wpkh",
                },
            },
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
            "storage": {"wallet_data_dir": str(wallet_data_dir)},
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


def test_complete_version_2_settings_are_atomically_migrated_to_version_3(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(_version_2_document(tmp_path), indent=2),
        encoding="utf-8",
    )

    loaded = ApplicationSettingsStore(path).load()
    migrated_document = json.loads(path.read_text(encoding="utf-8"))

    assert loaded.version == 3
    assert loaded.active_wallets == {
        "mainnet": ActiveWalletSelection(
            "Personal", AccountType.NATIVE_SEGWIT
        ),
        "testnet4": ActiveWalletSelection(None, AccountType.NATIVE_SEGWIT),
    }
    assert loaded.general == GeneralSettings("sats", "CNY", "dark", True)
    assert loaded.network["mainnet"] == BackendSettings(
        "custom", "https://node.example/api"
    )
    assert loaded.network["testnet4"] == BackendSettings()
    assert loaded.storage == StorageSettings(tmp_path.resolve())
    assert migrated_document["version"] == 3
    assert migrated_document["active_wallets"] == {
        "mainnet": {
            "wallet_name": "Personal",
            "account_type": "p2wpkh",
        },
        "testnet4": {
            "wallet_name": None,
            "account_type": "p2wpkh",
        },
    }
    assert list(path.parent.glob(".settings.json.*.tmp")) == []


def test_failed_version_2_migration_preserves_original_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_version_2_document(tmp_path)), encoding="utf-8")
    original = path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("simulated failure")

    monkeypatch.setattr(app_settings.os, "replace", fail_replace)

    with pytest.raises(SettingsError, match="Cannot save"):
        ApplicationSettingsStore(path).load()

    assert path.read_bytes() == original
    assert list(path.parent.glob(".settings.json.*.tmp")) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["active_wallets"].update(mainnet="  "),
        lambda doc: doc["active_wallets"].update(extra=None),
        lambda doc: doc["general"].update(theme="blue"),
        lambda doc: doc["network"]["mainnet"].update(extra=True),
        lambda doc: doc.pop("storage"),
        lambda doc: doc.update(extra=True),
    ],
)
def test_invalid_version_2_settings_are_not_migrated_or_modified(
    tmp_path: Path,
    mutate,
) -> None:
    path = tmp_path / "settings.json"
    document = _version_2_document(tmp_path)
    mutate(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(SettingsValidationError):
        ApplicationSettingsStore(path).load()

    assert path.read_bytes() == original


def test_round_trip_and_atomic_preferences_preserve_wallets_and_other_network() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        store = ApplicationSettingsStore(path)
        store.set_active_wallet("mainnet", "Personal")
        store.set_active_wallet("testnet4", "Testing")
        store.set_active_account_type(
            "testnet4",
            AccountType.LEGACY,
            expected_wallet_name="Testing",
        )
        first = store.load()
        store.apply_preferences(
            general=first.general,
            network="testnet4",
            backend=BackendSettings("custom", "http://localhost:3006/api"),
            storage=first.storage,
        )
        stale_dialog = store.load()
        store.set_active_wallet("mainnet", "NewSelection")
        store.set_active_account_type(
            "mainnet",
            AccountType.LEGACY,
            expected_wallet_name="NewSelection",
        )
        saved = store.apply_preferences(
            general=GeneralSettings("sats", "CNY", "dark", True),
            network="mainnet",
            backend=BackendSettings("custom", "https://node.example/api"),
            storage=StorageSettings(Path(directory).resolve()),
        )
        loaded = ApplicationSettingsStore(path).load()

        assert loaded == saved
        assert loaded.active_wallets == {
            "mainnet": ActiveWalletSelection(
                "NewSelection", AccountType.LEGACY
            ),
            "testnet4": ActiveWalletSelection("Testing", AccountType.LEGACY),
        }
        assert loaded.network["testnet4"] == stale_dialog.network["testnet4"]
        assert loaded.general == GeneralSettings("sats", "CNY", "dark", True)
        assert loaded.storage.wallet_data_dir == Path(directory).resolve()
        assert list(path.parent.glob(".settings.json.*.tmp")) == []


@pytest.mark.parametrize("version", [1, 4, None, "3"])
def test_unsupported_versions_are_not_migrated(version) -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        path.write_text(json.dumps({"version": version}), encoding="utf-8")
        original = path.read_bytes()

        with pytest.raises(UnsupportedSettingsVersionError, match="Version 3"):
            ApplicationSettingsStore(path).load()

        assert path.read_bytes() == original


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["active_wallets"]["mainnet"].update(
            account_type="P2PKH"
        ),
        lambda doc: doc["active_wallets"]["mainnet"].update(
            account_type="p2sh-p2wpkh"
        ),
        lambda doc: doc["active_wallets"]["mainnet"].update(
            wallet_name=None, account_type="p2pkh"
        ),
        lambda doc: doc["active_wallets"]["mainnet"].update(account_type=1),
        lambda doc: doc["active_wallets"]["mainnet"].update(wallet_name="  "),
        lambda doc: doc["active_wallets"]["mainnet"].pop("account_type"),
        lambda doc: doc["active_wallets"]["mainnet"].update(extra=True),
        lambda doc: doc["active_wallets"].update(mainnet=None),
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
def test_invalid_v3_values_are_rejected_without_fallback(mutate) -> None:
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
        settings.active_wallets["mainnet"] = ActiveWalletSelection(  # type: ignore[index]
            "Wallet"
        )
    with pytest.raises((AttributeError, TypeError)):
        settings.active_wallets["mainnet"].wallet_name = "Wallet"  # type: ignore[misc]
    with pytest.raises(TypeError):
        settings.network["mainnet"] = BackendSettings()  # type: ignore[index]
    with pytest.raises(SettingsValidationError, match="account type"):
        ActiveWalletSelection("Wallet", "p2pkh")  # type: ignore[arg-type]
    with TemporaryDirectory() as directory:
        store = ApplicationSettingsStore(Path(directory) / "settings.json")
        with pytest.raises(SettingsValidationError, match="non-empty"):
            store.set_active_wallet("mainnet", "  ")
    with pytest.raises(SettingsValidationError, match="missing active wallet"):
        ActiveWalletSelection(None, AccountType.LEGACY)


def test_active_wallet_and_account_updates_are_atomic_and_network_scoped(
    tmp_path: Path,
) -> None:
    store = ApplicationSettingsStore(tmp_path / "settings.json")
    store.set_active_wallet("mainnet", "MainWallet")
    store.set_active_wallet("testnet4", "TestWallet")

    updated = store.set_active_account_type(
        "mainnet",
        AccountType.LEGACY,
        expected_wallet_name="MainWallet",
    )

    assert store.active_wallet("mainnet") == "MainWallet"
    assert store.active_account_type("mainnet") is AccountType.LEGACY
    assert updated.active_wallets["testnet4"] == ActiveWalletSelection(
        "TestWallet", AccountType.NATIVE_SEGWIT
    )
    assert store.active_wallet_selection("mainnet") == ActiveWalletSelection(
        "MainWallet", AccountType.LEGACY
    )


def test_switching_active_wallet_resets_account_type_to_native_segwit(
    tmp_path: Path,
) -> None:
    store = ApplicationSettingsStore(tmp_path / "settings.json")
    store.set_active_wallet("mainnet", "First")
    store.set_active_account_type(
        "mainnet",
        AccountType.LEGACY,
        expected_wallet_name="First",
    )

    store.set_active_wallet("mainnet", "Second")

    assert store.active_wallet_selection("mainnet") == ActiveWalletSelection(
        "Second", AccountType.NATIVE_SEGWIT
    )


def test_selection_guard_rejects_stale_update_without_changing_file(
    tmp_path: Path,
) -> None:
    store = ApplicationSettingsStore(tmp_path / "settings.json")
    store.set_active_wallet("mainnet", "Current")
    before = store.path.read_bytes()

    with pytest.raises(SettingsValidationError, match="active wallet changed"):
        store.set_active_wallet_selection(
            "mainnet",
            ActiveWalletSelection("Stale", AccountType.LEGACY),
            expected_wallet_name="Stale",
        )

    assert store.path.read_bytes() == before
    assert store.active_wallet_selection("mainnet") == ActiveWalletSelection(
        "Current", AccountType.NATIVE_SEGWIT
    )


def test_complete_selection_update_can_preserve_account_type_during_rename(
    tmp_path: Path,
) -> None:
    store = ApplicationSettingsStore(tmp_path / "settings.json")
    store.set_active_wallet("mainnet", "Before")
    store.set_active_account_type(
        "mainnet",
        AccountType.LEGACY,
        expected_wallet_name="Before",
    )

    store.set_active_wallet_selection(
        "mainnet",
        ActiveWalletSelection("After", AccountType.LEGACY),
        expected_wallet_name="Before",
    )

    assert store.active_wallet_selection("mainnet") == ActiveWalletSelection(
        "After", AccountType.LEGACY
    )


def test_default_settings_file_uses_mini_wallet_config_directory(monkeypatch) -> None:
    expected = Path("X:/mini-config")
    monkeypatch.setattr(app_settings, "user_config_path", lambda *args, **kwargs: expected)
    assert default_application_settings_file() == expected / "settings.json"
    assert default_wallet_data_dir() == expected
    assert default_settings().storage.wallet_data_dir == expected.resolve()


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


def test_new_mini_install_resolves_all_wallet_files_beside_settings(
    tmp_path: Path,
) -> None:
    settings_file = tmp_path / "mini-bitcoin-wallet" / "settings.json"
    store = ApplicationSettingsStore(settings_file)
    wallet_data_dir = store.load().storage.wallet_data_dir

    assert wallet_data_dir == settings_file.parent.resolve()
    assert default_wallet_file(wallet_data_dir, "mainnet") == (
        settings_file.parent.resolve() / "wallets.json"
    )
    assert default_wallet_cache_file(wallet_data_dir, "mainnet") == (
        settings_file.parent.resolve() / "wallet_cache.json"
    )
    assert default_wallet_file(wallet_data_dir, "testnet4") == (
        settings_file.parent.resolve() / "wallets_testnet4.json"
    )
    assert default_wallet_cache_file(wallet_data_dir, "testnet4") == (
        settings_file.parent.resolve() / "wallet_cache_testnet4.json"
    )


def test_first_run_storage_selection_is_non_destructive_and_deterministic(
    monkeypatch,
) -> None:
    with (
        TemporaryDirectory() as application,
        TemporaryDirectory() as platform,
        TemporaryDirectory() as legacy,
    ):
        monkeypatch.setenv("BITCOIN_TOOL_DATADIR", platform)
        application_path = Path(application).resolve()
        platform_path = Path(platform).resolve()
        legacy_path = Path(legacy)
        legacy_wallet = default_wallet_file(legacy_path, "testnet4")
        legacy_wallet.write_text("do not read or change this", encoding="utf-8")
        before = legacy_wallet.read_bytes()

        assert select_initial_wallet_data_dir(
            application_path, legacy_path
        ) == legacy_path.resolve()
        assert legacy_wallet.read_bytes() == before
        default_wallet_file(platform_path, "mainnet").write_text(
            "platform wallet", encoding="utf-8"
        )
        assert select_initial_wallet_data_dir(
            application_path, legacy_path
        ) == platform_path
        default_wallet_file(application_path, "testnet4").write_text(
            "application wallet", encoding="utf-8"
        )
        assert select_initial_wallet_data_dir(
            application_path, legacy_path
        ) == application_path
        assert legacy_wallet.read_bytes() == before


def test_first_run_storage_ignores_nonstandard_wallet_filename(monkeypatch) -> None:
    with (
        TemporaryDirectory() as application,
        TemporaryDirectory() as platform,
        TemporaryDirectory() as legacy,
    ):
        monkeypatch.setenv("BITCOIN_TOOL_DATADIR", platform)
        Path(legacy, "wallet_old.json").write_text("ignored", encoding="utf-8")
        assert select_initial_wallet_data_dir(
            application, legacy
        ) == Path(application).resolve()


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
