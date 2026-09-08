"""Interfaces at the boundary between the app and Bitcoin infrastructure."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import (
    BitcoinAmount,
    BroadcastResult,
    SendPreview,
    TransactionStatus,
    WalletCreation,
    WalletSnapshot,
    WalletSummary,
    WithdrawalDraft,
    WithdrawalReview,
)


class WalletService(ABC):
    """Capability contract implemented by demo and production backends.

    A future bitcoin-tool adapter belongs behind this interface.  Pages must
    never import transaction builders, storage code, or network clients.
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
    def synchronize_wallet(self, name: str) -> WalletSnapshot:
        """Run a complete network synchronization for one named wallet."""

    @abstractmethod
    def transaction_status(self, txid: str) -> TransactionStatus:
        """Fetch one transaction's confirmation status without scanning addresses."""

    @abstractmethod
    def rename_wallet(self, name: str) -> WalletSnapshot:
        """Rename the wallet and return its updated snapshot."""

    @abstractmethod
    def create_wallet(
        self, name: str, password: str, mnemonic: str | None = None
    ) -> WalletCreation:
        """Create a generated or imported encrypted wallet."""

    @abstractmethod
    def preview_send(
        self,
        destination: str,
        amount: BitcoinAmount | None,
        fee_rate_sat_vb: int,
        *,
        send_all: bool = False,
    ) -> SendPreview:
        """Validate and fund a transfer without signing or broadcasting it."""

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
