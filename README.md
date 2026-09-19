# Mini Bitcoin Wallet

Desktop Bitcoin wallet built with Python and Tkinter. It supports mainnet and
Testnet4 and uses the copied `bitcoin-tool` Platform API for wallet and payment
operations.

## Design principles

- Keep domain and use-case code readable without requiring Tkinter knowledge.
- UI depends on wallet use cases; wallet use cases do not depend on UI.
- New backends implement `WalletService`.
- Store amounts and fees as integer satoshis.

## Architecture

```text
app.py (composition root)
  ├─ pages.py / widgets.py / state.py       presentation (Tkinter)
  ├─ sync_coordinator.py                    background refresh policy
  ├─ wallet_core/application.py             use cases
  ├─ wallet_core/models.py + ports.py       domain values and interfaces
  ├─ adapters/bitcoin_tool_wallet.py        Platform DTO → GUI model mapping
  └─ wallet/service.py + tx/service.py      bitcoin-tool Platform API
```

The GUI adapter may import `wallet.service` and `tx.service`, but must not import
`wallet_cache` or `tx.workflow`. Wallet JSON parsing, address discovery,
synchronization, transaction accounting, UTXO reservations, and payment
lifecycles belong to those Platform services. The adapter stores only the
product's active-wallet preference and maps Platform DTOs to view models.

Product choices stay in the UI layer. For example, Platform transaction DTOs
contain `network` and `txid`; `explorer_links.py` turns them into the chosen
mempool.space URL.

## Wallet setup behavior

- Without a wallet, Home shows zero balance and no history; Deposit hides its
  address and QR code.
- Wallet creation and BIP39 import require a wallet name and password.
- Multiple wallets can be listed and switched without entering a password. The
  active wallet for each network is remembered in non-secret `settings.json`.
- Wallet Settings can display recovery words, rename a wallet, replace its
  password, or remove it after ownership confirmation.
- Imported wallets discover receive and change history independently until each
  branch reaches 20 consecutive unused addresses. Only history through the last
  used index is stored, followed by the next receive address; a normal sync then
  loads UTXOs, balance, and transactions.
- Balance and history include both receive and change addresses. Home displays
  effective balance, while transaction funding uses confirmed, available UTXOs.
- Transaction rows show block time and open a detail dialog with TXID copy and
  the correct mainnet or Testnet4 mempool.space link.
- Withdrawals synchronize the active wallet, build and sign an exact transaction
  for review, and broadcast only after explicit confirmation.
- Withdrawal UTXO synchronization preserves cached history. An accepted
  broadcast is added to Home immediately as an unconfirmed transaction.
- Max spends every eligible UTXO from the active wallet in one transaction; a
  cancelled review releases its temporary UTXO reservations.
- A wallet has at most one active payment draft. Preparing reserves a change
  candidate without advancing its index; cancel or signing failure releases it.
  Successful signing permanently issues that change address, even if the signed
  payment is later abandoned before broadcast.
- Preset fee rates are 0/1/2/3 sat/vB; Custom selects integer rates from 0 to 20.
  Review requires at least 1 sat/vB because zero-fee transactions are rejected by
  the reused bitcoin-tool workflow and standard relay policy.
- Home renders cached data immediately. A complete background synchronization
  runs once at startup, after every wallet selection, and on manual Refresh.
  Transaction preparation also performs its own UTXO synchronization before
  funding. There is no periodic foreground scan and no full scan immediately
  after broadcast.
- Broadcast transactions use the lightweight Esplora transaction-status endpoint
  every 30 seconds for ten minutes and every two minutes afterward. Polling stops
  when confirmation is detected, then one complete wallet synchronization
  refreshes balance and history. Status-query errors use exponential backoff
  capped at 15 minutes.
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
├── about_dialog.py
├── app.py
├── app_metadata.py
├── app-testnet4.py
├── build_mainnet.py
├── build_testnet4.py
├── app_settings.py
├── adapters/
├── icon/
├── btc/
├── network/
├── tx/
├── wallet/
├── wallet_core/
├── wallet_manager.py
├── wallet_settings_dialog.py
├── tests/
├── requirements.txt
└── README.md
```

## Test

```bash
python -m pytest -q
```
