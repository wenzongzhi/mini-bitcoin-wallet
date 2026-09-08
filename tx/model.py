"""Typed Bitcoin transaction data models."""

from dataclasses import dataclass, field
import re

from .errors import TransactionError


UINT32_MAX = 0xFFFFFFFF
UINT64_MAX = 0xFFFFFFFFFFFFFFFF
TXID_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


def _require_uint(value: int, maximum: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise TransactionError(f"{name} must be an integer in range 0..{maximum}")


@dataclass
class TxInput:
    txid: str
    vout: int
    sequence: int = 0xFFFFFFFD
    script_sig: bytes = b""
    witness: list[bytes] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.txid, str) or not TXID_PATTERN.fullmatch(self.txid):
            raise TransactionError("txid must be exactly 64 hexadecimal characters")
        self.txid = self.txid.lower()
        _require_uint(self.vout, UINT32_MAX, "vout")
        _require_uint(self.sequence, UINT32_MAX, "sequence")
        if not isinstance(self.script_sig, bytes):
            raise TransactionError("script_sig must be bytes")
        if not isinstance(self.witness, list) or any(
            not isinstance(item, bytes) for item in self.witness
        ):
            raise TransactionError("witness must be a list of byte strings")

    @property
    def outpoint(self) -> str:
        return f"{self.txid}:{self.vout}"


@dataclass
class TxOutput:
    value: int
    script_pubkey: bytes

    def __post_init__(self) -> None:
        _require_uint(self.value, UINT64_MAX, "output value")
        if not isinstance(self.script_pubkey, bytes):
            raise TransactionError("script_pubkey must be bytes")


@dataclass
class Transaction:
    version: int = 2
    inputs: list[TxInput] = field(default_factory=list)
    outputs: list[TxOutput] = field(default_factory=list)
    locktime: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TransactionError("transaction version must be an integer")
        if not -(2**31) <= self.version < 2**31:
            raise TransactionError("transaction version must fit int32")
        _require_uint(self.locktime, UINT32_MAX, "locktime")
        if not isinstance(self.inputs, list) or any(
            not isinstance(item, TxInput) for item in self.inputs
        ):
            raise TransactionError("inputs must be a list of TxInput objects")
        if not isinstance(self.outputs, list) or any(
            not isinstance(item, TxOutput) for item in self.outputs
        ):
            raise TransactionError("outputs must be a list of TxOutput objects")
        outpoints = [item.outpoint for item in self.inputs]
        if len(outpoints) != len(set(outpoints)):
            raise TransactionError("transaction contains duplicate inputs")

    @property
    def has_witness(self) -> bool:
        return any(item.witness for item in self.inputs)


@dataclass(frozen=True)
class Prevout:
    txid: str
    vout: int
    value: int
    script_pubkey: bytes
    address: str
    address_type: str
    derivation_path: str
    account_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.txid, str) or not TXID_PATTERN.fullmatch(self.txid):
            raise TransactionError("prevout txid must be 64 hexadecimal characters")
        object.__setattr__(self, "txid", self.txid.lower())
        _require_uint(self.vout, UINT32_MAX, "prevout vout")
        _require_uint(self.value, UINT64_MAX, "prevout value")
        if not isinstance(self.script_pubkey, bytes) or not self.script_pubkey:
            raise TransactionError("prevout script_pubkey must be non-empty bytes")
        if not isinstance(self.address, str) or not self.address:
            raise TransactionError("prevout address is required")
        if self.address_type not in {"p2pkh", "p2wpkh"}:
            raise TransactionError("prevout address_type must be p2pkh or p2wpkh")
        if not isinstance(self.derivation_path, str) or not self.derivation_path:
            raise TransactionError("prevout derivation_path is required")

    @property
    def outpoint(self) -> str:
        return f"{self.txid}:{self.vout}"
