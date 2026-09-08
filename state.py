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
        """Return pending and unconfirmed TXIDs without duplicates."""

        return tuple(
            dict.fromkeys(
                (
                    *self._snapshot.pending_txids,
                    *(
                        transaction.txid
                        for transaction in self._snapshot.transactions
                        if not transaction.confirmed
                    ),
                )
            )
        )

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

    def rename_wallet(self, name: str) -> None:
        self._snapshot = self.application.rename_wallet(name)
        self.wallet_name.set(self._snapshot.name)

    def create_wallet(
        self, name: str, password: str, mnemonic: str | None = None
    ) -> WalletCreation:
        creation = self.application.create_wallet(name, password, mnemonic)
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

    def apply_wallet_creation(self, creation: WalletCreation) -> None:
        """Apply a wallet created by a background application use case."""

        self._apply_snapshot(creation.snapshot)
        self._announce("<<ActiveWalletChanged>>")

    def reload_wallet(self) -> None:
        """Reload persistent state after a partially completed operation."""

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
