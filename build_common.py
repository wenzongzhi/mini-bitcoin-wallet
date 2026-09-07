"""Shared, location-independent PyInstaller build pipeline."""

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys


PROJECT_DIRECTORY = Path(__file__).resolve().parent
ICON_DIRECTORY = PROJECT_DIRECTORY / "icon"
CREATE_ICON_SCRIPT = ICON_DIRECTORY / "create_icon.py"
WINDOWS_ICON_PATH = ICON_DIRECTORY / "bitcoin_tool_titlebar_icon.ico"
DIST_DIRECTORY = PROJECT_DIRECTORY / "dist"
BUILD_DIRECTORY = PROJECT_DIRECTORY / "build"
SPEC_DIRECTORY = BUILD_DIRECTORY / "specs"


@dataclass(frozen=True)
class BuildTarget:
    """Network-specific values; the build mechanism remains shared."""

    entry_filename: str
    executable_name: str

    @property
    def entry_path(self) -> Path:
        return PROJECT_DIRECTORY / self.entry_filename


def pyinstaller_command(target: BuildTarget) -> list[str]:
    """Create an argument list containing only absolute filesystem paths."""

    bundled_icon_directory = f"{ICON_DIRECTORY}{os.pathsep}icon"
    return [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "-w",
        "--name",
        target.executable_name,
        "--icon",
        str(WINDOWS_ICON_PATH),
        "--add-data",
        bundled_icon_directory,
        "--paths",
        str(PROJECT_DIRECTORY),
        "--distpath",
        str(DIST_DIRECTORY),
        "--workpath",
        str(BUILD_DIRECTORY / target.executable_name),
        "--specpath",
        str(SPEC_DIRECTORY),
        str(target.entry_path),
    ]


def build_executable(target: BuildTarget) -> Path:
    """Regenerate icons and build one windowed, single-file executable."""

    if not target.entry_path.is_file():
        raise FileNotFoundError(f"Entry point not found: {target.entry_path}")
    if not CREATE_ICON_SCRIPT.is_file():
        raise FileNotFoundError(f"Icon generator not found: {CREATE_ICON_SCRIPT}")

    SPEC_DIRECTORY.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [sys.executable, str(CREATE_ICON_SCRIPT)], cwd=PROJECT_DIRECTORY
    )
    subprocess.check_call(pyinstaller_command(target), cwd=PROJECT_DIRECTORY)

    executable = DIST_DIRECTORY / f"{target.executable_name}.exe"
    print(f"Build finished successfully: {executable}")
    return executable
