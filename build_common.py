"""Build native executables on Windows and Linux, including Raspberry Pi.

PyInstaller uses the current Python environment and host architecture. Run this
pipeline on the target system; a Windows build cannot produce a Linux ARM64 ELF.
"""

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


def executable_filename(name: str) -> str:
    """Windows executables use .exe; Linux ELF files have no added suffix."""

    return f"{name}.exe" if sys.platform == "win32" else name


def pyinstaller_command(target: BuildTarget) -> list[str]:
    """Create native build arguments with absolute source and output paths."""

    bundled_icon_directory = f"{ICON_DIRECTORY}{os.pathsep}icon"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        # PyInstaller ignores -w on Linux; it selects GUI mode on Windows.
        "-w",
        "--name",
        target.executable_name,
        # Include bundled/shared secp256k1 libraries from source-built installs
        # as well as wheels. CFFI's extension import is invisible to analysis.
        "--collect-binaries",
        "coincurve",
        "--hidden-import",
        "_cffi_backend",
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
    ]
    if sys.platform == "win32":
        command.extend(["--icon", str(WINDOWS_ICON_PATH)])
    command.append(str(target.entry_path))
    return command


def _prepare_icon_assets() -> None:
    """Regenerate Windows icons; use checked-in runtime PNGs on Linux."""

    required_assets = [
        ICON_DIRECTORY / "bitcoin_logo.png",
        ICON_DIRECTORY / "bitcoin_titlebar_icon.png",
    ]
    if sys.platform == "win32":
        if not CREATE_ICON_SCRIPT.is_file():
            raise FileNotFoundError(f"Icon generator not found: {CREATE_ICON_SCRIPT}")
        subprocess.check_call(
            [sys.executable, str(CREATE_ICON_SCRIPT)], cwd=PROJECT_DIRECTORY
        )
        required_assets.append(WINDOWS_ICON_PATH)
    for asset in required_assets:
        if not asset.is_file():
            raise FileNotFoundError(f"Required application icon not found: {asset}")


def build_executable(target: BuildTarget) -> Path:
    """Build one native, single-file executable and verify that it exists."""

    if not target.entry_path.is_file():
        raise FileNotFoundError(f"Entry point not found: {target.entry_path}")

    _prepare_icon_assets()
    SPEC_DIRECTORY.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(pyinstaller_command(target), cwd=PROJECT_DIRECTORY)

    executable = DIST_DIRECTORY / executable_filename(target.executable_name)
    if not executable.is_file():
        raise FileNotFoundError(
            f"PyInstaller finished without producing the expected executable: {executable}"
        )
    print(f"Build finished successfully: {executable}")
    return executable
