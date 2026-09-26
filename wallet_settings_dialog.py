"""Wallet lifecycle controls backed exclusively by the Platform service."""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from app_assets import apply_window_icon
from btc.chainparams import NETWORK_TESTNET4
from dialog_utils import show_centered_modal
from state import WalletUIState
from theme import Theme
from wallet_core import AccountType


@dataclass(frozen=True, slots=True)
class AccountTypeOption:
    """One Wallet Settings account choice, including future placeholders."""

    value: str
    title: str
    detail: str
    enabled: bool


def account_type_options(network: str) -> tuple[AccountTypeOption, ...]:
    """Return truthful address examples for the running Bitcoin network."""

    native_prefix = "tb1q" if network == NETWORK_TESTNET4 else "bc1q"
    legacy_prefix = "m/n" if network == NETWORK_TESTNET4 else "1"
    return (
        AccountTypeOption(
            AccountType.NATIVE_SEGWIT.value,
            "Native SegWit",
            f"BIP84 · {native_prefix}",
            True,
        ),
        AccountTypeOption(
            AccountType.LEGACY.value,
            "Legacy",
            f"BIP44 · {legacy_prefix}",
            True,
        ),
        AccountTypeOption(
            "p2sh-p2wpkh",
            "Nested SegWit",
            "Coming later",
            False,
        ),
        AccountTypeOption("p2tr", "Taproot", "Coming later", False),
    )


class WalletSettingsDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, theme: Theme, state: WalletUIState):
        super().__init__(parent)
        self.parent = parent
        self.theme = theme
        self.state = state
        self._account_options = account_type_options(state.network)
        self._account_switching = False
        self._account_results: Queue = Queue(maxsize=1)

        self.withdraw()
        self.title("Wallet Settings")
        apply_window_icon(self)
        self.transient(parent)
        self.configure(bg=theme.BG)
        self.resizable(False, False)

        panel = tk.Frame(self, bg=theme.CARD, padx=24, pady=22)
        panel.pack(fill="both", expand=True, padx=14, pady=14)
        self.title_label = tk.Label(
            panel,
            bg=theme.CARD,
            fg=theme.TEXT,
            font=theme.font_wallet_title,
            anchor="w",
        )
        self.title_label.pack(fill="x", pady=(0, 4))
        self.metadata_label = tk.Label(
            panel,
            bg=theme.CARD,
            fg=theme.MUTED,
            font=theme.font_small,
            anchor="w",
        )
        self.metadata_label.pack(fill="x", pady=(0, 16))

        self._build_account_type_section(panel)

        actions = (
            ("View recovery words…", self._show_mnemonic),
            ("Rename wallet…", self._rename),
            ("Change password…", self._change_password),
            ("Remove wallet from this device…", self._remove),
        )
        self.action_buttons: list[tk.Button] = []
        for label, command in actions:
            button = tk.Button(
                panel,
                text=label,
                command=command,
                anchor="w",
                relief="flat",
                bg=theme.INPUT_BG,
                fg=theme.TEXT,
                activebackground=theme.TRACK_OFF,
                font=theme.font_body,
                padx=14,
                pady=10,
            )
            button.pack(fill="x", pady=3)
            self.action_buttons.append(button)

        self.close_button = tk.Button(
            panel,
            text="Close",
            command=self._close,
            relief="flat",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=theme.font_small,
            padx=12,
            pady=8,
        )
        self.close_button.pack(anchor="e", pady=(14, 0))

        self._refresh_header()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        show_centered_modal(
            self,
            parent,
            minimum_width=430,
            minimum_height=650,
            initial_focus=self.account_buttons[0],
        )

    def _build_account_type_section(self, panel: tk.Misc) -> None:
        section = tk.Frame(panel, bg=self.theme.CARD, bd=0)
        section.pack(fill="x", pady=(0, 16))
        tk.Label(
            section,
            text="Account Type",
            bg=self.theme.CARD,
            fg=self.theme.TEXT,
            font=self.theme.font_body_bold,
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        self.account_type_value = tk.StringVar(
            self,
            value=self.state.active_account_type.value,
        )
        self.account_buttons: list[tk.Radiobutton] = []
        for option in self._account_options:
            row = tk.Frame(section, bg=self.theme.CARD, bd=0)
            row.pack(fill="x", pady=2)
            row.grid_columnconfigure(1, weight=1)
            button = tk.Radiobutton(
                row,
                variable=self.account_type_value,
                value=option.value,
                command=self._account_type_changed,
                bg=self.theme.CARD,
                fg=self.theme.TEXT,
                activebackground=self.theme.CARD,
                activeforeground=self.theme.TEXT,
                disabledforeground=self.theme.MUTED_2,
                selectcolor=self.theme.CARD,
                cursor="hand2" if option.enabled else "arrow",
                state="normal" if option.enabled else "disabled",
                takefocus=option.enabled,
            )
            button.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 6))
            title = tk.Label(
                row,
                text=option.title,
                bg=self.theme.CARD,
                fg=self.theme.TEXT if option.enabled else self.theme.MUTED,
                font=self.theme.font_body,
                anchor="w",
                cursor="hand2" if option.enabled else "arrow",
            )
            title.grid(row=0, column=1, sticky="ew")
            detail = tk.Label(
                row,
                text=option.detail,
                bg=self.theme.CARD,
                fg=self.theme.MUTED,
                font=self.theme.font_small,
                anchor="w",
                cursor="hand2" if option.enabled else "arrow",
            )
            detail.grid(row=1, column=1, sticky="ew")
            if option.enabled:
                for widget in (row, title, detail):
                    widget.bind(
                        "<Button-1>",
                        lambda _event, radio=button: radio.invoke(),
                    )
            self.account_buttons.append(button)

        self.account_progress = ttk.Progressbar(
            section,
            mode="indeterminate",
            length=280,
        )
        self.account_status = tk.Label(
            section,
            text="",
            bg=self.theme.CARD,
            fg=self.theme.MUTED,
            font=self.theme.font_small,
            anchor="w",
        )
        self.account_status.pack(fill="x", pady=(6, 0))

    def _account_type_changed(self) -> None:
        try:
            requested = AccountType(self.account_type_value.get())
        except ValueError:
            self.account_type_value.set(self.state.active_account_type.value)
            return
        if requested is self.state.active_account_type:
            return
        self._start_account_switch(requested)

    def _start_account_switch(self, account_type: AccountType) -> None:
        """Discover a first-use account without blocking Tk's event loop."""

        if self._account_switching or not self.state.is_initialized:
            self.account_type_value.set(self.state.active_account_type.value)
            return
        self._account_switching = True
        self._update_control_states()
        label = "Legacy" if account_type is AccountType.LEGACY else "Native SegWit"
        self.account_status.configure(
            text=f"Preparing {label} account · discovering addresses if needed…"
        )
        self.account_progress.pack(
            fill="x",
            pady=(6, 0),
            before=self.account_status,
        )
        self.account_progress.start(12)

        def switch_account() -> None:
            try:
                activation = self.state.application.select_account_type(account_type)
                self._account_results.put((activation, None))
            except Exception as exc:
                self._account_results.put((None, str(exc) or "Account switch failed."))

        Thread(
            target=switch_account,
            name="wallet-account-discovery",
            daemon=True,
        ).start()
        self.after(100, self._poll_account_switch)

    def _poll_account_switch(self) -> None:
        try:
            activation, error_message = self._account_results.get_nowait()
        except Empty:
            if self.winfo_exists():
                self.after(100, self._poll_account_switch)
            return

        self.account_progress.stop()
        self.account_progress.pack_forget()
        self._account_switching = False
        if error_message is not None:
            self.account_type_value.set(self.state.active_account_type.value)
            self.account_status.configure(text="Account type was not changed.")
            self._update_control_states()
            messagebox.showerror("Account Type", error_message, parent=self)
            return

        self.state.apply_account_activation(activation)
        self.account_type_value.set(self.state.active_account_type.value)
        selected = (
            "Legacy"
            if self.state.active_account_type is AccountType.LEGACY
            else "Native SegWit"
        )
        self.account_status.configure(
            text=f"{selected} is active · wallet synchronization started."
        )
        self._update_control_states()

    def _refresh_header(self) -> None:
        name = self.state.active_wallet_name
        if name is None:
            self.title_label.configure(text="No active wallet")
            self.metadata_label.configure(
                text="Create or import a wallet to use these settings."
            )
            self._update_control_states()
            return
        security = (
            "Password protected"
            if self.state.active_wallet_encrypted
            else "Not encrypted"
        )
        self.title_label.configure(text=name)
        self.metadata_label.configure(text=security)
        self.account_type_value.set(self.state.active_account_type.value)
        self._update_control_states()

    def _update_control_states(self) -> None:
        enabled = self.state.is_initialized and not self._account_switching
        for option, button in zip(self._account_options, self.account_buttons):
            button.configure(
                state="normal" if enabled and option.enabled else "disabled",
                cursor="hand2" if enabled and option.enabled else "arrow",
            )
        for button in self.action_buttons:
            button.configure(state="normal" if enabled else "disabled")
        self.close_button.configure(
            state="normal" if not self._account_switching else "disabled"
        )

    def _close(self) -> None:
        if not self._account_switching:
            self.destroy()

    def _ask_current_password(self, title: str) -> tuple[bool, str | None]:
        if not self.state.active_wallet_encrypted:
            return True, None
        password = simpledialog.askstring(
            title,
            "Current wallet password:",
            show="*",
            parent=self,
        )
        return password is not None, password

    def _show_mnemonic(self) -> None:
        accepted, password = self._ask_current_password("View Recovery Words")
        if not accepted:
            return
        try:
            mnemonic = self.state.get_mnemonic(password)
        except ValueError as exc:
            messagebox.showerror("View Recovery Words", str(exc), parent=self)
            return
        messagebox.showwarning(
            "Secret Recovery Words",
            "Anyone with these words can spend this wallet. "
            "Keep them private and offline.\n\n"
            f"{mnemonic}",
            parent=self,
        )

    def _rename(self) -> None:
        name = simpledialog.askstring(
            "Rename Wallet",
            "New wallet name (letters, numbers, _ and -):",
            initialvalue=self.state.active_wallet_name,
            parent=self,
        )
        if name is None:
            return
        accepted, password = self._ask_current_password("Rename Wallet")
        if not accepted:
            return
        try:
            self.state.rename_wallet(name, password)
        except ValueError as exc:
            messagebox.showerror("Rename Wallet", str(exc), parent=self)
            return
        self._refresh_header()
        messagebox.showinfo("Rename Wallet", "Wallet renamed.", parent=self)

    def _change_password(self) -> None:
        accepted, current_password = self._ask_current_password("Change Password")
        if not accepted:
            return
        new_password = simpledialog.askstring(
            "Change Password", "New wallet password:", show="*", parent=self
        )
        if new_password is None:
            return
        confirmation = simpledialog.askstring(
            "Change Password", "Confirm new password:", show="*", parent=self
        )
        if confirmation is None:
            return
        if new_password != confirmation:
            messagebox.showerror(
                "Change Password", "The new passwords do not match.", parent=self
            )
            return
        try:
            self.state.change_password(current_password, new_password)
        except ValueError as exc:
            messagebox.showerror("Change Password", str(exc), parent=self)
            return
        self._refresh_header()
        messagebox.showinfo("Change Password", "Wallet password changed.", parent=self)

    def _remove(self) -> None:
        wallet_name = self.state.active_wallet_name
        confirmation = simpledialog.askstring(
            "Remove Wallet",
            f'Type "{wallet_name}" to remove this wallet from this device:',
            parent=self,
        )
        if confirmation is None:
            return
        if confirmation != wallet_name:
            messagebox.showerror(
                "Remove Wallet", "Wallet name did not match.", parent=self
            )
            return
        accepted, password = self._ask_current_password("Remove Wallet")
        if not accepted:
            return
        try:
            self.state.remove_wallet(password)
        except ValueError as exc:
            messagebox.showerror("Remove Wallet", str(exc), parent=self)
            return
        messagebox.showinfo(
            "Remove Wallet",
            "Wallet removed from this device. Your recovery words are the backup.",
            parent=self,
        )
        self.destroy()


def show_wallet_settings(
    parent: tk.Misc,
    theme: Theme,
    state: WalletUIState,
) -> WalletSettingsDialog:
    return WalletSettingsDialog(parent, theme, state)
