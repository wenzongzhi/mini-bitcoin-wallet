from types import SimpleNamespace

from state import WalletUIState


def test_confirmation_monitoring_uses_only_locally_broadcast_pending_txids() -> None:
    state = object.__new__(WalletUIState)
    state._snapshot = SimpleNamespace(
        pending_txids=("ab" * 32, "ab" * 32),
        transactions=(
            SimpleNamespace(txid="cd" * 32, confirmed=False),
        ),
    )

    assert state.monitored_transaction_ids == ("ab" * 32,)
