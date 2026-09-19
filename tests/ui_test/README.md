# UI demo

Double-click `app-demo.py` on Windows, or run it from the project root:

```powershell
python tests/ui_test/app-demo.py
```

The launcher uses the real application UI with `DemoWalletService`, a deterministic
in-memory backend. It does not read wallet JSON files, contact a blockchain backend,
use real private keys, or broadcast transactions. All changes disappear when the
window closes.
