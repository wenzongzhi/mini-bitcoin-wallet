"""Run Mini Bitcoin Wallet against Bitcoin Testnet4."""

from app import run_wallet_app
from btc.chainparams import NETWORK_TESTNET4


def main():
    # Testnet4 has isolated wallet and cache files, so it can never read or
    # overwrite the mainnet wallet selected by app.py.
    run_wallet_app(
        network=NETWORK_TESTNET4,
        wallet_filename="wallets_testnet4.json",
        cache_filename="wallet_cache_testnet4.json",
        window_title="Bitcoin Wallet — TESTNET4",
    )


if __name__ == "__main__":
    main()
