"""Deterministic local backend used until the bitcoin-tool adapter is wired."""

from dataclasses import replace
from datetime import datetime, timezone

from wallet_core.models import (
    BitcoinAmount,
    BroadcastResult,
    SendPreview,
    TransactionDirection,
    TransactionSummary,
    WalletCreation,
    WalletSnapshot,
    WalletSummary,
    WithdrawalReview,
)
from wallet_core.ports import WalletService


class DemoWalletService(WalletService):
    """UI-safe demo data with the same contract as a real wallet backend."""

    ESTIMATED_TRANSACTION_VBYTES = 140

    def __init__(self) -> None:
        self._snapshot = WalletSnapshot(
            name="Bitcoin Wallet",
            balance=BitcoinAmount(408_000),
            receive_address="bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
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
            explorer_url="",
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
        return self._snapshot

    def rename_wallet(self, name: str) -> WalletSnapshot:
        self._snapshot = replace(self._snapshot, name=name)
        return self._snapshot

    def create_wallet(
        self, name: str, password: str, mnemonic: str | None = None
    ) -> WalletCreation:
        self._snapshot = replace(self._snapshot, name=name, is_initialized=True)
        return WalletCreation(self._snapshot, mnemonic or "demo mnemonic", mnemonic is not None)

    def preview_send(
        self,
        destination: str,
        amount: BitcoinAmount | None,
        fee_rate_sat_vb: int,
        *,
        send_all: bool = False,
    ) -> SendPreview:
        # A real adapter delegates address validation and funding to bitcoin-tool.
        if not destination.lower().startswith(("bc1", "1", "3")):
            raise ValueError("The destination is not a supported mainnet Bitcoin address.")
        fee = BitcoinAmount(self.ESTIMATED_TRANSACTION_VBYTES * fee_rate_sat_vb)
        if send_all:
            spendable = self._snapshot.balance.sats - fee.sats
            if spendable <= 0:
                raise ValueError("The wallet balance is not enough to pay the fee.")
            amount = BitcoinAmount(spendable)
        assert amount is not None
        if amount.sats <= 0:
            raise ValueError("The amount must be greater than zero.")
        if amount.sats + fee.sats > self._snapshot.balance.sats:
            raise ValueError("The amount and fee exceed the wallet balance.")
        return SendPreview(destination, amount, fee, fee_rate_sat_vb)

    def prepare_withdrawal(
        self,
        destination: str,
        amount: BitcoinAmount | None,
        fee_rate_sat_vb: int,
        password: str | None,
        *,
        send_all: bool = False,
    ) -> WithdrawalReview:
        preview = self.preview_send(
            destination,
            amount,
            fee_rate_sat_vb,
            send_all=send_all,
        )
        return WithdrawalReview(
            review_id="demo-review",
            wallet_name=self._snapshot.name,
            network=self._snapshot.network,
            txid="0" * 64,
            destination=destination,
            amount=preview.amount,
            fee=preview.fee,
            fee_rate_sat_vb=fee_rate_sat_vb,
            send_all=send_all,
        )

    def broadcast_withdrawal(self, review_id: str) -> BroadcastResult:
        if review_id != "demo-review":
            raise ValueError("Withdrawal review does not exist.")
        return BroadcastResult("0" * 64, "")

    def cancel_withdrawal(self, review_id: str) -> None:
        return None
