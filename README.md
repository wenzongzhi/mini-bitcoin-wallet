# Mini Bitcoin Wallet

Desktop Bitcoin wallet built with Python and Tkinter. It supports mainnet and
Testnet4 while reusing the wallet, network, and Bitcoin primitives copied from
`bitcoin-tool`.

## Design principles

- Keep domain and use-case code readable without requiring Tkinter knowledge.
- UI depends on wallet use cases; wallet use cases do not depend on UI.
- New backends implement `WalletService`.
- Store amounts and fees as integer satoshis.

## Architecture

```text
app.py (composition root)
  ├─ pages.py / widgets.py / state.py       presentation (Tkinter)
  ├─ wallet_core/application.py             use cases
  ├─ wallet_core/models.py + ports.py       domain values and interfaces
  ├─ adapters/bitcoin_tool_wallet.py        production adapter
  └─ btc/ + wallet/ + network/              copied bitcoin-tool implementation
```

The copied `bitcoin-tool` packages are accessed through
`BitcoinToolWalletService`; the upstream project is not modified. The adapter
owns network selection, wallet paths, synchronization, and conversion to domain
models. New features should be added in this order: domain model, service port,
application use case, UI state, then page or widget.

## Wallet setup behavior

- Without a wallet, Home shows zero balance and no history; Deposit hides its
  address and QR code.
- Wallet creation and BIP39 import require a wallet name and password.
- Multiple wallets can be listed and switched without entering a password. The
  active wallet for each network is remembered in non-secret `settings.json`.
- Imported wallets scan 20 receive and 20 change addresses. The next unused
  receive address is displayed after synchronization.
- Balance and history include both receive and change addresses.
- Transaction rows show block time and open a detail dialog with TXID copy and
  the correct mainnet or Testnet4 mempool.space link.
- Mainnet and Testnet4 use separate wallet, cache, and lock files.

Mnemonic discovery queries a public Esplora service and may reveal scanned
addresses to that service.

## Run

```bash
python -m pip install -r requirements.txt
python app.py
```

Run the isolated Testnet4 wallet with:

```bash
python app-testnet4.py
```

It uses `wallets_testnet4.json` and `wallet_cache_testnet4.json`; mainnet data is
never reused. The window title is marked `TESTNET4`.

Build windowed, single-file executables from any working directory with:

```bash
python build_mainnet.py
python build_testnet4.py
```

The outputs are `dist/mini_bitcoin_wallet.exe` and `dist/mini_bitcoin_wallet_testnet4.exe`. Packaged wallets and
caches are stored beside the executable, keeping mainnet and Testnet4 isolated.

## Structure

```text
mini-bitcoin-wallet/
├── app.py
├── app-testnet4.py
├── build_mainnet.py
├── build_testnet4.py
├── app_settings.py
├── adapters/
├── icon/
├── btc/
├── network/
├── wallet/
├── wallet_core/
├── wallet_manager.py
├── tests/
├── requirements.txt
└── README.md
```

## Test

```bash
python -m pytest -q
```
