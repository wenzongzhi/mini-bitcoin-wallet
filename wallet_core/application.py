"""Use cases consumed by the desktop UI."""

from __future__ import annotations

from .models import BitcoinAmount, DisplayUnit, SendPreview, WalletCreation, WalletSnapshot
from .ports import WalletService


class WalletApplication:
    """Coordinates wallet use cases while keeping Tkinter out of the core."""

    def __init__(self, service: WalletService):
        self._service = service

    def load_wallet(self) -> WalletSnapshot:
        return self._service.snapshot()

    def rename_wallet(self, name: str) -> WalletSnapshot:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Wallet name cannot be empty.")
        return self._service.rename_wallet(normalized)

    def create_wallet(
        self, name: str, password: str, mnemonic: str | None = None
    ) -> WalletCreation:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Wallet name cannot be empty.")
        if not password:
            raise ValueError("Password cannot be empty.")
        normalized_mnemonic = " ".join(mnemonic.strip().split()) if mnemonic else None
        return self._service.create_wallet(
            normalized_name,
            password,
            normalized_mnemonic,
        )

    def preview_send(
        self,
        destination: str,
        amount_text: str,
        unit: DisplayUnit,
        fee_rate_sat_vb: int,
        *,
        send_all: bool = False,
    ) -> SendPreview:
        normalized_destination = destination.strip()
        if not normalized_destination:
            raise ValueError("Enter a destination address.")
        if isinstance(fee_rate_sat_vb, bool) or not 1 <= fee_rate_sat_vb <= 100:
            raise ValueError("Fee rate must be between 1 and 100 sat/vB.")
        amount = None if send_all else BitcoinAmount.parse(amount_text, unit)
        return self._service.preview_send(
            normalized_destination,
            amount,
            fee_rate_sat_vb,
            send_all=send_all,
        )
