"""Build the mainnet wallet as a windowed Windows executable."""

from build_common import BuildTarget, build_executable


MAINNET_TARGET = BuildTarget(
    entry_filename="app.py",
    executable_name="mini_bitcoin_wallet",
)


if __name__ == "__main__":
    build_executable(MAINNET_TARGET)
