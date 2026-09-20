from pathlib import Path

import pytest

from app_settings import (
    ApplicationSettingsStore,
    BackendSettings,
    GeneralSettings,
    SettingsValidationError,
    StorageSettings,
)
from network import EsploraError
from settings_dialog import (
    SettingsController,
    SettingsDraft,
    verify_backend_connection,
)


@pytest.mark.parametrize(
    ("network", "expected_url"),
    [
        ("mainnet", "https://blockstream.info/api"),
        ("testnet4", "https://mempool.space/testnet4/api"),
    ],
)
def test_default_backend_endpoint_comes_from_platform_chain_params(
    network,
    expected_url,
):
    received = {}

    class Backend:
        def verify_network(self):
            pass

        def get_tip_height(self):
            return 1

    def builder(**kwargs):
        received.update(kwargs)
        return Backend()

    result = verify_backend_connection(
        network,
        BackendSettings(),
        backend_builder=builder,
    )

    assert received == {"network": network}
    assert result.endpoint == expected_url


class FakeState:
    def __init__(self, *, initialized=True):
        self.is_initialized = initialized
        self.applied = []
        self.refresh_count = 0

    def apply_general_settings(self, general):
        self.applied.append(general)

    def request_wallet_refresh(self):
        self.refresh_count += 1


def make_controller(tmp_path: Path, *, initialized=True):
    store = ApplicationSettingsStore(tmp_path / "settings.json")
    store.load_or_create()
    state = FakeState(initialized=initialized)
    return SettingsController(store, "mainnet", state), store, state


def test_save_persists_complete_draft_and_applies_general_settings(tmp_path):
    controller, store, state = make_controller(tmp_path)
    draft = SettingsDraft(
        general=GeneralSettings(
            display_unit="sats",
            fiat_currency="CNY",
            theme="dark",
            hide_balance=True,
        ),
        backend=BackendSettings(),
        storage=StorageSettings(),
    )

    result = controller.save(draft)

    saved = store.load()
    assert saved.general == draft.general
    assert saved.network["mainnet"] == draft.backend
    assert state.applied == [draft.general]
    assert result.general_changed is True
    assert result.restart_required is False


def test_cancel_does_not_write_settings(tmp_path):
    controller, store, state = make_controller(tmp_path)
    before = store.path.read_text(encoding="utf-8")

    controller.cancel()

    assert store.path.read_text(encoding="utf-8") == before
    assert state.applied == []
    assert state.refresh_count == 0


def test_backend_test_does_not_save_settings(tmp_path):
    controller, store, _state = make_controller(tmp_path)
    before = store.path.read_text(encoding="utf-8")
    backend = BackendSettings(
        backend_mode="custom",
        custom_esplora_url="https://node.example/api",
    )

    class Backend:
        def verify_network(self):
            pass

        def get_tip_height(self):
            return 123

    verify_backend_connection(
        "mainnet",
        backend,
        backend_builder=lambda **_kwargs: Backend(),
    )

    assert store.path.read_text(encoding="utf-8") == before
    assert controller.is_backend_verified(backend) is False


def test_changed_custom_backend_requires_successful_test(tmp_path):
    controller, _store, _state = make_controller(tmp_path)
    backend = BackendSettings(
        backend_mode="custom",
        custom_esplora_url="https://node.example/api",
    )
    draft = SettingsDraft(
        general=controller.original.general,
        backend=backend,
        storage=controller.original.storage,
    )

    with pytest.raises(SettingsValidationError, match="Test this custom backend"):
        controller.save(draft)

    controller.mark_backend_verified(backend)
    controller.save(draft)


def test_storage_change_marks_restart_required(tmp_path):
    controller, _store, _state = make_controller(tmp_path)
    wallet_data_dir = tmp_path / "wallet-data"
    wallet_data_dir.mkdir()
    (wallet_data_dir / "wallets.json").write_text("{}", encoding="utf-8")
    draft = SettingsDraft(
        general=controller.original.general,
        backend=controller.original.backend,
        storage=StorageSettings(wallet_data_dir),
    )

    result = controller.save(draft)

    assert result.restart_required is True


def test_missing_custom_wallet_file_is_rejected_at_save(tmp_path):
    controller, _store, state = make_controller(tmp_path)
    draft = SettingsDraft(
        general=controller.original.general,
        backend=controller.original.backend,
        storage=StorageSettings(tmp_path / "missing-wallet-data"),
    )

    with pytest.raises(SettingsValidationError, match="does not exist"):
        controller.save(draft)

    assert state.applied == []


def test_backend_change_requests_refresh_for_active_wallet(tmp_path):
    controller, _store, state = make_controller(tmp_path, initialized=True)
    backend = BackendSettings(
        backend_mode="custom",
        custom_esplora_url="http://localhost:3006/api",
    )
    controller.mark_backend_verified(backend)
    controller.save(
        SettingsDraft(
            general=controller.original.general,
            backend=backend,
            storage=controller.original.storage,
        )
    )

    assert state.refresh_count == 1


def test_backend_change_without_wallet_does_not_request_refresh(tmp_path):
    controller, _store, state = make_controller(tmp_path, initialized=False)
    backend = BackendSettings(
        backend_mode="custom",
        custom_esplora_url="http://localhost:3006/api",
    )
    controller.mark_backend_verified(backend)
    controller.save(
        SettingsDraft(
            general=controller.original.general,
            backend=backend,
            storage=controller.original.storage,
        )
    )

    assert state.refresh_count == 0


def test_verify_backend_connection_checks_network_then_tip_height():
    calls = []

    class Backend:
        def verify_network(self):
            calls.append("verify")

        def get_tip_height(self):
            calls.append("height")
            return 876_543

    received = {}

    def builder(**kwargs):
        received.update(kwargs)
        return Backend()

    result = verify_backend_connection(
        "testnet4",
        BackendSettings(
            backend_mode="custom",
            custom_esplora_url="http://192.168.1.8:3006/api/",
        ),
        backend_builder=builder,
    )

    assert calls == ["verify", "height"]
    assert received == {
        "base_url": "http://192.168.1.8:3006/api",
        "network": "testnet4",
    }
    assert result.block_height == 876_543


@pytest.mark.parametrize(
    "error",
    [
        EsploraError('Esplora backend is not connected to network "mainnet"'),
        EsploraError("connection refused"),
    ],
)
def test_verify_backend_connection_propagates_wrong_network_and_connection_errors(error):
    class Backend:
        def verify_network(self):
            raise error

        def get_tip_height(self):
            pytest.fail("tip height must not be requested after verification failure")

    with pytest.raises(EsploraError, match=str(error)):
        verify_backend_connection(
            "mainnet",
            BackendSettings(),
            backend_builder=lambda **_kwargs: Backend(),
        )
