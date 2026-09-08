"""Framework-independent wallet application core.

UI code should import from this package instead of depending on a concrete
Bitcoin implementation.  A production adapter can therefore replace the demo
service without changing the pages.
"""

from .application import WalletApplication
from .models import (
    BitcoinAmount,
    BroadcastResult,
    DisplayUnit,
    SendPreview,
    TransactionStatus,
    TransactionDirection,
    TransactionSummary,
    WalletCreation,
    WalletSnapshot,
    WalletSummary,
    WithdrawalDraft,
    WithdrawalReview,
)
from .ports import WalletService

__all__ = [
    "BitcoinAmount",
    "BroadcastResult",
    "DisplayUnit",
    "SendPreview",
    "TransactionStatus",
    "TransactionDirection",
    "TransactionSummary",
    "WalletApplication",
    "WalletCreation",
    "WalletService",
    "WalletSnapshot",
    "WalletSummary",
    "WithdrawalDraft",
    "WithdrawalReview",
]
