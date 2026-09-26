"""Deterministic in-memory wallet backend for tests and manual UI debugging."""

from dataclasses import replace
from datetime import datetime, timezone

from wallet_core.models import (
    AccountType,
    BitcoinAmount,
    BroadcastResult,
    TransactionDirection,
    TransactionStatus,
    TransactionSummary,
    WalletAccountActivation,
    WalletCreation,
    WalletSnapshot,
    WalletSummary,
    WithdrawalDraft,
    WithdrawalReview,
)
from wallet_core.ports import WalletService


class DemoWalletService(WalletService):
    """UI-safe demo data with the same contract as a real wallet backend."""

    ESTIMATED_TRANSACTION_VBYTES = 140
    RECEIVE_ADDRESSES = {
        AccountType.NATIVE_SEGWIT: "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        AccountType.LEGACY: "1BoatSLRHtKNngkdXEeobR76b53LETtpyT",
    }

    def __init__(self) -> None:
        self._demo_draft = None
        self._snapshot = WalletSnapshot(
            name="Bitcoin Wallet",
            balance=BitcoinAmount(408_000),
            receive_address=self.RECEIVE_ADDRESSES[AccountType.NATIVE_SEGWIT],
            account_type=AccountType.NATIVE_SEGWIT,
            authoritative_balance=BitcoinAmount(408_000),
            confirmed_balance=BitcoinAmount(408_000),
            unconfirmed_chain_balance=BitcoinAmount(0),
            pending_delta=BitcoinAmount(0),
            available_balance=BitcoinAmount(408_000),
            transactions=(
                self._transaction("demo-outgoing", -210_203),
                self._transaction("demo-incoming-1", 179_475),
                self._transaction("demo-incoming-2", 206_296),
                self._transaction("demo-incoming-3", 138_747),
            ),
        )

    @staticmethod
    def _transaction(txid: str, sats: int) -> TransactionSummary:
        direction = (
            TransactionDirection.INCOMING if sats >= 0 else TransactionDirection.OUTGOING
        )
        return TransactionSummary(
            txid=txid,
            network="mainnet",
            amount=BitcoinAmount(sats),
            direction=direction,
            received=BitcoinAmount(max(sats, 0)),
            sent=BitcoinAmount(abs(min(sats, 0))),
            fee=BitcoinAmount(0),
            confirmed=True,
            confirmations=1,
            block_height=1,
            block_time=datetime(2026, 7, 1, tzinfo=timezone.utc),
            addresses=(),
            account_ids=(),
            address_types=("P2WPKH",),
        )

    def snapshot(self) -> WalletSnapshot:
        return self._snapshot

    def list_wallets(self) -> tuple[WalletSummary, ...]:
        return (
            WalletSummary(
                name=self._snapshot.name,
                network=self._snapshot.network,
                encrypted=True,
            ),
        )

    def select_wallet(self, name: str) -> WalletSnapshot:
        if name != self._snapshot.name:
            raise ValueError(f'Wallet "{name}" does not exist.')
        self._snapshot = replace(
            self._snapshot,
            account_type=AccountType.NATIVE_SEGWIT,
            receive_address=self.RECEIVE_ADDRESSES[AccountType.NATIVE_SEGWIT],
        )
        return self._snapshot

    def select_account_type(
        self,
        account_type: AccountType,
    ) -> WalletAccountActivation:
        try:
            selected_type = AccountType(account_type)
        except (TypeError, ValueError) as exc:
            raise ValueError("Unsupported wallet account type.") from exc
        self._snapshot = replace(
            self._snapshot,
            account_type=selected_type,
            receive_address=self.RECEIVE_ADDRESSES[selected_type],
        )
        return WalletAccountActivation(self._snapshot, selected_type)

    def synchronize_wallet(self, name: str) -> WalletSnapshot:
        if name != self._snapshot.name:
            raise ValueError(f'Wallet "{name}" does not exist.')
        return self._snapshot

    def transaction_status(self, txid: str) -> TransactionStatus:
        return TransactionStatus(txid=txid, confirmed=True, block_height=1)

    def get_mnemonic(self, password: str | None) -> str:
        return "demo mnemonic"

    def rename_wallet(
        self, name: str, password: str | None = None
    ) -> WalletSnapshot:
        self._snapshot = replace(self._snapshot, name=name)
        return self._snapshot

    def change_password(
        self,
        current_password: str | None,
        new_password: str,
    ) -> WalletSnapshot:
        return self._snapshot

    def remove_wallet(self, password: str | None) -> WalletSnapshot:
        self._snapshot = WalletSnapshot(
            name="No Wallet",
            balance=BitcoinAmount(0),
            receive_address="",
            authoritative_balance=BitcoinAmount(0),
            confirmed_balance=BitcoinAmount(0),
            unconfirmed_chain_balance=BitcoinAmount(0),
            pending_delta=BitcoinAmount(0),
            available_balance=BitcoinAmount(0),
            is_initialized=False,
        )
        return self._snapshot

    def create_wallet(self, name: str, password: str) -> WalletCreation:
        self._snapshot = replace(
            self._snapshot,
            name=name,
            receive_address=self.RECEIVE_ADDRESSES[AccountType.NATIVE_SEGWIT],
            account_type=AccountType.NATIVE_SEGWIT,
            is_initialized=True,
        )
        return WalletCreation(self._snapshot, "demo mnemonic")

    def import_wallet(
        self,
        name: str,
        password: str,
        mnemonic: str,
    ) -> WalletCreation:
        self._snapshot = replace(
            self._snapshot,
            name=name,
            receive_address=self.RECEIVE_ADDRESSES[AccountType.NATIVE_SEGWIT],
            account_type=AccountType.NATIVE_SEGWIT,
            is_initialized=True,
        )
        return WalletCreation(self._snapshot, generated_mnemonic=None)

    def prepare_withdrawal(
        self,
        destination: str,
        amount: BitcoinAmount | None,
        fee_rate_sat_vb: int,
        *,
        send_all: bool = False,
    ) -> WithdrawalDraft:
        funded_amount, fee = self._fund_withdrawal(
            destination,
            amount,
            fee_rate_sat_vb,
            send_all=send_all,
        )
        self._demo_draft = (
            destination,
            funded_amount,
            fee,
            fee_rate_sat_vb,
            send_all,
            self._snapshot.account_type,
        )
        return WithdrawalDraft(
            draft_id="demo-review",
            wallet_name=self._snapshot.name,
            network=self._snapshot.network,
            destination=destination,
            amount=funded_amount,
            estimated_fee=fee,
            fee_rate_sat_vb=fee_rate_sat_vb,
            send_all=send_all,
            account_type=self._snapshot.account_type,
        )

    def _fund_withdrawal(
        self,
        destination: str,
        amount: BitcoinAmount | None,
        fee_rate_sat_vb: int,
        *,
        send_all: bool,
    ) -> tuple[BitcoinAmount, BitcoinAmount]:
        """Provide deterministic funding for the UI-only prepare workflow."""

        if not destination.lower().startswith(("bc1", "1", "3")):
            raise ValueError(
                "The destination is not a supported mainnet Bitcoin address."
            )
        fee = BitcoinAmount(self.ESTIMATED_TRANSACTION_VBYTES * fee_rate_sat_vb)
        if send_all:
            spendable = self._snapshot.available_balance.sats - fee.sats
            if spendable <= 0:
                raise ValueError("The wallet balance is not enough to pay the fee.")
            amount = BitcoinAmount(spendable)
        assert amount is not None
        if amount.sats <= 0:
            raise ValueError("The amount must be greater than zero.")
        if amount.sats + fee.sats > self._snapshot.available_balance.sats:
            raise ValueError("The amount and fee exceed the wallet balance.")
        return amount, fee

    def sign_withdrawal(
        self, draft_id: str, password: str | None
    ) -> WithdrawalReview:
        if draft_id != "demo-review" or self._demo_draft is None:
            raise ValueError("Withdrawal draft does not exist.")
        (
            destination,
            amount,
            fee,
            fee_rate_sat_vb,
            send_all,
            account_type,
        ) = self._demo_draft
        return WithdrawalReview(
            review_id=draft_id,
            wallet_name=self._snapshot.name,
            network=self._snapshot.network,
            txid="0" * 64,
            destination=destination,
            amount=amount,
            fee=fee,
            fee_rate_sat_vb=fee_rate_sat_vb,
            send_all=send_all,
            account_type=account_type,
        )

    def broadcast_withdrawal(self, review_id: str) -> BroadcastResult:
        if review_id != "demo-review":
            raise ValueError("Withdrawal review does not exist.")
        return BroadcastResult("0" * 64, self._snapshot.network)

    def cancel_withdrawal(self, review_id: str) -> None:
        if review_id == "demo-review":
            self._demo_draft = None
        return None
