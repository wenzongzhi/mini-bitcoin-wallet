from pathlib import Path

import app


def test_fresh_install_keeps_wallet_data_beside_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "mini-bitcoin-wallet" / "settings.json"
    monkeypatch.setattr(
        app,
        "select_initial_wallet_data_dir",
        lambda application_dir, _legacy_dir: Path(application_dir).resolve(),
    )

    store = app.create_settings_store(
        settings_path=settings_path,
        legacy_data_directory=tmp_path / "empty-legacy-data",
    )

    assert store.wallet_data_dir() == settings_path.parent.resolve()


def test_legacy_wallet_directory_is_considered_only_on_first_v2_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "config" / "settings.json"
    legacy_directory = (tmp_path / "legacy-wallet-data").resolve()
    discovery_calls = []

    def discover(application_dir, candidate):
        assert Path(application_dir) == settings_path.parent
        discovery_calls.append(Path(candidate))
        return legacy_directory

    monkeypatch.setattr(app, "select_initial_wallet_data_dir", discover)
    created = app.create_settings_store(
        settings_path=settings_path,
        legacy_data_directory=legacy_directory,
    )

    assert created.wallet_data_dir() == legacy_directory
    assert discovery_calls == [legacy_directory]

    def unexpected_discovery(_application_dir, _legacy_dir):
        raise AssertionError("legacy discovery ran after settings already existed")

    monkeypatch.setattr(
        app,
        "select_initial_wallet_data_dir",
        unexpected_discovery,
    )
    reopened = app.create_settings_store(
        settings_path=settings_path,
        legacy_data_directory=tmp_path / "another-directory",
    )

    assert reopened.wallet_data_dir() == legacy_directory
