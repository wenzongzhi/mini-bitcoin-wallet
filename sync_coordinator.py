"""Background synchronization policy for the desktop wallet.

This module owns timing, deduplication, and retry decisions.  It depends only on
wallet-core use cases and presentation state; bitcoin-tool remains an unchanged
infrastructure dependency behind the application boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Empty, Queue
from threading import Thread
import time
import tkinter as tk

from state import WalletUIState


@dataclass(frozen=True, slots=True)
class SyncPolicy:
    """Tunable public-backend request policy, expressed in seconds."""

    switch_stale_after: int = 120
    foreground_sync_after: int = 300
    foreground_check_every: int = 60
    post_broadcast_delay: int = 5
    status_fast_interval: int = 30
    status_fast_window: int = 10 * 60
    status_slow_interval: int = 120
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
    """Serialize wallet scans and cheaply monitor unconfirmed transactions."""

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
        self._pending_full_sync: tuple[str, bool, str | None] | None = None
        self._closed = False
        self._poll_job = None
        self._foreground_job = None
        self._status_job = None
        self._failures = 0
        self._next_automatic_attempt = 0.0
        self._first_monitored: dict[tuple[str, str], float] = {}
        self._last_status_check: dict[tuple[str, str], float] = {}

        root.bind("<<ActiveWalletChanged>>", self._wallet_changed, add="+")
        root.bind("<<WalletRefreshRequested>>", self._manual_refresh, add="+")
        root.bind("<<WalletBroadcast>>", self._broadcast_completed, add="+")

    def start(self) -> None:
        """Start cached-first synchronization after the first UI render."""

        if self._closed:
            return
        self.root.after(250, lambda: self.request_full_sync("startup", force=True))
        self._schedule_foreground_check()
        self._schedule_status_check()

    def close(self) -> None:
        """Stop scheduling callbacks; running daemon work may finish naturally."""

        self._closed = True
        for job in (self._poll_job, self._foreground_job, self._status_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass

    def request_full_sync(
        self,
        reason: str,
        *,
        force: bool = False,
        wallet_name: str | None = None,
    ) -> None:
        wallet_name = wallet_name or self.state.active_wallet_name
        if self._closed or wallet_name is None:
            return
        if self._worker_active:
            self._pending_full_sync = (reason, force, wallet_name)
            return
        if (
            not force
            and wallet_name == self.state.active_wallet_name
            and not self._wallet_cache_is_stale(reason)
        ):
            return
        if (
            not force
            and reason != "manual"
            and time.monotonic() < self._next_automatic_attempt
        ):
            return

        if wallet_name == self.state.active_wallet_name:
            self.state.set_sync_activity(True, "Synchronizing wallet in background…")
        self._start_worker(
            "full",
            wallet_name,
            lambda: self.state.application.synchronize_wallet(wallet_name),
        )

    def _wallet_cache_is_stale(self, reason: str) -> bool:
        synced_at = self.state.synced_at
        if synced_at is None:
            return True
        maximum_age = (
            self.policy.switch_stale_after
            if reason == "wallet-change"
            else self.policy.foreground_sync_after
        )
        age = (datetime.now(timezone.utc) - synced_at).total_seconds()
        return age >= maximum_age

    def _wallet_changed(self, _event=None) -> None:
        self._discard_other_wallet_monitoring()
        self.request_full_sync("wallet-change")

    def _manual_refresh(self, _event=None) -> None:
        self.request_full_sync("manual", force=True)

    def _broadcast_completed(self, _event=None) -> None:
        wallet_name = self.state.active_wallet_name
        self.state.set_sync_activity(False, "Broadcast accepted · refresh queued")
        self.root.after(
            self.policy.post_broadcast_delay * 1000,
            lambda: self.request_full_sync(
                "post-broadcast", force=True, wallet_name=wallet_name
            ),
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
        if error is not None:
            self._record_failure(error, wallet_name)
        elif kind == "full":
            self._record_success()
            if self.state.apply_synchronized_snapshot(result):
                updated = datetime.now().strftime("%H:%M:%S")
                self.state.set_sync_activity(False, f"Updated {updated}")
            else:
                self.state.set_sync_activity(False, "")
        elif kind == "status":
            self._record_success()
            if wallet_name == self.state.active_wallet_name and any(
                status.confirmed for status in result
            ):
                self.request_full_sync("transaction-confirmed", force=True)

        pending = self._pending_full_sync
        self._pending_full_sync = None
        if pending is not None:
            reason, force, pending_wallet = pending
            self.root.after(
                0,
                lambda: self.request_full_sync(
                    reason, force=force, wallet_name=pending_wallet
                ),
            )

    def _record_success(self) -> None:
        self._failures = 0
        self._next_automatic_attempt = 0.0

    def _record_failure(self, _error: Exception, wallet_name: str) -> None:
        self._failures += 1
        delay = self.policy.retry_delay(self._failures)
        self._next_automatic_attempt = time.monotonic() + delay
        if wallet_name == self.state.active_wallet_name:
            self.state.set_sync_activity(
                False,
                f"Sync unavailable · retrying in {delay}s",
            )

    def _schedule_foreground_check(self) -> None:
        if self._closed:
            return
        self._foreground_job = self.root.after(
            self.policy.foreground_check_every * 1000,
            self._foreground_check,
        )

    def _foreground_check(self) -> None:
        self._foreground_job = None
        if self._window_is_active():
            self.request_full_sync("foreground")
        self._schedule_foreground_check()

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
            and self._window_is_active()
            and now >= self._next_automatic_attempt
        ):
            due = []
            for txid in txids:
                key = (wallet_name, txid)
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

    def _window_is_active(self) -> bool:
        try:
            return self.root.winfo_exists() and self.root.state() == "normal"
        except tk.TclError:
            return False
