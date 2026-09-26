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
  ├─ app_settings.py / settings_dialog.py   product settings
  ├─ sync_coordinator.py                    background refresh policy
  ├─ wallet_core/application.py             use cases
  ├─ wallet_core/models.py + ports.py       domain values and interfaces
  ├─ adapters/bitcoin_tool_wallet.py        Platform DTO → GUI model mapping
  └─ wallet/service.py + tx/service.py      bitcoin-tool Platform API
```

The GUI adapter may import `wallet.service` and `tx.service`, but must not import
`wallet_cache` or `tx.workflow`. Wallet JSON parsing, address discovery,
synchronization, transaction accounting, UTXO reservations, and payment
lifecycles belong to those Platform services. The adapter receives the shared
application settings dependency and maps Platform DTOs to view models.

Product choices stay in the UI layer. For example, Platform transaction DTOs
contain `network` and `txid`; `explorer_links.py` turns them into the chosen
mempool.space URL.

`wallets.json` is authoritative for wallet identity, names, keys, and address
lifecycle. `wallet_cache.json` is non-authoritative and rebuildable: stale cache
entries can never create or restore a wallet, or override authoritative wallet
identity. It may also contain transient payment reservations and pending
summaries, so Platform maintenance isolates the affected entry instead of
discarding unrelated wallet state. Product code must not parse or mutate either
file directly.

## Wallet setup behavior

- Without a wallet, Home shows zero balance and no history; Deposit hides its
  address and QR code.
- Wallet creation and BIP39 import require a wallet name and password.
- Multiple wallets can be listed and switched without entering a password. The
  active wallet for each network is remembered in non-secret application
  settings.
- New and imported wallets start with the Native SegWit account (BIP84). Wallet
  Settings can switch the active account to Legacy (BIP44); Nested SegWit and
  Taproot are visible as future options but cannot yet be selected.
- When the BIP44 address book is still empty, the first Legacy selection
  performs gap-limit discovery for both receive and change branches. Later
  synchronization scans every account that has been enabled. Home shows their
  aggregate balance and history, while Deposit and Withdrawal use only the
  currently selected account type.
- Account selection is remembered independently for Mainnet and Testnet4.
  Selecting a different wallet resets its account type to Native SegWit;
  renaming the active wallet preserves its current account type.
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

## Application settings

The Settings dialog contains only General, Network, and Storage:

- General persists BTC/sats display, fiat currency, System/Light/Dark theme,
  and balance privacy.
- Network selects the default Platform backend or a custom Esplora-compatible
  endpoint independently for Mainnet and Testnet4. A custom endpoint must pass
  a background genesis/network check before it can be saved.
- Storage uses Mini Bitcoin Wallet's application directory by default, or a
  user-selected directory containing the network's standard wallet filename.
  Storage changes take effect after restart; cache paths remain
  Platform-managed.

`settings.json` uses a strict schema and is stored in Mini Bitcoin Wallet's
stable `platformdirs` configuration directory. Each network stores one complete
active selection: `wallet_name` plus `account_type` (`p2wpkh` or `p2pkh`). A
valid settings file from the immediately previous schema is upgraded atomically
with Native SegWit selected; malformed or unsupported schemas are rejected
without rewriting the source file.

New installations keep `wallets.json`, `wallet_cache.json`,
`wallets_testnet4.json`, and `wallet_cache_testnet4.json` in that same
directory. Settings contains no mnemonic, password, private key, or wallet
JSON.

On first launch, existing data is protected using this directory priority: Mini
Bitcoin Wallet default, bitcoin-tool default, then the older source/EXE
directory. Both networks are checked. The chosen directory is only referenced;
wallet files are never moved, copied, merged, or deleted automatically.

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

It uses the Platform-standard `wallets_testnet4.json` and
`wallet_cache_testnet4.json`; mainnet files are never reused. The window title
is marked `TESTNET4`.

Build windowed, single-file executables from any working directory with:

```bash
python build_mainnet.py
python build_testnet4.py
```

The outputs are `dist/mini_bitcoin_wallet.exe` and
`dist/mini_bitcoin_wallet_testnet4.exe`. New packaged installations keep wallet
and cache files beside the application settings in Mini Bitcoin Wallet's user
configuration directory; Settings → Storage can select another directory
containing the standard wallet filename.

## Update the vendored Platform

The `btc/`, `network/`, `wallet/`, and `tx/` directories are copied from the
bitcoin-tool commit pinned in `tools/platform_upstream.json`. Update them only
with the local synchronization tool; it cannot overwrite product-owned
directories.

See [tools/README.md](tools/README.md) for the complete command-line workflow,
safety rules, exit codes, and troubleshooting guide.

```bash
# 1. Complete and test the bitcoin-tool change, then commit it.
# 2. Check out the intended bitcoin-tool branch/commit.
python tools/sync_bitcoin_tool_platform.py --source ../bitcoin-tool
python tools/sync_bitcoin_tool_platform.py --source ../bitcoin-tool --check
python -m pytest -q
```

Commit the four vendored directories and `tools/platform_upstream.json`
together. The tool rejects uncommitted changes inside the upstream Platform
directories so the recorded commit always reproduces the copied source.
Changes elsewhere in the upstream checkout do not block synchronization.

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
├── settings_dialog.py
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
├── tools/
│   ├── README.md
│   ├── platform_upstream.json
│   └── sync_bitcoin_tool_platform.py
├── requirements.txt
└── README.md
```

## Test

```bash
python -m pytest -q
```
