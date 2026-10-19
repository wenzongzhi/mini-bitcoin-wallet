from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest

import build_common
from build_common import BuildTarget, PROJECT_DIRECTORY, pyinstaller_command
from build_mainnet import MAINNET_TARGET
from build_testnet4 import TESTNET4_TARGET


@pytest.fixture(params=["win32", "linux"])
def build_platform(request, monkeypatch):
    """Exercise native build policy without changing the test runner's OS."""

    platform_name = request.param
    monkeypatch.setattr(
        build_common,
        "sys",
        SimpleNamespace(platform=platform_name, executable=sys.executable),
    )
    monkeypatch.setattr(
        build_common,
        "os",
        SimpleNamespace(pathsep=";" if platform_name == "win32" else ":"),
    )
    return platform_name


@pytest.mark.parametrize("target", [MAINNET_TARGET, TESTNET4_TARGET])
def test_native_build_command_from_another_working_directory(
    target, build_platform, tmp_path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    command = pyinstaller_command(target)

    assert command[:3] == [sys.executable, "-m", "PyInstaller"]
    assert "-w" in command
    assert "--onefile" in command
    assert Path(command[-1]) == PROJECT_DIRECTORY / target.entry_filename
    for option in ("--paths", "--distpath", "--workpath", "--specpath"):
        assert Path(command[command.index(option) + 1]).is_absolute()
    separator = ";" if build_platform == "win32" else ":"
    assert command[command.index("--add-data") + 1] == (
        f"{PROJECT_DIRECTORY / 'icon'}{separator}icon"
    )
    assert command[command.index("--collect-binaries") + 1] == "coincurve"
    hidden_imports = {
        command[index + 1]
        for index, option in enumerate(command[:-1])
        if option == "--hidden-import"
    }
    assert {"_cffi_backend", "PIL._tkinter_finder"} <= hidden_imports
    if build_platform == "win32":
        assert command[command.index("--icon") + 1] == str(
            build_common.WINDOWS_ICON_PATH
        )
    else:
        assert "--icon" not in command


@pytest.fixture
def build_project(tmp_path, monkeypatch):
    """Make isolated build inputs, including spaces in the project path."""

    project = tmp_path / "wallet source"
    icon_directory = project / "icon"
    icon_directory.mkdir(parents=True)
    for filename in ("bitcoin_logo.png", "bitcoin_titlebar_icon.png"):
        (icon_directory / filename).touch()
    for filename in ("app.py", "app-testnet4.py"):
        (project / filename).touch()
    for name, path in {
        "PROJECT_DIRECTORY": project,
        "ICON_DIRECTORY": icon_directory,
        "CREATE_ICON_SCRIPT": icon_directory / "create_icon.py",
        "WINDOWS_ICON_PATH": icon_directory / "bitcoin_tool_titlebar_icon.ico",
        "DIST_DIRECTORY": project / "dist",
        "BUILD_DIRECTORY": project / "build",
        "SPEC_DIRECTORY": project / "build" / "specs",
    }.items():
        monkeypatch.setattr(build_common, name, path)
    return project


@pytest.mark.parametrize("target", [MAINNET_TARGET, TESTNET4_TARGET])
def test_build_returns_native_output_and_only_generates_icons_on_windows(
    target, build_platform, build_project, tmp_path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    if build_platform == "win32":
        build_common.CREATE_ICON_SCRIPT.touch()
    expected_name = target.executable_name
    if build_platform == "win32":
        expected_name += ".exe"
    expected_output = build_project / "dist" / expected_name
    calls = []

    def build_step(command, *, cwd):
        assert cwd == build_project
        calls.append(command)
        if command[1] == "-m":
            expected_output.parent.mkdir(parents=True, exist_ok=True)
            expected_output.touch()
        else:
            assert command == [
                sys.executable,
                str(build_common.CREATE_ICON_SCRIPT),
            ]
            build_common.WINDOWS_ICON_PATH.touch()

    monkeypatch.setattr(build_common.subprocess, "check_call", build_step)

    assert build_common.build_executable(target) == expected_output
    assert len(calls) == (2 if build_platform == "win32" else 1)
    assert calls[-1] == pyinstaller_command(target)
    if build_platform == "linux":
        assert not build_common.CREATE_ICON_SCRIPT.exists()
        assert not build_common.WINDOWS_ICON_PATH.exists()


def test_build_reports_missing_output_instead_of_claiming_success(
    build_project, monkeypatch,
) -> None:
    monkeypatch.setattr(build_common, "_prepare_icon_assets", lambda: None)
    monkeypatch.setattr(build_common.subprocess, "check_call", lambda *a, **kw: None)

    with pytest.raises(FileNotFoundError, match="expected executable"):
        build_common.build_executable(MAINNET_TARGET)


def test_pyinstaller_failure_propagates_even_when_an_older_output_exists(
    build_project, monkeypatch,
) -> None:
    output = build_project / "dist" / build_common.executable_filename("wallet")
    output.parent.mkdir()
    output.touch()
    monkeypatch.setattr(build_common, "_prepare_icon_assets", lambda: None)

    def fail(command, **_options):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(build_common.subprocess, "check_call", fail)

    with pytest.raises(subprocess.CalledProcessError):
        build_common.build_executable(BuildTarget("app.py", "wallet"))
