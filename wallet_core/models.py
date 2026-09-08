"""Small, immutable values shared by every wallet implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum


SATS_PER_BTC = 100_000_000


class DisplayUnit(str, Enum):
    BTC = "BTC"
    SATS = "Sats"


class TransactionDirection(str, Enum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    SELF = "self"


@dataclass(frozen=True, slots=True)
class BitcoinAmount:
    """A Bitcoin amount stored exactly as satoshis."""

    sats: int

    def __post_init__(self) -> None:
        if isinstance(self.sats, bool) or not isinstance(self.sats, int):
            raise TypeError("satoshi amount must be an integer")

    @classmethod
    def parse(cls, text: str, unit: DisplayUnit) -> BitcoinAmount:
        normalized = text.strip().replace(",", "")
        if not normalized:
            raise ValueError("Enter an amount.")
        try:
            value = Decimal(normalized)
        except InvalidOperation as exc:
            raise ValueError("Amount must be a number.") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError("Amount must be greater than zero.")

        satoshis = value if unit is DisplayUnit.SATS else value * SATS_PER_BTC
        if satoshis != satoshis.to_integral_value():
            raise ValueError("Amount cannot contain a fraction of a satoshi.")
        return cls(int(satoshis))

    def format(self, unit: DisplayUnit, *, signed: bool = False) -> str:
        prefix = "+ " if signed and self.sats >= 0 else "- " if self.sats < 0 else ""
        absolute = abs(self.sats)
        if unit is DisplayUnit.SATS:
            return f"{prefix}{absolute:,}"
        btc = Decimal(absolute) / SATS_PER_BTC
        digits = format(btc, "f").rstrip("0").rstrip(".") or "0"
        return f"{prefix}{digits}"


@dataclass(frozen=True, slots=True)
class TransactionSummary:
    """Complete wallet-facing metadata for one synchronized transaction."""

    txid: str
    amount: BitcoinAmount
    direction: TransactionDirection
    received: BitcoinAmount
    sent: BitcoinAmount
    fee: BitcoinAmount
    confirmed: bool
    confirmations: int
    block_height: int | None
    block_time: datetime | None
    addresses: tuple[str, ...]
    account_ids: tuple[str, ...]
    address_types: tuple[str, ...]
    explorer_url: str


@dataclass(frozen=True, slots=True)
class WalletSummary:
    """Non-secret metadata used to list wallets without unlocking them."""

    name: str
    network: str
    encrypted: bool
    master_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class WalletSnapshot:
    name: str
    balance: BitcoinAmount
    receive_address: str
    network: str = "mainnet"
    transactions: tuple[TransactionSummary, ...] = ()
    is_initialized: bool = True


@dataclass(frozen=True, slots=True)
class WalletCreation:
    snapshot: WalletSnapshot
    mnemonic: str
    imported: bool


@dataclass(frozen=True, slots=True)
class SendPreview:
    destination: str
    amount: BitcoinAmount
    fee: BitcoinAmount
    fee_rate_sat_vb: int

    @property
    def total(self) -> BitcoinAmount:
        return BitcoinAmount(self.amount.sats + self.fee.sats)


@dataclass(frozen=True, slots=True)
class WithdrawalReview:
    """Exact signed transaction values awaiting broadcast confirmation."""

    review_id: str
    wallet_name: str
    network: str
    txid: str
    destination: str
    amount: BitcoinAmount
    fee: BitcoinAmount
    fee_rate_sat_vb: int
    send_all: bool

    @property
    def total(self) -> BitcoinAmount:
        return BitcoinAmount(self.amount.sats + self.fee.sats)


@dataclass(frozen=True, slots=True)
class BroadcastResult:
    """Public result returned after a transaction broadcast attempt succeeds."""

    txid: str
    explorer_url: str
    cache_warning: str | None = None
