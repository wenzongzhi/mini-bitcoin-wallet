from pathlib import Path

import app


def test_legacy_wallet_directory_is_considered_only_on_first_v2_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "config" / "settings.json"
    legacy_directory = (tmp_path / "legacy-wallet-data").resolve()
    discovery_calls = []

    def discover(candidate, network):
        discovery_calls.append(Path(candidate))
        assert network == "mainnet"
        return legacy_directory

    monkeypatch.setattr(app, "detect_legacy_wallet_data_dir", discover)
    created = app.create_settings_store(
        network="mainnet",
        settings_path=settings_path,
        legacy_data_directory=legacy_directory,
    )

    assert created.wallet_data_dir() == legacy_directory
    assert discovery_calls == [legacy_directory]

    def unexpected_discovery(_candidate, _network):
        raise AssertionError("legacy discovery ran after settings already existed")

    monkeypatch.setattr(
        app,
        "detect_legacy_wallet_data_dir",
        unexpected_discovery,
    )
    reopened = app.create_settings_store(
        network="mainnet",
        settings_path=settings_path,
        legacy_data_directory=tmp_path / "another-directory",
    )

    assert reopened.wallet_data_dir() == legacy_directory
