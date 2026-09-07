from pathlib import Path

from build_common import BuildTarget, PROJECT_DIRECTORY, pyinstaller_command


def test_pyinstaller_build_is_windowed_and_uses_absolute_paths() -> None:
    target = BuildTarget("app.py", "app")
    command = pyinstaller_command(target)

    assert "-w" in command
    assert "--onefile" in command
    assert Path(command[-1]).is_absolute()
    assert Path(command[-1]) == PROJECT_DIRECTORY / "app.py"


def test_pyinstaller_bundles_the_icon_directory() -> None:
    command = pyinstaller_command(BuildTarget("app-testnet4.py", "app-testnet4"))
    add_data_value = command[command.index("--add-data") + 1]

    assert str(PROJECT_DIRECTORY / "icon") in add_data_value
    assert add_data_value.endswith("icon")
