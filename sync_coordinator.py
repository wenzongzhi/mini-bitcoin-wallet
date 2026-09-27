"""Background synchronization policy for the desktop wallet.

Full wallet scans are deliberately event-driven because public Esplora servers
are a shared resource. Lightweight receive-activity and transaction-status
probes are scheduled independently and share one serialized worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from queue import Empty, Queue
from threading import Thread
import time
import tkinter as tk

from state import WalletUIState
from wallet_core import WalletReceiveActivity


@dataclass(frozen=True, slots=True)
class SyncPolicy:
    """Tunable public-backend request policy, expressed in seconds."""

    status_fast_interval: int = 30
    status_fast_window: int = 10 * 60
    status_slow_interval: int = 120
    receive_probe_interval: int = 60
    maximum_backoff: int = 15 * 60

    def status_interval(self, monitored_for: float) -> int:
        return (
            self.status_fast_interval
            if monitored_for < self.status_fast_window
            else self.status_slow_interval
        )

    def retry_delay(self, consecutive_failures: int) -> int:
        if consecutive_failures <= 0:
            return 0
        return min(30 * (2 ** (consecutive_failures - 1)), self.maximum_backoff)


class WalletSyncCoordinator:
    """Serialize wallet work and run independent low-cost activity probes."""

    def __init__(
        self,
        root: tk.Misc,
        state: WalletUIState,
        policy: SyncPolicy | None = None,
    ) -> None:
        self.root = root
        self.state = state
        self.policy = policy or SyncPolicy()
        self._results: Queue = Queue(maxsize=1)
        self._worker_active = False
        self._pending_full_sync: tuple[str, str | None] | None = None
        self._closed = False
        self._poll_job = None
        self._status_job = None
        self._activity_job = None
        self._failures = 0
        self._next_automatic_attempt = 0.0
        self._first_monitored: dict[tuple[str, str], float] = {}
        self._last_status_check: dict[tuple[str, str], float] = {}
        self._confirmed_transactions: set[tuple[str, str]] = set()
        self._receive_activity_baselines: dict[str, WalletReceiveActivity] = {}
        initial_activity = getattr(state, "receive_activity_baseline", None)
        if state.active_wallet_name is not None and initial_activity is not None:
            self._receive_activity_baselines[state.active_wallet_name] = (
                initial_activity
            )

        root.bind("<<ActiveWalletChanged>>", self._wallet_changed, add="+")
        root.bind("<<WalletRefreshRequested>>", self._manual_refresh, add="+")
        root.bind("<<WalletBroadcast>>", self._broadcast_completed, add="+")

    def start(self) -> None:
        """Start cached-first synchronization after the first UI render."""

        if self._closed:
            return
        self.root.after(250, lambda: self.request_full_sync("startup"))
        self._schedule_status_check()
        self._schedule_activity_check()

    def close(self) -> None:
        """Stop scheduling callbacks; running daemon work may finish naturally."""

        self._closed = True
        for job in (self._poll_job, self._status_job, self._activity_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass

    def request_full_sync(
        self,
        reason: str,
        *,
        wallet_name: str | None = None,
    ) -> None:
        """Queue one full scan for an explicit wallet lifecycle event."""

        wallet_name = wallet_name or self.state.active_wallet_name
        if self._closed or wallet_name is None:
            return
        if self._worker_active:
            self._pending_full_sync = (reason, wallet_name)
            return

        if wallet_name == self.state.active_wallet_name:
            self.state.set_sync_activity(True, "Synchronizing wallet in background…")
        self._start_worker(
            "full",
            wallet_name,
            lambda: self.state.application.synchronize_wallet(wallet_name),
        )

    def _wallet_changed(self, _event=None) -> None:
        self._discard_other_wallet_monitoring()
        wallet_name = self.state.active_wallet_name
        activity = getattr(self.state, "receive_activity_baseline", None)
        if wallet_name is not None and activity is not None:
            self._receive_activity_baselines[wallet_name] = activity
        self.request_full_sync("wallet-change")

    def _manual_refresh(self, _event=None) -> None:
        self.request_full_sync("manual")

    def _broadcast_completed(self, _event=None) -> None:
        self.state.set_sync_activity(
            False,
            "Broadcast accepted · monitoring confirmation",
        )

    def _start_worker(self, kind: str, wallet_name: str, operation) -> None:
        self._worker_active = True

        def run() -> None:
            try:
                self._results.put((kind, wallet_name, operation(), None))
            except Exception as exc:
                self._results.put((kind, wallet_name, None, exc))

        Thread(
            target=run,
            name=f"wallet-{kind}-sync",
            daemon=True,
        ).start()
        self._poll_job = self.root.after(100, self._poll_result)

    def _poll_result(self) -> None:
        if self._closed:
            return
        try:
            kind, wallet_name, result, error = self._results.get_nowait()
        except Empty:
            self._poll_job = self.root.after(100, self._poll_result)
            return

        self._poll_job = None
        self._worker_active = False
        detected_sync: tuple[str, str] | None = None
        if error is not None:
            self._record_failure(kind, error, wallet_name)
        elif kind == "full":
            self._record_success()
            activity = getattr(result, "receive_activity", None)
            if activity is not None:
                self._receive_activity_baselines[wallet_name] = activity
            if self.state.apply_synchronized_snapshot(result):
                updated = datetime.now().strftime("%H:%M:%S")
                self.state.set_sync_activity(False, f"Updated {updated}")
            else:
                self.state.set_sync_activity(False, "")
        elif kind == "status":
            self._record_success()
            confirmed_txids = {
                status.txid for status in result if status.confirmed
            }
            self._confirmed_transactions.update(
                (wallet_name, txid) for txid in confirmed_txids
            )
            if wallet_name == self.state.active_wallet_name and confirmed_txids:
                detected_sync = ("transaction-confirmed", wallet_name)
            elif wallet_name == self.state.active_wallet_name:
                self.state.set_sync_activity(
                    False,
                    "Waiting for transaction confirmation",
                )
        elif kind == "activity":
            self._record_success()
            if wallet_name == self.state.active_wallet_name:
                baseline = self._receive_activity_baselines.get(wallet_name)
                if baseline is None:
                    if result.has_history:
                        detected_sync = ("incoming-activity", wallet_name)
                    else:
                        self._receive_activity_baselines[wallet_name] = result
                elif result != baseline:
                    detected_sync = ("incoming-activity", wallet_name)

        pending = self._pending_full_sync
        self._pending_full_sync = None
        requested_sync = pending or detected_sync
        if requested_sync is not None:
            reason, requested_wallet = requested_sync
            self.request_full_sync(reason, wallet_name=requested_wallet)

    def _record_success(self) -> None:
        self._failures = 0
        self._next_automatic_attempt = 0.0

    def _record_failure(
        self,
        kind: str,
        _error: Exception,
        wallet_name: str,
    ) -> None:
        self._failures += 1
        delay = self.policy.retry_delay(self._failures)
        self._next_automatic_attempt = time.monotonic() + delay
        if wallet_name == self.state.active_wallet_name:
            if kind == "status":
                message = f"Confirmation check unavailable · retrying in {delay}s"
            elif kind == "full":
                message = "Sync unavailable · use Refresh to retry"
            else:
                # Incoming probes are passive. Retry quietly rather than
                # replacing a useful sync or confirmation status in the UI.
                return
            self.state.set_sync_activity(False, message)

    def _schedule_status_check(self) -> None:
        if self._closed:
            return
        self._status_job = self.root.after(
            self.policy.status_fast_interval * 1000,
            self._status_check,
        )

    def _status_check(self) -> None:
        self._status_job = None
        now = time.monotonic()
        wallet_name = self.state.active_wallet_name
        txids = self.state.monitored_transaction_ids
        if (
            wallet_name is not None
            and txids
            and not self._worker_active
            and now >= self._next_automatic_attempt
        ):
            due = []
            for txid in txids:
                key = (wallet_name, txid)
                if key in self._confirmed_transactions:
                    continue
                first = self._first_monitored.setdefault(key, now)
                interval = self.policy.status_interval(now - first)
                if now - self._last_status_check.get(key, 0.0) >= interval:
                    due.append(txid)
                    self._last_status_check[key] = now
            if due:
                self._start_worker(
                    "status",
                    wallet_name,
                    lambda: tuple(
                        self.state.application.transaction_status(txid)
                        for txid in due
                    ),
                )
        self._discard_other_wallet_monitoring()
        self._schedule_status_check()

    def _schedule_activity_check(self) -> None:
        if self._closed:
            return
        self._activity_job = self.root.after(
            self.policy.receive_probe_interval * 1000,
            self._activity_check,
        )

    def _activity_check(self) -> None:
        """Run one wallet-wide receive-address statistics probe when idle."""

        self._activity_job = None
        wallet_name = self.state.active_wallet_name
        if (
            wallet_name is not None
            and not self._worker_active
            and time.monotonic() >= self._next_automatic_attempt
        ):
            self._start_worker(
                "activity",
                wallet_name,
                self.state.application.receive_activity,
            )
        self._schedule_activity_check()

    def _discard_other_wallet_monitoring(self) -> None:
        wallet_name = self.state.active_wallet_name
        active_keys = {
            (wallet_name, txid)
            for txid in self.state.monitored_transaction_ids
        }
        self._first_monitored = {
            key: value
            for key, value in self._first_monitored.items()
            if key in active_keys
        }
        self._last_status_check = {
            key: value
            for key, value in self._last_status_check.items()
            if key in active_keys
        }
        self._receive_activity_baselines = {
            name: activity
            for name, activity in self._receive_activity_baselines.items()
            if name == wallet_name
        }
