"""Tkinter-facing state for the wallet screens.

This module adapts framework-independent use cases to ``tk.Variable`` objects.
Business rules live in :mod:`wallet_core`; pages only coordinate presentation.
"""

import tkinter as tk

from wallet_core import (
    BitcoinAmount,
    DisplayUnit,
    WalletApplication,
    WalletCreation,
    WalletSnapshot,
    WalletSummary,
    WithdrawalDraft,
    WithdrawalReview,
    BroadcastResult,
)


class WalletUIState:
    def __init__(self, root: tk.Misc, application: WalletApplication):
        self.root = root
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
        self.sync_status = tk.StringVar(root, value="")
        self.syncing = tk.BooleanVar(root, value=False)
        self.revision = tk.IntVar(root, value=0)

    @property
    def transactions(self):
        return self._snapshot.transactions

    @property
    def synced_at(self):
        return self._snapshot.synced_at

    @property
    def monitored_transaction_ids(self) -> tuple[str, ...]:
        """Return transactions broadcast by this wallet that await confirmation."""

        return tuple(dict.fromkeys(self._snapshot.pending_txids))

    @property
    def wallets(self) -> tuple[WalletSummary, ...]:
        return self._wallets

    @property
    def active_wallet_name(self) -> str | None:
        return self._snapshot.name if self._snapshot.is_initialized else None

    @property
    def active_wallet_encrypted(self) -> bool:
        return next(
            (
                wallet.encrypted
                for wallet in self._wallets
                if wallet.name == self.active_wallet_name
            ),
            False,
        )

    @property
    def unit(self) -> DisplayUnit:
        return DisplayUnit(self.display_unit.get())

    @property
    def is_initialized(self) -> bool:
        return self._snapshot.is_initialized

    def formatted_balance(self) -> tuple[str, str]:
        return self._snapshot.balance.format(self.unit), self.unit.value

    def get_mnemonic(self, password: str | None) -> str:
        return self.application.get_mnemonic(password)

    def rename_wallet(self, name: str, password: str | None = None) -> None:
        self._apply_snapshot(self.application.rename_wallet(name, password))
        # The active identity changed.  Refresh scheduling belongs to the
        # product layer, so request a normal background synchronization that
        # also rebuilds any cache entry the Platform had to ignore.
        self._announce("<<ActiveWalletChanged>>")

    def change_password(
        self,
        current_password: str | None,
        new_password: str,
    ) -> None:
        self._apply_snapshot(
            self.application.change_password(current_password, new_password)
        )

    def remove_wallet(self, password: str | None) -> None:
        self.amount.set("")
        self.address.set("")
        self.send_all.set(False)
        self._apply_snapshot(self.application.remove_wallet(password))
        self._announce("<<ActiveWalletChanged>>")

    def create_wallet(self, name: str, password: str) -> WalletCreation:
        creation = self.application.create_wallet(name, password)
        self._apply_snapshot(creation.snapshot)
        self._announce("<<ActiveWalletChanged>>")
        return creation

    def select_wallet(self, name: str) -> None:
        snapshot = self.application.select_wallet(name)
        self.amount.set("")
        self.address.set("")
        self.send_all.set(False)
        self._apply_snapshot(snapshot)
        self._announce("<<ActiveWalletChanged>>")

    def apply_wallet_import(self, snapshot: WalletSnapshot) -> None:
        """Apply an imported wallet after its background worker completes."""

        self._apply_snapshot(snapshot)
        self._announce("<<ActiveWalletChanged>>")

    def reload_wallet(self) -> None:
        """Reload the active wallet from the Platform-backed adapter."""

        self._apply_snapshot(self.application.load_wallet())

    def _apply_snapshot(self, snapshot) -> None:
        self._snapshot = snapshot
        self._wallets = self.application.list_wallets()
        self.wallet_name.set(snapshot.name)
        self.receive_address.set(snapshot.receive_address)
        self.revision.set(self.revision.get() + 1)

    def apply_synchronized_snapshot(self, snapshot) -> bool:
        """Apply a background result only if its wallet is still active."""

        if snapshot.name != self.active_wallet_name:
            return False
        self._apply_snapshot(snapshot)
        return True

    def request_wallet_refresh(self) -> None:
        self._announce("<<WalletRefreshRequested>>")

    def set_sync_activity(self, active: bool, message: str) -> None:
        self.syncing.set(active)
        self.sync_status.set(message)

    def _announce(self, event_name: str) -> None:
        self.root.event_generate(event_name, when="tail")

    def amount_is_valid(self) -> bool:
        if self.send_all.get():
            return True
        try:
            # Parsing here gives immediate feedback. The use case repeats the
            # check at its own trust boundary before preparing a transaction.
            BitcoinAmount.parse(self.amount.get(), self.unit)
            return True
        except ValueError:
            return False

    def prepare_withdrawal(self, fee_rate_sat_vb: int) -> WithdrawalDraft:
        return self.application.prepare_withdrawal(
            self.address.get(),
            self.amount.get(),
            self.unit,
            fee_rate_sat_vb,
            send_all=self.send_all.get(),
        )

    def sign_withdrawal(
        self, draft_id: str, password: str | None
    ) -> WithdrawalReview:
        return self.application.sign_withdrawal(draft_id, password)

    def broadcast_withdrawal(self, review_id: str) -> BroadcastResult:
        result = self.application.broadcast_withdrawal(review_id)
        self.reload_wallet()
        return result

    def cancel_withdrawal(self, review_id: str) -> None:
        self.application.cancel_withdrawal(review_id)

    def apply_broadcast_success(self) -> None:
        """Clear the completed form and refresh all wallet-facing screens."""

        self.amount.set("")
        self.address.set("")
        self.send_all.set(False)
        self.reload_wallet()
        self._announce("<<WalletBroadcast>>")

    def fiat_zero_text(self) -> str:
        return {
            "USD": "US$0.00",
            "JPY": "¥0",
            "CNY": "CN¥0.00",
            "EUR": "€0.00",
        }.get(self.fiat_currency.get(), f"{self.fiat_currency.get()} 0.00")
