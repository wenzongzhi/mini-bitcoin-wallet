"""Presentation-only dialogs for creating and importing encrypted wallets."""

from __future__ import annotations

from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from state import WalletUIState
from app_assets import apply_window_icon


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
    messagebox.showwarning(
        "Back Up Secret Words",
        "Write down these secret words in order and store them offline.\n\n"
        f"{creation.mnemonic}\n\n"
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

    progress = tk.Toplevel(parent)
    progress.title("Import Wallet")
    apply_window_icon(progress)
    progress.transient(parent)
    progress.resizable(False, False)
    progress.protocol("WM_DELETE_WINDOW", lambda: None)
    tk.Label(
        progress,
        text="Scanning 20 receive and 20 change addresses…\n"
        "Balance and transaction history will update when complete.",
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
            creation = state.application.create_wallet(name, password, mnemonic)
            results.put((creation, None))
        except Exception as exc:  # passed to the UI thread for presentation
            results.put((None, exc))

    def poll_result():
        try:
            creation, error = results.get_nowait()
        except Empty:
            if progress.winfo_exists():
                parent.after(100, poll_result)
            return

        indicator.stop()
        progress.grab_release()
        progress.destroy()
        if error is not None:
            # bitcoin-tool may already have safely stored the encrypted wallet
            # before a network scan fails. Reflect that state instead of leaving
            # the UI looking uninitialized.
            try:
                state.reload_wallet()
            except ValueError:
                pass
            messagebox.showerror("Import Wallet", str(error), parent=parent)
            return

        state.apply_wallet_creation(creation)
        messagebox.showinfo(
            "Import Wallet",
            f'Wallet "{state.wallet_name.get()}" was imported and synchronized.',
            parent=parent,
        )

    Thread(target=run_import, name="wallet-import-scan", daemon=True).start()
    parent.after(100, poll_result)
