from unittest.mock import Mock, call

from sync_coordinator import SyncPolicy, WalletSyncCoordinator
from wallet_core import TransactionStatus


class FakeRoot:
    def __init__(self) -> None:
        self.bindings = {}
        self.scheduled = []

    def bind(self, event_name, callback, add=None):
        self.bindings[event_name] = callback

    def after(self, delay, callback):
        job = (delay, callback)
        self.scheduled.append(job)
        return job

    def after_cancel(self, job):
        self.scheduled.remove(job)


class FakeState:
    def __init__(self) -> None:
        self.active_wallet_name = "wallet"
        self.monitored_transaction_ids = ()
        self.set_sync_activity = Mock()


def test_start_schedules_one_startup_scan_and_only_the_status_timer() -> None:
    root = FakeRoot()
    coordinator = WalletSyncCoordinator(root, FakeState(), SyncPolicy())

    coordinator.start()

    assert [delay for delay, _callback in root.scheduled] == [250, 30_000]


def test_wallet_change_and_manual_refresh_always_request_a_full_sync() -> None:
    coordinator = WalletSyncCoordinator(FakeRoot(), FakeState(), SyncPolicy())
    coordinator.request_full_sync = Mock()

    coordinator._wallet_changed()
    coordinator._manual_refresh()

    assert coordinator.request_full_sync.call_args_list == [
        call("wallet-change"),
        call("manual"),
    ]


def test_broadcast_starts_confirmation_monitoring_without_a_full_sync() -> None:
    state = FakeState()
    coordinator = WalletSyncCoordinator(FakeRoot(), state, SyncPolicy())
    coordinator.request_full_sync = Mock()

    coordinator._broadcast_completed()

    coordinator.request_full_sync.assert_not_called()
    state.set_sync_activity.assert_called_once_with(
        False,
        "Broadcast accepted · monitoring confirmation",
    )


def test_successful_full_sync_applies_snapshot_and_reports_update_time() -> None:
    root = FakeRoot()
    state = FakeState()
    state.apply_synchronized_snapshot = Mock(return_value=True)
    coordinator = WalletSyncCoordinator(root, state, SyncPolicy())
    snapshot = object()
    coordinator._worker_active = True
    coordinator._results.put(("full", "wallet", snapshot, None))

    coordinator._poll_result()

    state.apply_synchronized_snapshot.assert_called_once_with(snapshot)
    assert state.set_sync_activity.call_args.args[0] is False
    assert state.set_sync_activity.call_args.args[1].startswith("Updated ")


def test_confirmed_transaction_stops_status_polling_before_final_sync() -> None:
    txid = "ab" * 32
    root = FakeRoot()
    state = FakeState()
    state.monitored_transaction_ids = (txid,)
    coordinator = WalletSyncCoordinator(root, state, SyncPolicy())
    coordinator.request_full_sync = Mock()
    coordinator._worker_active = True
    coordinator._results.put(
        (
            "status",
            "wallet",
            (TransactionStatus(txid=txid, confirmed=True),),
            None,
        )
    )

    coordinator._poll_result()
    coordinator._status_check()

    coordinator.request_full_sync.assert_called_once_with("transaction-confirmed")
    assert coordinator._worker_active is False
    assert ("wallet", txid) in coordinator._confirmed_transactions
