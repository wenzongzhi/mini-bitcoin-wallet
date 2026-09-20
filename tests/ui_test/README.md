# UI demo

Double-click `app-demo.py` on Windows, or run it from the project root:

```powershell
python tests/ui_test/app-demo.py
```

The launcher uses the real application UI with `DemoWalletService`, a deterministic
in-memory backend. It does not read wallet JSON files, use real private keys, or
broadcast transactions. All changes disappear when the window closes. Application
Settings are also written only to a temporary demo directory and cannot modify the
real Mini Bitcoin Wallet configuration.

The one explicit network action is **Settings → Network → Test Connection**. If
you click it, the dialog performs a read-only genesis and block-height check against
the displayed Esplora endpoint. Normal demo startup, wallet actions, Refresh, and
Save remain in memory and do not contact a blockchain backend.
