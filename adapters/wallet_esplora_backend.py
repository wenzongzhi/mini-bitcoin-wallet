"""Mini-wallet-specific Esplora extensions.

The copied bitcoin-tool backend deliberately remains untouched.  Capabilities
needed only by the desktop synchronization policy live in this subclass.
"""

from __future__ import annotations

import re

from network import EsploraBackend, EsploraError


class WalletEsploraBackend(EsploraBackend):
    """Esplora backend with a lightweight transaction-status endpoint."""

    def get_transaction_status(self, txid: str) -> dict:
        if not isinstance(txid, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
            raise EsploraError("transaction id must be 64 hexadecimal characters")
        status = self._get_json(f"/tx/{txid.lower()}/status")
        if not isinstance(status, dict) or not isinstance(status.get("confirmed"), bool):
            raise EsploraError("invalid transaction status response")
        return status
