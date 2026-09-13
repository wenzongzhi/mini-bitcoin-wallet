"""High-level payment lifecycle service for bitcoin-tool consumers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from threading import RLock

from network import EsploraError
from wallet import WalletError, get_wallet_address_book
from wallet.service import WalletService
from wallet.transaction_accounting import (
    pending_summary_from_signed,
    upsert_cached_transaction,
)
from wallet.wallet_cache import load_wallet_cache, locked_cache_file, save_wallet_cache

from .builder import create_raw_transaction
from .errors import TransactionError
from .workflow import (
    broadcast_signed_transaction,
    fund_all_transaction,
    fund_transaction,
    sign_funded_transaction,
)


@dataclass(frozen=True, slots=True)
class FeeEstimate:
    target_blocks: int
    sat_vb: Decimal


@dataclass(frozen=True, slots=True)
class PaymentDraft:
    draft_id: str
    wallet_name: str
    network: str
    destination: str
    amount_sats: int
    estimated_fee_sats: int
    fee_rate_sat_vb: int
    send_all: bool


@dataclass(frozen=True, slots=True)
class SignedPayment:
    payment_id: str
    wallet_name: str
    network: str
    txid: str
    destination: str
    amount_sats: int
    fee_sats: int
    fee_rate_sat_vb: int
    send_all: bool


@dataclass(frozen=True, slots=True)
class BroadcastReceipt:
    txid: str
    network: str
    cache_warning: str | None = None


class PaymentService:
    """Own prepare, sign, broadcast, cancellation, and reservation state."""

    def __init__(self, wallet_service: WalletService) -> None:
        self.wallet_service = wallet_service
        self._lock: RLock = wallet_service.operation_lock
        self._funded: dict[str, dict] = {}
        self._signed: dict[str, dict] = {}

    def estimate_fee(self) -> tuple[FeeEstimate, ...]:
        """Return backend fee estimates ordered by confirmation target."""

        try:
            estimates = self.wallet_service.backend_factory(
                self.wallet_service.network
            ).get_fee_estimates()
        except (AttributeError, EsploraError) as exc:
            raise TransactionError(str(exc)) from exc
        result = []
        for target, rate in estimates.items():
            try:
                target_blocks = int(target)
                normalized_rate = Decimal(rate)
            except (TypeError, ValueError) as exc:
                raise TransactionError("backend returned invalid fee estimates") from exc
            if target_blocks <= 0 or normalized_rate <= 0:
                raise TransactionError("backend returned invalid fee estimates")
            result.append(FeeEstimate(target_blocks, normalized_rate))
        return tuple(sorted(result, key=lambda item: item.target_blocks))

    def prepare(
        self,
        wallet_name: str,
        destination: str,
        amount_sats: int | None,
        fee_rate_sat_vb: int,
        *,
        send_all: bool = False,
    ) -> PaymentDraft:
        """Validate, refresh spendable state, and reserve funded inputs."""

        if not destination:
            raise TransactionError("destination address is required")
        if isinstance(fee_rate_sat_vb, bool) or fee_rate_sat_vb <= 0:
            raise TransactionError("fee rate must be a positive integer")
        if not send_all and (
            isinstance(amount_sats, bool)
            or not isinstance(amount_sats, int)
            or amount_sats <= 0
        ):
            raise TransactionError("amount must be a positive integer number of satoshis")

        with self._lock:
            funded = None
            try:
                create_raw_transaction(
                    [],
                    [{"address": destination, "amount_sats": 1}],
                    self.wallet_service.network,
                )
                self.wallet_service.sync_wallet(
                    wallet_name,
                    include_transactions=False,
                )
                if send_all:
                    funded = fund_all_transaction(
                        destination,
                        wallet_name,
                        self.wallet_service.cache_file,
                        self.wallet_service.network,
                        self.wallet_service.address_type,
                        fee_rate_sat_vb,
                        utxo_source="fresh backend synchronization",
                    )
                else:
                    template = create_raw_transaction(
                        [],
                        [{"address": destination, "amount_sats": amount_sats}],
                        self.wallet_service.network,
                    )
                    funded = fund_transaction(
                        template,
                        wallet_name,
                        self.wallet_service.wallet_file,
                        self.wallet_service.cache_file,
                        self.wallet_service.network,
                        self.wallet_service.address_type,
                        fee_rate_sat_vb,
                        utxo_source="fresh backend synchronization",
                    )
            except (TransactionError, WalletError, EsploraError):
                if funded is not None:
                    self.release_reservation(wallet_name, funded.get("draft_id"))
                raise

            draft_id = funded["draft_id"]
            destination_sats = sum(
                output["value"]
                for output in funded["outputs"]
                if output.get("is_change") is False
            )
            self._funded[draft_id] = {
                "document": funded,
                "destination": destination,
                "fee_rate_sat_vb": fee_rate_sat_vb,
                "send_all": send_all,
            }
            return PaymentDraft(
                draft_id,
                wallet_name,
                self.wallet_service.network,
                destination,
                destination_sats,
                funded["estimated_fee_sats"],
                fee_rate_sat_vb,
                send_all,
            )

    def sign(self, draft_id: str, password: str | None) -> SignedPayment:
        """Sign a prepared draft and retain it until broadcast or cancellation."""

        with self._lock:
            prepared = self._funded.get(draft_id)
            if prepared is None:
                raise TransactionError("payment draft has expired or does not exist")
            funded = prepared["document"]
            try:
                signed = sign_funded_transaction(
                    funded,
                    funded["wallet_name"],
                    password,
                    self.wallet_service.wallet_file,
                    self.wallet_service.cache_file,
                    self.wallet_service.network,
                )
            except (TransactionError, WalletError):
                self._funded.pop(draft_id, None)
                self.release_reservation(funded["wallet_name"], draft_id)
                raise

            self._funded.pop(draft_id, None)
            self._signed[draft_id] = signed
            destination_sats = sum(
                output["value"]
                for output in signed["outputs"]
                if output.get("is_change") is False
            )
            return SignedPayment(
                draft_id,
                signed["wallet_name"],
                self.wallet_service.network,
                signed["txid"],
                prepared["destination"],
                destination_sats,
                signed["fee_sats"],
                prepared["fee_rate_sat_vb"],
                prepared["send_all"],
            )

    def broadcast(self, payment_id: str) -> BroadcastReceipt:
        """Broadcast a signed payment and persist its canonical pending summary."""

        with self._lock:
            signed = self._signed.get(payment_id)
            if signed is None:
                raise TransactionError("signed payment has expired or does not exist")
            backend = self.wallet_service.backend_factory(
                self.wallet_service.network
            )
            result = broadcast_signed_transaction(
                signed,
                self.wallet_service.network,
                backend,
                cache_file=self.wallet_service.cache_file,
            )
            cache_warning = result.get("cache_warning")
            if cache_warning is None:
                try:
                    address_book = get_wallet_address_book(
                        signed["wallet_name"],
                        wallet_file=self.wallet_service.wallet_file,
                        network=self.wallet_service.network,
                    )
                    owned = {
                        item["address"]: item
                        for item in address_book["addresses"]
                    }
                    summary = pending_summary_from_signed(signed, owned)
                    upsert_cached_transaction(
                        signed["wallet_name"],
                        summary,
                        self.wallet_service.cache_file,
                    )
                except WalletError as exc:
                    cache_warning = str(exc)
            self._signed.pop(payment_id, None)
            return BroadcastReceipt(
                result["txid"],
                self.wallet_service.network,
                cache_warning,
            )

    def cancel(self, payment_id: str) -> None:
        """Cancel either a funded or signed payment and release its reservation."""

        with self._lock:
            funded = self._funded.pop(payment_id, None)
            signed = self._signed.pop(payment_id, None)
            document = signed or (
                funded["document"] if funded is not None else None
            )
            if document is not None:
                self.release_reservation(
                    document["wallet_name"],
                    document.get("draft_id"),
                )

    def release_reservation(self, wallet_name: str, draft_id: str | None) -> None:
        """Public, idempotent release API for abandoned platform drafts."""

        if not draft_id or not self.wallet_service.cache_file.exists():
            return
        with locked_cache_file(self.wallet_service.cache_file):
            cache = load_wallet_cache(self.wallet_service.cache_file)
            wallet_cache = cache.get("wallets", {}).get(wallet_name)
            if not isinstance(wallet_cache, dict):
                return
            reservations = wallet_cache.get("reserved_outpoints", {})
            if not isinstance(reservations, dict):
                raise TransactionError("wallet UTXO reservations are invalid")
            remaining = {
                outpoint: reservation
                for outpoint, reservation in reservations.items()
                if not isinstance(reservation, dict)
                or reservation.get("draft_id") != draft_id
            }
            if remaining != reservations:
                wallet_cache["reserved_outpoints"] = remaining
                save_wallet_cache(cache, self.wallet_service.cache_file)
