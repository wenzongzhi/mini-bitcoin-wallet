"""Build the isolated Testnet4 wallet as a windowed Windows executable."""

from build_common import BuildTarget, build_executable


TESTNET4_TARGET = BuildTarget(
    entry_filename="app-testnet4.py",
    executable_name="mini_bitcoin_wallet_testnet4",
)


if __name__ == "__main__":
    build_executable(TESTNET4_TARGET)
