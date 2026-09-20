"""Use cases consumed by the desktop UI."""

from __future__ import annotations

from .models import (
    BitcoinAmount,
    BroadcastResult,
    DisplayUnit,
    TransactionStatus,
    WalletCreation,
    WalletSnapshot,
    WalletSummary,
    WithdrawalDraft,
    WithdrawalReview,
)
from .ports import WalletService


class WalletApplication:
    """Coordinates wallet use cases while keeping Tkinter out of the core."""

    def __init__(self, service: WalletService):
        self._service = service

    def load_wallet(self) -> WalletSnapshot:
        return self._service.snapshot()

    def list_wallets(self) -> tuple[WalletSummary, ...]:
        return self._service.list_wallets()

    def select_wallet(self, name: str) -> WalletSnapshot:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Wallet name cannot be empty.")
        return self._service.select_wallet(normalized)

    def synchronize_wallet(self, name: str) -> WalletSnapshot:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Wallet name cannot be empty.")
        return self._service.synchronize_wallet(normalized)

    def transaction_status(self, txid: str) -> TransactionStatus:
        normalized = txid.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("Transaction ID must be 64 hexadecimal characters.")
        return self._service.transaction_status(normalized)

    def get_mnemonic(self, password: str | None) -> str:
        return self._service.get_mnemonic(password)

    def rename_wallet(
        self, name: str, password: str | None = None
    ) -> WalletSnapshot:
        normalized = name.strip()
        if not normalized:
            raise ValueError("Wallet name cannot be empty.")
        return self._service.rename_wallet(normalized, password)

    def change_password(
        self,
        current_password: str | None,
        new_password: str,
    ) -> WalletSnapshot:
        if not new_password:
            raise ValueError("New password cannot be empty.")
        return self._service.change_password(current_password, new_password)

    def remove_wallet(self, password: str | None) -> WalletSnapshot:
        return self._service.remove_wallet(password)

    def create_wallet(self, name: str, password: str) -> WalletCreation:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Wallet name cannot be empty.")
        if not password:
            raise ValueError("Password cannot be empty.")
        result = self._service.create_wallet(normalized_name, password)
        if not result.generated_mnemonic:
            raise ValueError("Wallet creation did not return generated recovery words.")
        return result

    def import_wallet(
        self,
        name: str,
        password: str,
        mnemonic: str,
    ) -> WalletCreation:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Wallet name cannot be empty.")
        if not password:
            raise ValueError("Password cannot be empty.")
        normalized_mnemonic = " ".join(mnemonic.strip().split())
        if not normalized_mnemonic:
            raise ValueError("Recovery words cannot be empty.")
        result = self._service.import_wallet(
            normalized_name,
            password,
            normalized_mnemonic,
        )
        # Enforce the product boundary even if a custom adapter violates the
        # port contract: caller-provided recovery words stop at import.
        return WalletCreation(
            snapshot=result.snapshot,
            generated_mnemonic=None,
            discovery=result.discovery,
        )

    def prepare_withdrawal(
        self,
        destination: str,
        amount_text: str,
        unit: DisplayUnit,
        fee_rate_sat_vb: int,
        *,
        send_all: bool = False,
    ) -> WithdrawalDraft:
        normalized_destination = destination.strip()
        if not normalized_destination:
            raise ValueError("Enter a destination address.")
        if isinstance(fee_rate_sat_vb, bool) or not 1 <= fee_rate_sat_vb <= 20:
            raise ValueError(
                "Choose a fee rate from 1 to 20 sat/vB. "
                "Zero-fee transactions are not accepted by bitcoin-tool or standard relays."
            )
        amount = None if send_all else BitcoinAmount.parse(amount_text, unit)
        return self._service.prepare_withdrawal(
            normalized_destination,
            amount,
            fee_rate_sat_vb,
            send_all=send_all,
        )

    def sign_withdrawal(
        self, draft_id: str, password: str | None
    ) -> WithdrawalReview:
        if not draft_id:
            raise ValueError("Withdrawal draft identifier is missing.")
        return self._service.sign_withdrawal(draft_id, password)

    def broadcast_withdrawal(self, review_id: str) -> BroadcastResult:
        if not review_id:
            raise ValueError("Withdrawal review identifier is missing.")
        return self._service.broadcast_withdrawal(review_id)

    def cancel_withdrawal(self, review_id: str) -> None:
        if review_id:
            self._service.cancel_withdrawal(review_id)
