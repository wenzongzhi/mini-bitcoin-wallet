"""Tkinter-facing state for the wallet screens.

This module adapts framework-independent use cases to ``tk.Variable`` objects.
Business rules live in :mod:`wallet_core`; pages only coordinate presentation.
"""

import tkinter as tk

from wallet_core import (
    BitcoinAmount,
    DisplayUnit,
    SendPreview,
    WalletApplication,
    WalletCreation,
    WalletSummary,
)


class WalletUIState:
    def __init__(self, root: tk.Misc, application: WalletApplication):
        self.application = application
        self._snapshot = application.load_wallet()
        self._wallets = application.list_wallets()

        self.display_unit = tk.StringVar(root, value=DisplayUnit.BTC.value)
        self.wallet_name = tk.StringVar(root, value=self._snapshot.name)
        self.receive_address = tk.StringVar(root, value=self._snapshot.receive_address)
        self.amount = tk.StringVar(root, value="")
        self.address = tk.StringVar(root, value="")
        self.send_all = tk.BooleanVar(root, value=False)
        self.custom_fee = tk.BooleanVar(root, value=False)
        self.fiat_currency = tk.StringVar(root, value="USD")
        self.dark_mode = tk.BooleanVar(root, value=False)
        self.revision = tk.IntVar(root, value=0)

    @property
    def transactions(self):
        return self._snapshot.transactions

    @property
    def wallets(self) -> tuple[WalletSummary, ...]:
        return self._wallets

    @property
    def active_wallet_name(self) -> str | None:
        return self._snapshot.name if self._snapshot.is_initialized else None

    @property
    def unit(self) -> DisplayUnit:
        return DisplayUnit(self.display_unit.get())

    @property
    def is_initialized(self) -> bool:
        return self._snapshot.is_initialized

    def formatted_balance(self) -> tuple[str, str]:
        return self._snapshot.balance.format(self.unit), self.unit.value

    def rename_wallet(self, name: str) -> None:
        self._snapshot = self.application.rename_wallet(name)
        self.wallet_name.set(self._snapshot.name)

    def create_wallet(
        self, name: str, password: str, mnemonic: str | None = None
    ) -> WalletCreation:
        creation = self.application.create_wallet(name, password, mnemonic)
        self._apply_snapshot(creation.snapshot)
        return creation

    def select_wallet(self, name: str) -> None:
        self._apply_snapshot(self.application.select_wallet(name))

    def apply_wallet_creation(self, creation: WalletCreation) -> None:
        """Apply a wallet created by a background application use case."""

        self._apply_snapshot(creation.snapshot)

    def reload_wallet(self) -> None:
        """Reload persistent state after a partially completed operation."""

        self._apply_snapshot(self.application.load_wallet())

    def _apply_snapshot(self, snapshot) -> None:
        self._snapshot = snapshot
        self._wallets = self.application.list_wallets()
        self.wallet_name.set(snapshot.name)
        self.receive_address.set(snapshot.receive_address)
        self.revision.set(self.revision.get() + 1)

    def amount_is_valid(self) -> bool:
        if self.send_all.get():
            return True
        try:
            # Parsing here gives immediate feedback. The use case repeats the
            # check at its own trust boundary before creating a preview.
            BitcoinAmount.parse(self.amount.get(), self.unit)
            return True
        except ValueError:
            return False

    def preview_send(self, fee_rate_sat_vb: int) -> SendPreview:
        return self.application.preview_send(
            self.address.get(),
            self.amount.get(),
            self.unit,
            fee_rate_sat_vb,
            send_all=self.send_all.get(),
        )

    def fiat_zero_text(self) -> str:
        return {
            "USD": "US$0.00",
            "JPY": "¥0",
            "CNY": "CN¥0.00",
            "EUR": "€0.00",
        }.get(self.fiat_currency.get(), f"{self.fiat_currency.get()} 0.00")
