"""Non-blocking review, confirmation, and broadcast presentation flow."""

from __future__ import annotations

from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Callable

from app_assets import apply_window_icon
from state import WalletUIState
from theme import Theme
from wallet_core import BroadcastResult, WithdrawalReview


def _run_with_progress(
    parent: tk.Misc,
    *,
    title: str,
    message: str,
    operation: Callable[[], object],
    on_success: Callable[[object], None],
    on_error: Callable[[Exception], None],
) -> None:
    """Run wallet/network work without touching Tkinter from a worker thread."""

    progress = tk.Toplevel(parent)
    progress.title(title)
    apply_window_icon(progress)
    progress.transient(parent)
    progress.resizable(False, False)
    progress.protocol("WM_DELETE_WINDOW", lambda: None)
    tk.Label(
        progress,
        text=message,
        padx=24,
        pady=18,
        justify="center",
    ).pack()
    indicator = ttk.Progressbar(progress, mode="indeterminate", length=280)
    indicator.pack(padx=24, pady=(0, 20))
    indicator.start(12)
    progress.grab_set()

    results = Queue(maxsize=1)

    def run_operation() -> None:
        try:
            results.put((operation(), None))
        except Exception as exc:
            results.put((None, exc))

    def poll_result() -> None:
        try:
            result, error = results.get_nowait()
        except Empty:
            if progress.winfo_exists():
                parent.after(100, poll_result)
            return

        indicator.stop()
        progress.grab_release()
        progress.destroy()
        if error is not None:
            on_error(error)
        else:
            on_success(result)

    Thread(target=run_operation, name=f"{title.lower()}-worker", daemon=True).start()
    parent.after(100, poll_result)


def review_withdrawal(
    parent: tk.Misc,
    theme: Theme,
    state: WalletUIState,
    fee_rate_sat_vb: int,
) -> None:
    """Prepare an exact signed review and broadcast only after confirmation."""

    if fee_rate_sat_vb == 0:
        messagebox.showerror(
            "Cannot Review Withdrawal",
            "0 sat/vB is available as a slider endpoint, but bitcoin-tool and "
            "standard Bitcoin relays require at least 1 sat/vB.",
            parent=parent,
        )
        return
    password = None
    if state.active_wallet_encrypted:
        password = simpledialog.askstring(
            "Unlock Wallet",
            f'Enter the password for "{state.active_wallet_name}":',
            show="*",
            parent=parent,
        )
        if password is None:
            return

    # Tk variables are read only on the UI thread; the worker receives values.
    destination = state.address.get()
    amount_text = state.amount.get()
    unit = state.unit
    send_all = state.send_all.get()

    def prepare() -> WithdrawalReview:
        return state.application.prepare_withdrawal(
            destination,
            amount_text,
            unit,
            fee_rate_sat_vb,
            password,
            send_all=send_all,
        )

    _run_with_progress(
        parent,
        title="Prepare Withdrawal",
        message="Synchronizing wallet and signing transaction…",
        operation=prepare,
        on_success=lambda result: _confirm_withdrawal(parent, theme, state, result),
        on_error=lambda error: messagebox.showerror(
            "Cannot Review Withdrawal", str(error), parent=parent
        ),
    )


def _confirm_withdrawal(
    parent: tk.Misc,
    theme: Theme,
    state: WalletUIState,
    review: WithdrawalReview,
) -> None:
    action = "Send all" if review.send_all else "Send"
    confirmed = messagebox.askokcancel(
        "Confirm Withdrawal",
        "\n".join(
            (
                f"Wallet: {review.wallet_name}",
                f"Network: {review.network}",
                f"{action}: {review.amount.format(state.unit)} {state.unit.value}",
                f"Fee: {review.fee.format(state.unit)} {state.unit.value}",
                f"Total debit: {review.total.format(state.unit)} {state.unit.value}",
                f"Fee rate: {review.fee_rate_sat_vb} sat/vB",
                f"To: {review.destination}",
                f"TXID: {review.txid}",
                "",
                "Confirm to broadcast this signed transaction.",
            )
        ),
        icon="warning",
        parent=parent,
    )
    if not confirmed:
        state.cancel_withdrawal(review.review_id)
        return

    _broadcast_withdrawal(parent, theme, state, review)


def _broadcast_withdrawal(
    parent: tk.Misc,
    theme: Theme,
    state: WalletUIState,
    review: WithdrawalReview,
) -> None:
    def broadcast() -> BroadcastResult:
        return state.application.broadcast_withdrawal(review.review_id)

    def completed(result: BroadcastResult) -> None:
        state.apply_broadcast_success()
        warning = f"\n\nWarning: {result.cache_warning}" if result.cache_warning else ""
        messagebox.showinfo(
            "Withdrawal Broadcast",
            f"Transaction accepted.\n\nTXID: {result.txid}\n{result.explorer_url}{warning}",
            parent=parent,
        )

    def failed(error: Exception) -> None:
        retry = messagebox.askretrycancel(
            "Broadcast Failed",
            f"{error}\n\nThe signed transaction remains reserved. Retry broadcast?",
            parent=parent,
        )
        if retry:
            _broadcast_withdrawal(parent, theme, state, review)

    _run_with_progress(
        parent,
        title="Broadcast Withdrawal",
        message="Broadcasting signed transaction…",
        operation=broadcast,
        on_success=completed,
        on_error=failed,
    )
