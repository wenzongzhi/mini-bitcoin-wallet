"""Thin GUI adapter over bitcoin-tool's high-level platform services."""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Callable

from app_settings import ActiveWalletSelection, ApplicationSettingsStore
from btc.chainparams import NETWORK_MAINNET
from tx.service import PaymentService as PlatformPaymentService
from tx.service import TransactionError
from wallet.service import AccountedTransaction
from wallet.service import TransactionStatus as PlatformTransactionStatus
from wallet.service import WalletCreationResult as PlatformWalletCreationResult
from wallet.service import WalletError
from wallet.service import WalletMetadata as PlatformWalletMetadata
from wallet.service import WalletService as PlatformWalletService
from wallet.service import WalletState as PlatformWalletState
from wallet_core.models import (
    AccountType,
    AddressDiscoverySummary,
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


class BitcoinToolWalletService(WalletService):
    """Map stable bitcoin-tool Platform DTOs to mini-wallet view models."""

    def __init__(
        self,
        wallet_file: Path,
        cache_file: Path,
        *,
        settings_store: ApplicationSettingsStore,
        network: str = NETWORK_MAINNET,
        backend_factory: Callable[[str], object] | None = None,
    ) -> None:
        self.wallet_file = Path(wallet_file)
        self.cache_file = Path(cache_file)
        self.network = network
        self.settings_store = settings_store
        self.platform_wallet = PlatformWalletService(
            self.wallet_file,
            self.cache_file,
            network,
            backend_factory,
        )
        self.platform_payment = PlatformPaymentService(self.platform_wallet)
        self._selection_lock = RLock()
        self._wallet_name, self._account_type = self._resolve_active_wallet()

    def list_wallets(self) -> tuple[WalletSummary, ...]:
        try:
            return tuple(
                self._wallet_summary(wallet)
                for wallet in self.platform_wallet.list_wallets()
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc

    def _resolve_active_wallet(self) -> tuple[str | None, AccountType]:
        wallets = self.list_wallets()
        wallet_names = {wallet.name for wallet in wallets}
        configured = self.settings_store.active_wallet_selection(self.network)
        configured_name = configured.wallet_name
        selected_name = (
            configured_name
            if configured_name in wallet_names
            else wallets[0].name if wallets else None
        )
        if selected_name != configured_name:
            self.settings_store.set_active_wallet(self.network, selected_name)
            return selected_name, AccountType.NATIVE_SEGWIT
        return selected_name, configured.account_type

    def select_wallet(self, name: str) -> WalletSnapshot:
        if name not in {wallet.name for wallet in self.list_wallets()}:
            raise ValueError(f'Wallet "{name}" does not exist on {self.network}.')
        account_type = AccountType.NATIVE_SEGWIT
        try:
            state = self.platform_wallet.get_wallet_state(
                name,
                address_type=account_type.value,
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        self._activate_wallet(name)
        return self._wallet_snapshot(state, account_type)

    def select_account_type(
        self,
        account_type: AccountType,
    ) -> WalletAccountActivation:
        """Enable an account before atomically making it the UI selection."""

        selected_type = self._validated_account_type(account_type)
        wallet_name, _current_type = self._active_selection()
        if wallet_name is None:
            raise ValueError("Create or import a wallet before continuing.")
        try:
            result = self.platform_wallet.enable_account(
                wallet_name,
                selected_type.value,
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc

        # Persist with an expected-wallet guard before changing in-memory
        # selection. A slow discovery can therefore never reactivate a wallet
        # that the user switched away from in the meantime.
        with self._selection_lock:
            if self._wallet_name != wallet_name:
                raise ValueError(
                    "The active wallet changed while its account was enabled."
                )
            self.settings_store.set_active_account_type(
                self.network,
                selected_type,
                expected_wallet_name=wallet_name,
            )
            self._account_type = selected_type
        return WalletAccountActivation(
            snapshot=self._wallet_snapshot(result.state, selected_type),
            account_type=selected_type,
            discovery=self._discovery_summary(result.discovery),
        )

    def synchronize_wallet(self, name: str) -> WalletSnapshot:
        wallet_name, account_type = self._active_selection()
        selected_type = (
            account_type if name == wallet_name else AccountType.NATIVE_SEGWIT
        )
        try:
            state = self.platform_wallet.sync_wallet(
                name,
                address_type=selected_type.value,
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        return self._wallet_snapshot(state, selected_type)

    def transaction_status(self, txid: str) -> TransactionStatus:
        try:
            return self._transaction_status(
                self.platform_wallet.transaction_status(txid)
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc

    def snapshot(self) -> WalletSnapshot:
        wallet_name, account_type = self._active_selection()
        if wallet_name is None:
            return WalletSnapshot(
                name="No Wallet",
                balance=BitcoinAmount(0),
                receive_address="",
                account_type=AccountType.NATIVE_SEGWIT,
                authoritative_balance=BitcoinAmount(0),
                confirmed_balance=BitcoinAmount(0),
                unconfirmed_chain_balance=BitcoinAmount(0),
                pending_delta=BitcoinAmount(0),
                available_balance=BitcoinAmount(0),
                network=self.network,
                transactions=(),
                is_initialized=False,
            )
        try:
            state = self.platform_wallet.get_wallet_state(
                wallet_name,
                address_type=account_type.value,
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        return self._wallet_snapshot(state, account_type)

    def create_wallet(self, name: str, password: str) -> WalletCreation:
        account_type = AccountType.NATIVE_SEGWIT
        try:
            result = self.platform_wallet.create_wallet(
                name,
                password,
                address_type=account_type.value,
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        if result.generated_mnemonic is None:
            raise ValueError("The Platform did not return generated recovery words.")
        self._activate_wallet(name)
        return self._creation_result(
            result,
            generated_mnemonic=result.generated_mnemonic,
            account_type=account_type,
        )

    def import_wallet(
        self,
        name: str,
        password: str,
        mnemonic: str,
    ) -> WalletCreation:
        account_type = AccountType.NATIVE_SEGWIT
        try:
            result = self.platform_wallet.import_wallet(
                name,
                password,
                mnemonic,
                address_type=account_type.value,
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        self._activate_wallet(name)
        # Imported recovery words are caller-owned input.  Never propagate
        # them into product DTOs, even if a future Platform implementation
        # accidentally populates the optional field.
        return self._creation_result(
            result,
            generated_mnemonic=None,
            account_type=account_type,
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
        wallet_name, account_type = self._require_active_selection()
        try:
            state = self.platform_wallet.rename_wallet(
                wallet_name,
                name,
                password,
                address_type=account_type.value,
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        with self._selection_lock:
            if self._wallet_name != wallet_name:
                raise ValueError(
                    "The active wallet changed while it was being renamed."
                )
            self.settings_store.set_active_wallet_selection(
                self.network,
                ActiveWalletSelection(name, account_type),
                expected_wallet_name=wallet_name,
            )
            self._wallet_name = name
            self._account_type = account_type
        return self._wallet_snapshot(state, account_type)

    def change_password(
        self,
        current_password: str | None,
        new_password: str | None,
    ) -> WalletSnapshot:
        wallet_name, account_type = self._require_active_selection()
        try:
            state = self.platform_wallet.change_password(
                wallet_name,
                current_password,
                new_password,
                address_type=account_type.value,
            )
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        return self._wallet_snapshot(state, account_type)

    def remove_wallet(self, password: str | None) -> WalletSnapshot:
        wallet_name, _account_type = self._require_active_selection()
        try:
            self.platform_wallet.remove_wallet(wallet_name, password)
        except WalletError as exc:
            raise ValueError(str(exc)) from exc
        remaining = self.list_wallets()
        selected_name = remaining[0].name if remaining else None
        self._activate_wallet(selected_name)
        return self.snapshot()

    def prepare_withdrawal(
        self,
        destination: str,
        amount: BitcoinAmount | None,
        fee_rate_sat_vb: int,
        *,
        send_all: bool = False,
    ) -> WithdrawalDraft:
        wallet_name, account_type = self._require_active_selection()
        try:
            draft = self.platform_payment.prepare(
                wallet_name,
                destination,
                None if amount is None else amount.sats,
                fee_rate_sat_vb,
                send_all=send_all,
                address_type=account_type.value,
            )
        except (TransactionError, WalletError) as exc:
            raise ValueError(str(exc)) from exc
        return WithdrawalDraft(
            draft_id=draft.draft_id,
            wallet_name=draft.wallet_name,
            network=draft.network,
            destination=draft.destination,
            amount=BitcoinAmount(draft.amount_sats),
            estimated_fee=BitcoinAmount(draft.estimated_fee_sats),
            fee_rate_sat_vb=draft.fee_rate_sat_vb,
            send_all=draft.send_all,
            account_type=self._validated_account_type(draft.address_type),
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
            review_id=payment.payment_id,
            wallet_name=payment.wallet_name,
            network=payment.network,
            txid=payment.txid,
            destination=payment.destination,
            amount=BitcoinAmount(payment.amount_sats),
            fee=BitcoinAmount(payment.fee_sats),
            fee_rate_sat_vb=payment.fee_rate_sat_vb,
            send_all=payment.send_all,
            account_type=self._validated_account_type(payment.address_type),
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
        try:
            self.platform_payment.cancel(review_id)
        except (TransactionError, WalletError) as exc:
            raise ValueError(str(exc)) from exc

    def _activate_wallet(self, name: str | None) -> None:
        """Activate a wallet at the product's Native SegWit default."""

        with self._selection_lock:
            self.settings_store.set_active_wallet(self.network, name)
            self._wallet_name = name
            self._account_type = AccountType.NATIVE_SEGWIT

    def _active_selection(self) -> tuple[str | None, AccountType]:
        with self._selection_lock:
            return self._wallet_name, self._account_type

    def _require_active_selection(self) -> tuple[str, AccountType]:
        wallet_name, account_type = self._active_selection()
        if wallet_name is None:
            raise ValueError("Create or import a wallet before continuing.")
        return wallet_name, account_type

    def _require_active_wallet(self) -> str:
        wallet_name, _account_type = self._require_active_selection()
        return wallet_name

    def _creation_result(
        self,
        result: PlatformWalletCreationResult,
        *,
        generated_mnemonic: str | None,
        account_type: AccountType,
    ) -> WalletCreation:
        return WalletCreation(
            snapshot=self._wallet_snapshot(result.state, account_type),
            generated_mnemonic=generated_mnemonic,
            discovery=self._discovery_summary(result.discovery),
        )

    def _wallet_snapshot(
        self,
        state: PlatformWalletState,
        account_type: AccountType,
    ) -> WalletSnapshot:
        return WalletSnapshot(
            name=state.metadata.name,
            balance=BitcoinAmount(state.effective_balance_sats),
            receive_address=state.receive_address,
            account_type=self._validated_account_type(account_type),
            authoritative_balance=BitcoinAmount(state.authoritative_balance_sats),
            confirmed_balance=BitcoinAmount(state.confirmed_balance_sats),
            unconfirmed_chain_balance=BitcoinAmount(
                state.unconfirmed_chain_balance_sats
            ),
            pending_delta=BitcoinAmount(state.pending_delta_sats),
            available_balance=BitcoinAmount(state.available_balance_sats),
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
    def _validated_account_type(value: AccountType | str) -> AccountType:
        try:
            return AccountType(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Unsupported wallet account type.") from exc

    @staticmethod
    def _discovery_summary(discovery) -> AddressDiscoverySummary | None:
        if discovery is None:
            return None
        return AddressDiscoverySummary(
            receive_scanned=discovery.receive.scanned_count,
            change_scanned=discovery.change.scanned_count,
            receive_used=len(discovery.receive.used_indexes),
            change_used=len(discovery.change.used_indexes),
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
