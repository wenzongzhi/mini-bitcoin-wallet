"""Wallet transaction signing for P2PKH and native P2WPKH inputs."""

from decimal import Decimal

from wallet import WalletError, WalletSigningSession

from .codec import transaction_metrics
from .errors import TransactionError
from .model import Prevout, Transaction
from .script import push_data
from .sighash import SIGHASH_ALL, bip143_sighash_all, legacy_sighash_all
from .verifier import verify_all_inputs


def sign_transaction(
    tx: Transaction,
    prevouts: list[Prevout],
    signing_session: WalletSigningSession,
) -> dict:
    if len(tx.inputs) != len(prevouts):
        raise TransactionError("prevout count does not match transaction inputs")
    address_types = {prevout.address_type for prevout in prevouts}
    if len(address_types) != 1 or not address_types <= {"p2pkh", "p2wpkh"}:
        raise TransactionError("mixed or unsupported transaction input types")

    for index, (tx_input, prevout) in enumerate(zip(tx.inputs, prevouts)):
        if tx_input.txid != prevout.txid or tx_input.vout != prevout.vout:
            raise TransactionError("prevout order does not match transaction inputs")
        tx_input.script_sig = b""
        tx_input.witness = []
        digest = (
            bip143_sighash_all(tx, index, prevout)
            if prevout.address_type == "p2wpkh"
            else legacy_sighash_all(tx, index, prevout)
        )
        metadata = {
            "address": prevout.address,
            "address_type": prevout.address_type,
            "derivation_path": prevout.derivation_path,
            "account_id": prevout.account_id,
            "script_pubkey": prevout.script_pubkey.hex(),
        }
        try:
            signature_der, public_key = signing_session.sign_digest(metadata, digest)
        except WalletError as exc:
            raise TransactionError(str(exc)) from exc
        signature = signature_der + bytes([SIGHASH_ALL])
        if prevout.address_type == "p2wpkh":
            tx_input.witness = [signature, public_key]
        else:
            tx_input.script_sig = push_data(signature) + push_data(public_key)

    verification = verify_all_inputs(tx, prevouts)
    complete = all(item["valid"] for item in verification)
    if not complete:
        raise TransactionError("local transaction signature verification failed")
    metrics = transaction_metrics(tx)
    input_total = sum(prevout.value for prevout in prevouts)
    output_total = sum(output.value for output in tx.outputs)
    fee = input_total - output_total
    if fee < 0:
        raise TransactionError("transaction outputs exceed known input value")
    return {
        **metrics,
        "complete": True,
        "fee_sats": fee,
        "fee_rate_sat_vb": format(Decimal(fee) / metrics["vsize"], "f"),
        "signed_input_count": len(prevouts),
        "input_count": len(tx.inputs),
        "verification": verification,
        "errors": [],
    }
