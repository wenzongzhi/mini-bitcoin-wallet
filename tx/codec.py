"""Bitcoin transaction CompactSize and wire serialization."""

import hashlib
import struct

from .errors import TransactionError
from .model import Transaction, TxInput, TxOutput


def encode_compact_size(value: int) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise TransactionError("CompactSize value must be a uint64")
    if value < 253:
        return bytes([value])
    if value <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", value)
    if value <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", value)
    return b"\xff" + struct.pack("<Q", value)


class ByteReader:
    def __init__(self, data: bytes):
        if not isinstance(data, bytes):
            raise TransactionError("transaction data must be bytes")
        self.data = data
        self.offset = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset

    def read(self, length: int) -> bytes:
        if length < 0 or self.remaining < length:
            raise TransactionError("truncated transaction data")
        result = self.data[self.offset : self.offset + length]
        self.offset += length
        return result

    def read_compact_size(self) -> int:
        prefix = self.read(1)[0]
        if prefix < 253:
            return prefix
        if prefix == 253:
            value = struct.unpack("<H", self.read(2))[0]
            if value < 253:
                raise TransactionError("non-canonical CompactSize encoding")
            return value
        if prefix == 254:
            value = struct.unpack("<I", self.read(4))[0]
            if value <= 0xFFFF:
                raise TransactionError("non-canonical CompactSize encoding")
            return value
        value = struct.unpack("<Q", self.read(8))[0]
        if value <= 0xFFFFFFFF:
            raise TransactionError("non-canonical CompactSize encoding")
        return value


def decode_compact_size(data: bytes, offset: int = 0) -> tuple[int, int]:
    reader = ByteReader(data)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise TransactionError("CompactSize offset must be non-negative")
    reader.offset = offset
    value = reader.read_compact_size()
    return value, reader.offset


def serialize_outpoint(tx_input: TxInput) -> bytes:
    return bytes.fromhex(tx_input.txid)[::-1] + struct.pack("<I", tx_input.vout)


def serialize_tx_output(tx_output: TxOutput) -> bytes:
    return (
        struct.pack("<Q", tx_output.value)
        + encode_compact_size(len(tx_output.script_pubkey))
        + tx_output.script_pubkey
    )


def serialize_transaction(tx: Transaction, include_witness: bool = True) -> bytes:
    use_witness = include_witness and tx.has_witness
    result = bytearray(struct.pack("<i", tx.version))
    if use_witness:
        result.extend(b"\x00\x01")
    result.extend(encode_compact_size(len(tx.inputs)))
    for tx_input in tx.inputs:
        result.extend(serialize_outpoint(tx_input))
        result.extend(encode_compact_size(len(tx_input.script_sig)))
        result.extend(tx_input.script_sig)
        result.extend(struct.pack("<I", tx_input.sequence))
    result.extend(encode_compact_size(len(tx.outputs)))
    for tx_output in tx.outputs:
        result.extend(serialize_tx_output(tx_output))
    if use_witness:
        for tx_input in tx.inputs:
            result.extend(encode_compact_size(len(tx_input.witness)))
            for item in tx_input.witness:
                result.extend(encode_compact_size(len(item)))
                result.extend(item)
    result.extend(struct.pack("<I", tx.locktime))
    return bytes(result)


def serialize_transaction_hex(tx: Transaction, include_witness: bool = True) -> str:
    return serialize_transaction(tx, include_witness).hex()


def _read_inputs(reader: ByteReader, count: int) -> list[TxInput]:
    inputs = []
    for _ in range(count):
        txid = reader.read(32)[::-1].hex()
        vout = struct.unpack("<I", reader.read(4))[0]
        script_length = reader.read_compact_size()
        script_sig = reader.read(script_length)
        sequence = struct.unpack("<I", reader.read(4))[0]
        inputs.append(TxInput(txid, vout, sequence, script_sig))
    return inputs


def _read_outputs(reader: ByteReader, count: int) -> list[TxOutput]:
    outputs = []
    for _ in range(count):
        value = struct.unpack("<Q", reader.read(8))[0]
        script_length = reader.read_compact_size()
        outputs.append(TxOutput(value, reader.read(script_length)))
    return outputs


def _deserialize(data: bytes, force_legacy: bool = False) -> Transaction:
    reader = ByteReader(data)
    version = struct.unpack("<i", reader.read(4))[0]
    input_count = reader.read_compact_size()
    segwit = False
    if not force_legacy and input_count == 0 and reader.remaining:
        flag = reader.read(1)[0]
        if flag == 0:
            raise TransactionError("invalid SegWit flag")
        if flag != 1:
            raise TransactionError("unsupported transaction optional data flag")
        segwit = True
        input_count = reader.read_compact_size()
    inputs = _read_inputs(reader, input_count)
    output_count = reader.read_compact_size()
    outputs = _read_outputs(reader, output_count)
    if segwit:
        for tx_input in inputs:
            item_count = reader.read_compact_size()
            tx_input.witness = [
                reader.read(reader.read_compact_size()) for _ in range(item_count)
            ]
        if not any(tx_input.witness for tx_input in inputs):
            raise TransactionError("superfluous SegWit marker and flag")
    locktime = struct.unpack("<I", reader.read(4))[0]
    if reader.remaining:
        raise TransactionError("trailing bytes after transaction")
    return Transaction(version, inputs, outputs, locktime)


def deserialize_transaction(data: bytes) -> Transaction:
    try:
        return _deserialize(data)
    except TransactionError as segwit_error:
        # An unsigned zero-input template begins with 00 followed by its output
        # count, which is ambiguous with the SegWit marker and flag.
        if len(data) >= 6 and data[4] == 0:
            try:
                return _deserialize(data, force_legacy=True)
            except TransactionError:
                pass
        raise segwit_error


def deserialize_transaction_hex(raw_hex: str) -> Transaction:
    if not isinstance(raw_hex, str) or not raw_hex:
        raise TransactionError("raw transaction hex is required")
    try:
        data = bytes.fromhex(raw_hex)
    except ValueError as exc:
        raise TransactionError("raw transaction contains invalid hex") from exc
    return deserialize_transaction(data)


def double_sha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def transaction_txid(tx: Transaction) -> str:
    return double_sha256(serialize_transaction(tx, include_witness=False))[::-1].hex()


def transaction_wtxid(tx: Transaction) -> str:
    return double_sha256(serialize_transaction(tx, include_witness=True))[::-1].hex()


def transaction_metrics(tx: Transaction) -> dict:
    stripped = serialize_transaction(tx, include_witness=False)
    full = serialize_transaction(tx, include_witness=True)
    stripped_size = len(stripped)
    size = len(full)
    weight = stripped_size * 4 + (size - stripped_size)
    return {
        "txid": transaction_txid(tx),
        "wtxid": transaction_wtxid(tx),
        "size": size,
        "stripped_size": stripped_size,
        "weight": weight,
        "vsize": (weight + 3) // 4,
        "segwit": tx.has_witness,
    }
