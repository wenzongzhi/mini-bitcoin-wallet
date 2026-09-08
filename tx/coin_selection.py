"""Deterministic largest-first coin selection and fee estimation."""

from decimal import Decimal, InvalidOperation, ROUND_CEILING
import re

from .codec import encode_compact_size
from .errors import TransactionError
from .model import TxOutput
from .script import classify_script_pubkey


DUST_THRESHOLDS = {
    "p2pkh": 546,
    "p2wpkh": 294,
}
MAX_REASONABLE_FEE_RATE = Decimal("10000")


def parse_fee_rate(value: str | int | Decimal) -> Decimal:
    try:
        fee_rate = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TransactionError("fee rate must be a decimal sat/vB value") from exc
    if not fee_rate.is_finite() or fee_rate <= 0:
        raise TransactionError("fee rate must be greater than zero")
    if fee_rate > MAX_REASONABLE_FEE_RATE:
        raise TransactionError(
            f"fee rate exceeds safety limit of {MAX_REASONABLE_FEE_RATE} sat/vB"
        )
    return fee_rate


def fee_for_vsize(vsize: int, fee_rate: Decimal) -> int:
    if isinstance(vsize, bool) or not isinstance(vsize, int) or vsize <= 0:
        raise TransactionError("vsize must be a positive integer")
    return int((fee_rate * vsize).to_integral_value(rounding=ROUND_CEILING))


def _output_size(output: TxOutput) -> int:
    return 8 + len(encode_compact_size(len(output.script_pubkey))) + len(output.script_pubkey)


def estimate_signed_vsize(
    input_count: int,
    address_type: str,
    outputs: list[TxOutput],
) -> int:
    if input_count <= 0:
        raise TransactionError("at least one input is required for fee estimation")
    if address_type not in {"p2pkh", "p2wpkh"}:
        raise TransactionError("fee estimation supports only p2pkh and p2wpkh inputs")
    output_size = sum(_output_size(output) for output in outputs)
    if address_type == "p2pkh":
        # Conservative maximum: 73-byte signature+sighash and compressed pubkey.
        return (
            4
            + len(encode_compact_size(input_count))
            + 149 * input_count
            + len(encode_compact_size(len(outputs)))
            + output_size
            + 4
        )
    stripped_size = (
        4
        + len(encode_compact_size(input_count))
        + 41 * input_count
        + len(encode_compact_size(len(outputs)))
        + output_size
        + 4
    )
    witness_size = 2 + 109 * input_count
    return (stripped_size * 4 + witness_size + 3) // 4


def _normalize_cached_utxo(utxo: dict) -> dict:
    if not isinstance(utxo, dict):
        raise TransactionError("wallet cache contains an invalid UTXO")
    required = (
        "txid",
        "vout",
        "value",
        "address",
        "path",
        "script_pubkey",
        "account_id",
        "address_type",
        "confirmations",
        "confirmed",
    )
    if any(key not in utxo for key in required):
        raise TransactionError("wallet cache UTXO metadata is incomplete")
    address_type = str(utxo["address_type"]).lower()
    normalized = {
        **utxo,
        "address_type": address_type,
        "outpoint": f"{utxo['txid']}:{utxo['vout']}",
    }
    if address_type not in {"p2pkh", "p2wpkh"}:
        return normalized
    try:
        bytes.fromhex(str(utxo["script_pubkey"]))
    except ValueError as exc:
        raise TransactionError("wallet cache UTXO scriptPubKey is invalid") from exc
    if (
        isinstance(utxo["value"], bool)
        or not isinstance(utxo["value"], int)
        or utxo["value"] <= 0
        or isinstance(utxo["vout"], bool)
        or not isinstance(utxo["vout"], int)
        or not 0 <= utxo["vout"] <= 0xFFFFFFFF
        or not isinstance(utxo["txid"], str)
        or not re.fullmatch(r"[0-9a-fA-F]{64}", utxo["txid"])
    ):
        raise TransactionError("wallet cache UTXO value or vout is invalid")
    return normalized


def select_coins(
    utxos: list[dict],
    destination_outputs: list[TxOutput],
    address_type: str,
    fee_rate: Decimal,
    *,
    min_confirmations: int = 1,
    include_outpoints: list[str] | None = None,
    exclude_outpoints: set[str] | None = None,
    reserved_outpoints: set[str] | None = None,
    max_fee_sats: int | None = None,
) -> dict:
    if not destination_outputs:
        raise TransactionError("transaction must contain at least one destination output")
    if any(output.value <= 0 for output in destination_outputs):
        raise TransactionError("destination output values must be greater than zero")
    if min_confirmations < 1:
        raise TransactionError("min confirmations must be at least 1")
    if max_fee_sats is not None and (
        isinstance(max_fee_sats, bool)
        or not isinstance(max_fee_sats, int)
        or max_fee_sats < 0
    ):
        raise TransactionError("max fee must be a non-negative integer")
    address_type = address_type.lower()
    if address_type not in DUST_THRESHOLDS:
        raise TransactionError("coin selection supports only p2pkh and p2wpkh")
    include_outpoints = include_outpoints or []
    exclude_outpoints = exclude_outpoints or set()
    reserved_outpoints = reserved_outpoints or set()
    if set(include_outpoints) & exclude_outpoints:
        raise TransactionError("the same UTXO cannot be both included and excluded")

    normalized = [_normalize_cached_utxo(utxo) for utxo in utxos]
    eligible = {
        utxo["outpoint"]: utxo
        for utxo in normalized
        if utxo["address_type"] == address_type
        and utxo.get("confirmed") is True
        and isinstance(utxo.get("confirmations"), int)
        and utxo["confirmations"] >= min_confirmations
        and utxo["outpoint"] not in exclude_outpoints
    }
    for outpoint in include_outpoints:
        if outpoint in reserved_outpoints:
            raise TransactionError(f'UTXO "{outpoint}" is already reserved')
        if outpoint not in eligible:
            raise TransactionError(f'included UTXO "{outpoint}" is not spendable')

    selected = []
    selected_outpoints = set()
    for outpoint in include_outpoints:
        if outpoint not in selected_outpoints:
            selected.append(eligible[outpoint])
            selected_outpoints.add(outpoint)
    candidates = sorted(
        (
            utxo
            for outpoint, utxo in eligible.items()
            if outpoint not in selected_outpoints and outpoint not in reserved_outpoints
        ),
        key=lambda item: (-item["value"], item["txid"], item["vout"]),
    )
    destination_total = sum(output.value for output in destination_outputs)
    change_script = b"\x00\x14" + b"\x00" * 20 if address_type == "p2wpkh" else b"\x76\xa9\x14" + b"\x00" * 20 + b"\x88\xac"
    change_template = TxOutput(0, change_script)

    def evaluate() -> dict | None:
        if not selected:
            return None
        total = sum(item["value"] for item in selected)
        vsize_with_change = estimate_signed_vsize(
            len(selected), address_type, [*destination_outputs, change_template]
        )
        fee_with_change = fee_for_vsize(vsize_with_change, fee_rate)
        change = total - destination_total - fee_with_change
        if change >= DUST_THRESHOLDS[address_type]:
            return {
                "change_sats": change,
                "estimated_fee_sats": fee_with_change,
                "estimated_vsize": vsize_with_change,
            }
        vsize_without_change = estimate_signed_vsize(
            len(selected), address_type, destination_outputs
        )
        minimum_fee = fee_for_vsize(vsize_without_change, fee_rate)
        remainder = total - destination_total
        if remainder >= minimum_fee:
            return {
                "change_sats": 0,
                "estimated_fee_sats": remainder,
                "estimated_vsize": vsize_without_change,
            }
        return None

    result = evaluate()
    while result is None and candidates:
        selected.append(candidates.pop(0))
        result = evaluate()
    if result is None:
        available = sum(item["value"] for item in eligible.values() if item["outpoint"] not in reserved_outpoints)
        minimum_vsize = estimate_signed_vsize(
            max(len(selected), 1), address_type, destination_outputs
        )
        minimum_fee = fee_for_vsize(minimum_vsize, fee_rate)
        raise TransactionError(
            "insufficient funds: "
            f"available={available} sats, target={destination_total} sats, "
            f"estimated_fee={minimum_fee} sats"
        )
    if max_fee_sats is not None and result["estimated_fee_sats"] > max_fee_sats:
        raise TransactionError(
            f'estimated fee {result["estimated_fee_sats"]} exceeds max fee {max_fee_sats}'
        )
    return {
        **result,
        "selected": selected,
        "total_input_sats": sum(item["value"] for item in selected),
        "destination_total_sats": destination_total,
    }


def select_all_coins(
    utxos: list[dict],
    destination_script_pubkey: bytes,
    address_type: str,
    fee_rate: Decimal,
    *,
    min_confirmations: int = 1,
    exclude_outpoints: set[str] | None = None,
    reserved_outpoints: set[str] | None = None,
    max_fee_sats: int | None = None,
) -> dict:
    """Select every eligible UTXO and subtract the fee from one destination output."""
    if min_confirmations < 1:
        raise TransactionError("min confirmations must be at least 1")
    if max_fee_sats is not None and (
        isinstance(max_fee_sats, bool)
        or not isinstance(max_fee_sats, int)
        or max_fee_sats < 0
    ):
        raise TransactionError("max fee must be a non-negative integer")
    address_type = address_type.lower()
    if address_type not in DUST_THRESHOLDS:
        raise TransactionError("coin selection supports only p2pkh and p2wpkh")
    if not isinstance(destination_script_pubkey, bytes) or not destination_script_pubkey:
        raise TransactionError("destination scriptPubKey is invalid")

    exclude_outpoints = exclude_outpoints or set()
    reserved_outpoints = reserved_outpoints or set()
    selected = sorted(
        (
            utxo
            for utxo in (_normalize_cached_utxo(item) for item in utxos)
            if utxo["address_type"] == address_type
            and utxo.get("confirmed") is True
            and isinstance(utxo.get("confirmations"), int)
            and utxo["confirmations"] >= min_confirmations
            and utxo["outpoint"] not in exclude_outpoints
            and utxo["outpoint"] not in reserved_outpoints
        ),
        key=lambda item: (-item["value"], item["txid"], item["vout"]),
    )
    if not selected:
        raise TransactionError("wallet has no eligible UTXOs to send")

    output_template = TxOutput(0, destination_script_pubkey)
    estimated_vsize = estimate_signed_vsize(
        len(selected),
        address_type,
        [output_template],
    )
    estimated_fee = fee_for_vsize(estimated_vsize, fee_rate)
    if max_fee_sats is not None and estimated_fee > max_fee_sats:
        raise TransactionError(
            f"estimated fee {estimated_fee} sats exceeds max fee {max_fee_sats} sats"
        )

    total_input = sum(item["value"] for item in selected)
    destination_value = total_input - estimated_fee
    if destination_value <= 0:
        raise TransactionError(
            "insufficient funds: "
            f"available={total_input} sats, estimated_fee={estimated_fee} sats"
        )
    destination_type = classify_script_pubkey(destination_script_pubkey)
    if destination_type is None:
        raise TransactionError("sendall supports only P2PKH and P2WPKH destinations")
    if destination_value < DUST_THRESHOLDS[destination_type]:
        raise TransactionError(
            f"sendall destination would be dust ({destination_value} sats)"
        )

    return {
        "selected": selected,
        "total_input_sats": total_input,
        "destination_total_sats": destination_value,
        "estimated_fee_sats": estimated_fee,
        "estimated_vsize": estimated_vsize,
    }
