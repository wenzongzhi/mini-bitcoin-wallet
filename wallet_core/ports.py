"""Interfaces at the boundary between the app and Bitcoin infrastructure."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import (
    AccountType,
    BitcoinAmount,
    BroadcastResult,
    FeeRateEstimate,
    TransactionStatus,
    WalletAccountActivation,
    WalletCreation,
    WalletSnapshot,
    WalletSummary,
    WithdrawalDraft,
    WithdrawalReview,
)


class WalletService(ABC):
    """Capability contract implemented by demo and production backends.

    Concrete Platform and demo adapters belong behind this interface. Pages
    must never import transaction builders, storage code, or network clients.
    """

    @abstractmethod
    def snapshot(self) -> WalletSnapshot:
        """Return data needed to render the current wallet."""

    @abstractmethod
    def list_wallets(self) -> tuple[WalletSummary, ...]:
        """Return non-secret summaries for wallets on the selected network."""

    @abstractmethod
    def select_wallet(self, name: str) -> WalletSnapshot:
        """Persist and load the active wallet without decrypting it."""

    @abstractmethod
    def select_account_type(
        self,
        account_type: AccountType,
    ) -> WalletAccountActivation:
        """Enable and select an account type for the active wallet."""

    @abstractmethod
    def synchronize_wallet(self, name: str) -> WalletSnapshot:
        """Run a complete network synchronization for one named wallet."""

    @abstractmethod
    def transaction_status(self, txid: str) -> TransactionStatus:
        """Fetch one transaction's confirmation status without scanning addresses."""

    @abstractmethod
    def fee_estimates(self) -> tuple[FeeRateEstimate, ...]:
        """Return backend fee estimates without exposing Platform DTOs."""

    @abstractmethod
    def get_mnemonic(self, password: str | None) -> str:
        """Unlock and return the active wallet's recovery words."""

    @abstractmethod
    def rename_wallet(
        self, name: str, password: str | None = None
    ) -> WalletSnapshot:
        """Rename the wallet and return its updated snapshot."""

    @abstractmethod
    def change_password(
        self,
        current_password: str | None,
        new_password: str,
    ) -> WalletSnapshot:
        """Replace the active wallet's encryption password."""

    @abstractmethod
    def remove_wallet(self, password: str | None) -> WalletSnapshot:
        """Remove the active wallet and return the next active snapshot."""

    @abstractmethod
    def create_wallet(self, name: str, password: str) -> WalletCreation:
        """Create a wallet and return its newly generated recovery words."""

    @abstractmethod
    def import_wallet(
        self,
        name: str,
        password: str,
        mnemonic: str,
    ) -> WalletCreation:
        """Import a wallet without returning the caller-provided recovery words."""

    @abstractmethod
    def prepare_withdrawal(
        self,
        destination: str,
        amount: BitcoinAmount | None,
        fee_rate_sat_vb: int,
        *,
        send_all: bool = False,
    ) -> WithdrawalDraft:
        """Synchronize and fund a transaction before requesting a password."""

    @abstractmethod
    def sign_withdrawal(
        self, draft_id: str, password: str | None
    ) -> WithdrawalReview:
        """Unlock the wallet and sign a previously validated draft."""

    @abstractmethod
    def broadcast_withdrawal(self, review_id: str) -> BroadcastResult:
        """Broadcast a previously reviewed signed transaction."""

    @abstractmethod
    def cancel_withdrawal(self, review_id: str) -> None:
        """Release UTXOs reserved by an unbroadcast transaction review."""
