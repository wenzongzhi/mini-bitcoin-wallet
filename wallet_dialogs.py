"""Presentation-only dialogs for creating and importing encrypted wallets."""

from __future__ import annotations

from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from state import WalletUIState
from app_assets import apply_window_icon


def _import_error_message(error: Exception, mnemonic: str) -> str:
    """Return a display-safe error without retaining recovery words/traceback."""

    message = str(error) or "Wallet import failed."
    normalized_mnemonic = " ".join(mnemonic.split())
    for secret in {mnemonic, normalized_mnemonic}:
        if secret:
            message = message.replace(secret, "[recovery words redacted]")
    return message


def _ask_credentials(parent: tk.Misc, action: str) -> tuple[str, str] | None:
    name = simpledialog.askstring(
        action,
        "Wallet name (letters, numbers, _ and -):",
        parent=parent,
    )
    if name is None:
        return None
    password = simpledialog.askstring(
        action,
        "Wallet password:",
        show="*",
        parent=parent,
    )
    if password is None:
        return None
    confirmation = simpledialog.askstring(
        action,
        "Confirm wallet password:",
        show="*",
        parent=parent,
    )
    if confirmation is None:
        return None
    if password != confirmation:
        messagebox.showerror(action, "The passwords do not match.", parent=parent)
        return None
    return name, password


def create_new_wallet(parent: tk.Misc, state: WalletUIState) -> bool:
    credentials = _ask_credentials(parent, "Create Wallet")
    if credentials is None:
        return False
    name, password = credentials
    try:
        creation = state.create_wallet(name, password)
    except ValueError as exc:
        messagebox.showerror("Create Wallet", str(exc), parent=parent)
        return False
    generated_mnemonic = creation.generated_mnemonic
    if not generated_mnemonic:
        messagebox.showerror(
            "Create Wallet",
            "The wallet was created without recovery words to display.",
            parent=parent,
        )
        return False
    messagebox.showwarning(
        "Back Up Secret Words",
        "Write down these secret words in order and store them offline.\n\n"
        f"{generated_mnemonic}\n\n"
        "They will not be shown again automatically.",
        parent=parent,
    )
    return True


def import_wallet(parent: tk.Misc, state: WalletUIState) -> bool:
    mnemonic = simpledialog.askstring(
        "Import Wallet",
        "Enter your BIP39 secret words in order:",
        parent=parent,
    )
    if mnemonic is None:
        return False
    credentials = _ask_credentials(parent, "Import Wallet")
    if credentials is None:
        return False
    name, password = credentials
    _start_import_scan(parent, state, name, password, mnemonic)
    return True


def _start_import_scan(
    parent: tk.Misc,
    state: WalletUIState,
    name: str,
    password: str,
    mnemonic: str,
) -> None:
    """Import and scan without blocking Tkinter's event loop."""

    previous_grab = parent.grab_current()
    progress = tk.Toplevel(parent)
    progress.title("Import Wallet")
    apply_window_icon(progress)
    progress.transient(parent)
    progress.resizable(False, False)
    progress.protocol("WM_DELETE_WINDOW", lambda: None)
    tk.Label(
        progress,
        text="Discovering receive and change address history…\n"
        "Each branch stops after 20 consecutive unused addresses.",
        padx=24,
        pady=18,
        justify="center",
    ).pack()
    indicator = ttk.Progressbar(progress, mode="indeterminate", length=280)
    indicator.pack(padx=24, pady=(0, 20))
    indicator.start(12)
    progress.grab_set()
    progress.update_idletasks()

    results = Queue(maxsize=1)

    def run_import():
        try:
            result = state.application.import_wallet(name, password, mnemonic)
            # Pass only non-secret presentation data across the worker queue.
            # In particular, the user-provided recovery words never make the
            # return trip back to Tk's UI thread.
            results.put((result.snapshot, result.discovery, None))
        except Exception as exc:
            # Exception tracebacks retain worker-frame locals.  Queue only a
            # redacted string so the mnemonic does not remain reachable from
            # Tk's UI thread through ``exc.__traceback__``.
            results.put((None, None, _import_error_message(exc, mnemonic)))

    def poll_result():
        try:
            snapshot, discovery, error_message = results.get_nowait()
        except Empty:
            if progress.winfo_exists():
                parent.after(100, poll_result)
            return

        indicator.stop()
        progress.grab_release()
        progress.destroy()
        if previous_grab is not None:
            try:
                if previous_grab.winfo_exists():
                    previous_grab.grab_set()
            except tk.TclError:
                pass
        if error_message is not None:
            messagebox.showerror("Import Wallet", error_message, parent=parent)
            return

        state.apply_wallet_import(snapshot)
        discovery_text = ""
        if discovery is not None:
            discovery_text = (
                "\n\n"
                f"Receive: scanned {discovery.receive_scanned}, "
                f"found {discovery.receive_used} used.\n"
                f"Change: scanned {discovery.change_scanned}, "
                f"found {discovery.change_used} used."
            )
        messagebox.showinfo(
            "Import Wallet",
            f'Wallet "{state.wallet_name.get()}" was imported and synchronized.'
            f"{discovery_text}",
            parent=parent,
        )

    Thread(target=run_import, name="wallet-import-scan", daemon=True).start()
    parent.after(100, poll_result)
