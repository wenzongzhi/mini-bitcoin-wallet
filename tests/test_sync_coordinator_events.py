from unittest.mock import Mock, call

from sync_coordinator import SyncPolicy, WalletSyncCoordinator
from wallet_core import TransactionStatus, WalletReceiveActivity


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
        self.application = Mock()
        self.receive_activity_baseline = None


def receive_activity(*, chain_transactions=0, mempool_transactions=0):
    return WalletReceiveActivity(
        address_count=2,
        chain_tx_count=chain_transactions,
        chain_funded_txo_count=chain_transactions,
        chain_spent_txo_count=0,
        chain_funded_sats=chain_transactions * 10_000,
        chain_spent_sats=0,
        mempool_tx_count=mempool_transactions,
        mempool_funded_txo_count=mempool_transactions,
        mempool_spent_txo_count=0,
        mempool_funded_sats=mempool_transactions * 10_000,
        mempool_spent_sats=0,
    )


def test_start_schedules_startup_sync_and_both_lightweight_monitors() -> None:
    root = FakeRoot()
    coordinator = WalletSyncCoordinator(root, FakeState(), SyncPolicy())

    coordinator.start()

    assert [delay for delay, _callback in root.scheduled] == [
        250,
        30_000,
        60_000,
    ]


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

    coordinator.request_full_sync.assert_called_once_with(
        "transaction-confirmed",
        wallet_name="wallet",
    )
    assert coordinator._worker_active is False
    assert ("wallet", txid) in coordinator._confirmed_transactions


def test_receive_probe_uses_an_independent_timer_and_application_api() -> None:
    root = FakeRoot()
    state = FakeState()
    coordinator = WalletSyncCoordinator(root, state, SyncPolicy())
    coordinator._start_worker = Mock()

    coordinator._activity_check()

    kind, wallet_name, operation = coordinator._start_worker.call_args.args
    assert (kind, wallet_name) == ("activity", "wallet")
    assert operation == state.application.receive_activity
    assert root.scheduled[-1][0] == 60_000


def test_changed_receive_activity_requests_one_full_sync() -> None:
    state = FakeState()
    baseline = receive_activity(chain_transactions=1)
    changed = receive_activity(chain_transactions=1, mempool_transactions=1)
    state.receive_activity_baseline = baseline
    coordinator = WalletSyncCoordinator(FakeRoot(), state, SyncPolicy())
    coordinator.request_full_sync = Mock()
    coordinator._worker_active = True
    coordinator._results.put(("activity", "wallet", changed, None))

    coordinator._poll_result()

    coordinator.request_full_sync.assert_called_once_with(
        "incoming-activity",
        wallet_name="wallet",
    )
    assert coordinator._receive_activity_baselines["wallet"] == baseline


def test_unchanged_receive_activity_does_not_synchronize() -> None:
    state = FakeState()
    baseline = receive_activity(chain_transactions=1)
    state.receive_activity_baseline = baseline
    coordinator = WalletSyncCoordinator(FakeRoot(), state, SyncPolicy())
    coordinator.request_full_sync = Mock()
    coordinator._worker_active = True
    coordinator._results.put(("activity", "wallet", baseline, None))

    coordinator._poll_result()

    coordinator.request_full_sync.assert_not_called()


def test_full_sync_result_replaces_receive_activity_baseline() -> None:
    root = FakeRoot()
    state = FakeState()
    state.apply_synchronized_snapshot = Mock(return_value=True)
    coordinator = WalletSyncCoordinator(root, state, SyncPolicy())
    refreshed = receive_activity(chain_transactions=2)
    snapshot = Mock(receive_activity=refreshed)
    coordinator._worker_active = True
    coordinator._results.put(("full", "wallet", snapshot, None))

    coordinator._poll_result()

    assert coordinator._receive_activity_baselines["wallet"] == refreshed


def test_probe_change_and_pending_request_are_coalesced_into_one_sync() -> None:
    state = FakeState()
    baseline = receive_activity(chain_transactions=1)
    state.receive_activity_baseline = baseline
    coordinator = WalletSyncCoordinator(FakeRoot(), state, SyncPolicy())
    coordinator.request_full_sync = Mock()
    coordinator._pending_full_sync = ("manual", "wallet")
    coordinator._worker_active = True
    coordinator._results.put(
        (
            "activity",
            "wallet",
            receive_activity(chain_transactions=2),
            None,
        )
    )

    coordinator._poll_result()

    coordinator.request_full_sync.assert_called_once_with(
        "manual",
        wallet_name="wallet",
    )
