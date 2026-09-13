"""Thin GUI adapter over bitcoin-tool's high-level platform services."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from app_settings import ApplicationSettingsStore
from btc.chainparams import NETWORK_MAINNET
from tx.service import PaymentService as PlatformPaymentService
from tx.service import TransactionError
from wallet.service import AccountedTransaction
from wallet.service import TransactionStatus as PlatformTransactionStatus
from wallet.service import WalletError
from wallet.service import WalletMetadata as PlatformWalletMetadata
from wallet.service import WalletService as PlatformWalletService
from wallet.service import WalletState as PlatformWalletState
from wallet_core.models import (
    BitcoinAmount,
    BroadcastResult,
    SendPreview,
    TransactionDirection,
    TransactionStatus,
    TransactionSummary,
    WalletCreation,
    WalletSnapshot,
    WalletSummary,
    WithdrawalDraft,
    WithdrawalReview,
)
from wallet_core.ports import WalletService


class BitcoinToolWalletService(WalletService):
    """Map stable bitcoin-tool Platform DTOs to mini-wallet view models."""

    def __init__(
        self,
        wallet_file: Path,
        cache_file: Path,
        network: str = NETWORK_MAINNET,
        backend_factory: Callable[[str], object] | None = None,
        settings_file: Path | None = None,
    ) -> None:
        self.wallet_file = Path(wallet_file)
        self.cache_file = Path(cache_file)
        self.network = network
        self.settings = ApplicationSettingsStore(
            settings_file or self.wallet_file.parent / "settings.json"
        )
        self.settings.ensure_exists()
        self.platform_wallet = PlatformWalletService(
            self.wallet_file,
            self.cache_file,
            network,
            backend_factory,
        )
        self.platform_payment = PlatformPaymentService(self.platform_wallet)
        self._wallet_name = self._resolve_active_wallet()

    def list_wallets(self) -> tuple[WalletSummary, ...]:
        try:
            return tuple(
                self._wallet_summary(wallet)
                for wallet in self.platform_wallet.list_wallets()
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc

    def _resolve_active_wallet(self) -> str | None:
        wallets = self.list_wallets()
        wallet_names = {wallet.name for wallet in wallets}
        configured_name = self.settings.active_wallet(self.network)
        selected_name = (
            configured_name
            if configured_name in wallet_names
            else wallets[0].name if wallets else None
        )
        if selected_name != configured_name:
            self.settings.set_active_wallet(self.network, selected_name)
        return selected_name

    def select_wallet(self, name: str) -> WalletSnapshot:
        if name not in {wallet.name for wallet in self.list_wallets()}:
            raise ValueError(f'Wallet "{name}" does not exist on {self.network}.')
        self._activate_wallet(name)
        return self.snapshot()

    def synchronize_wallet(self, name: str) -> WalletSnapshot:
        try:
            return self._wallet_snapshot(self.platform_wallet.sync_wallet(name))
        except WalletError as exc:
            raise ValueError(str(exc)) from exc

    def transaction_status(self, txid: str) -> TransactionStatus:
        try:
            return self._transaction_status(
                self.platform_wallet.transaction_status(txid)
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc

    def snapshot(self) -> WalletSnapshot:
        if self._wallet_name is None:
            return WalletSnapshot(
                name="No Wallet",
                balance=BitcoinAmount(0),
                receive_address="",
                network=self.network,
                transactions=(),
                is_initialized=False,
            )
        try:
            return self._wallet_snapshot(
                self.platform_wallet.get_wallet_state(self._wallet_name)
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc

    def create_wallet(
        self,
        name: str,
        password: str,
        mnemonic: str | None = None,
    ) -> WalletCreation:
        imported = mnemonic is not None
        try:
            creation = (
                self.platform_wallet.import_wallet(name, password, mnemonic)
                if mnemonic is not None
                else self.platform_wallet.create_wallet(name, password)
            )
        except WalletError as exc:
            if imported and self._platform_wallet_exists(name):
                self._activate_wallet(name)
                raise ValueError(
                    "Wallet was imported, but its address scan could not complete: "
                    f"{exc}"
                ) from exc
            raise ValueError(str(exc)) from exc
        self._activate_wallet(name)
        return WalletCreation(
            self._wallet_snapshot(creation.state),
            creation.mnemonic,
            creation.imported,
        )

    def get_mnemonic(self, password: str | None) -> str:
        wallet_name = self._require_active_wallet()
        try:
            return self.platform_wallet.get_mnemonic(wallet_name, password)
        except WalletError as exc:
            raise ValueError(str(exc)) from exc

    def rename_wallet(
        self,
        name: str,
        password: str | None = None,
    ) -> WalletSnapshot:
        wallet_name = self._require_active_wallet()
        try:
            state = self.platform_wallet.rename_wallet(wallet_name, name, password)
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        self._activate_wallet(name)
        return self._wallet_snapshot(state)

    def change_password(
        self,
        current_password: str | None,
        new_password: str | None,
    ) -> WalletSnapshot:
        wallet_name = self._require_active_wallet()
        try:
            state = self.platform_wallet.change_password(
                wallet_name,
                current_password,
                new_password,
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        return self._wallet_snapshot(state)

    def remove_wallet(self, password: str | None) -> WalletSnapshot:
        wallet_name = self._require_active_wallet()
        try:
            self.platform_wallet.remove_wallet(wallet_name, password)
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        remaining = self.list_wallets()
        self._wallet_name = remaining[0].name if remaining else None
        self.settings.set_active_wallet(self.network, self._wallet_name)
        return self.snapshot()

    def preview_send(
        self,
        destination: str,
        amount: BitcoinAmount | None,
        fee_rate_sat_vb: int,
        *,
        send_all: bool = False,
    ) -> SendPreview:
        raise ValueError("Use prepare withdrawal for an exact funded preview.")

    def prepare_withdrawal(
        self,
        destination: str,
        amount: BitcoinAmount | None,
        fee_rate_sat_vb: int,
        *,
        send_all: bool = False,
    ) -> WithdrawalDraft:
        wallet_name = self._require_active_wallet()
        try:
            draft = self.platform_payment.prepare(
                wallet_name,
                destination,
                None if amount is None else amount.sats,
                fee_rate_sat_vb,
                send_all=send_all,
            )
        except (TransactionError, WalletError) as exc:
            raise ValueError(str(exc)) from exc
        return WithdrawalDraft(
            draft.draft_id,
            draft.wallet_name,
            draft.network,
            draft.destination,
            BitcoinAmount(draft.amount_sats),
            BitcoinAmount(draft.estimated_fee_sats),
            draft.fee_rate_sat_vb,
            draft.send_all,
        )

    def sign_withdrawal(
        self,
        draft_id: str,
        password: str | None,
    ) -> WithdrawalReview:
        try:
            payment = self.platform_payment.sign(draft_id, password)
        except (TransactionError, WalletError) as exc:
            raise ValueError(str(exc)) from exc
        return WithdrawalReview(
            payment.payment_id,
            payment.wallet_name,
            payment.network,
            payment.txid,
            payment.destination,
            BitcoinAmount(payment.amount_sats),
            BitcoinAmount(payment.fee_sats),
            payment.fee_rate_sat_vb,
            payment.send_all,
        )

    def broadcast_withdrawal(self, review_id: str) -> BroadcastResult:
        try:
            receipt = self.platform_payment.broadcast(review_id)
        except (TransactionError, WalletError) as exc:
            raise ValueError(str(exc)) from exc
        return BroadcastResult(
            receipt.txid,
            receipt.network,
            receipt.cache_warning,
        )

    def cancel_withdrawal(self, review_id: str) -> None:
        self.platform_payment.cancel(review_id)

    def _activate_wallet(self, name: str) -> None:
        self._wallet_name = name
        self.settings.set_active_wallet(self.network, name)

    def _require_active_wallet(self) -> str:
        if self._wallet_name is None:
            raise ValueError("Create or import a wallet before continuing.")
        return self._wallet_name

    def _platform_wallet_exists(self, name: str) -> bool:
        try:
            return name in {
                wallet.name for wallet in self.platform_wallet.list_wallets()
            }
        except WalletError:
            return False

    def _wallet_snapshot(self, state: PlatformWalletState) -> WalletSnapshot:
        return WalletSnapshot(
            name=state.metadata.name,
            balance=BitcoinAmount(state.balance_sats),
            receive_address=state.receive_address,
            network=state.metadata.network,
            transactions=tuple(
                self._transaction_summary(transaction)
                for transaction in state.transactions
            ),
            is_initialized=True,
            synced_at=state.synced_at,
            pending_txids=state.pending_txids,
        )

    @staticmethod
    def _wallet_summary(wallet: PlatformWalletMetadata) -> WalletSummary:
        return WalletSummary(
            wallet.name,
            wallet.network,
            wallet.encrypted,
            wallet.master_fingerprint,
        )

    def _transaction_summary(
        self,
        transaction: AccountedTransaction,
    ) -> TransactionSummary:
        direction = {
            "receive": TransactionDirection.INCOMING,
            "send": TransactionDirection.OUTGOING,
            "self": TransactionDirection.SELF,
        }[transaction.direction]
        return TransactionSummary(
            txid=transaction.txid,
            network=self.network,
            amount=BitcoinAmount(transaction.net_sats),
            direction=direction,
            received=BitcoinAmount(transaction.received_sats),
            sent=BitcoinAmount(transaction.sent_sats),
            fee=BitcoinAmount(transaction.fee_sats),
            confirmed=transaction.confirmed,
            confirmations=transaction.confirmations,
            block_height=transaction.block_height,
            block_time=transaction.block_time,
            addresses=transaction.addresses,
            account_ids=transaction.account_ids,
            address_types=transaction.address_types,
        )

    @staticmethod
    def _transaction_status(
        status: PlatformTransactionStatus,
    ) -> TransactionStatus:
        return TransactionStatus(
            status.txid,
            status.confirmed,
            status.block_height,
            status.block_time,
        )
