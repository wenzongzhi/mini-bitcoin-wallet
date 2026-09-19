"""Launch the desktop UI with deterministic in-memory wallet data.

This file is intentionally self-contained so it can be started by double-clicking
it in Windows Explorer, where the project root is not necessarily the working
directory.
"""

from pathlib import Path
import sys


PROJECT_DIRECTORY = Path(__file__).resolve().parents[2]
# Keep the project ahead of globally installed packages with generic names such
# as ``tests`` when the script is launched outside the repository directory.
sys.path.insert(0, str(PROJECT_DIRECTORY))

from app import BitcoinWalletApp  # noqa: E402
from tests.ui_test.demo_wallet import DemoWalletService  # noqa: E402


def main() -> None:
    """Run the real UI against a disposable in-memory wallet backend."""

    service = DemoWalletService()
    BitcoinWalletApp(
        service,
        window_title="Bitcoin Wallet — UI DEMO",
    ).mainloop()


if __name__ == "__main__":
    main()
