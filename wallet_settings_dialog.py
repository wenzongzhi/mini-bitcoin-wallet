"""Wallet lifecycle controls backed exclusively by the Platform service."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog

from app_assets import apply_window_icon
from dialog_utils import show_centered_modal
from state import WalletUIState
from theme import Theme


class WalletSettingsDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, theme: Theme, state: WalletUIState):
        super().__init__(parent)
        self.parent = parent
        self.theme = theme
        self.state = state

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

        tk.Button(
            panel,
            text="Close",
            command=self.destroy,
            relief="flat",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=theme.font_small,
            padx=12,
            pady=8,
        ).pack(anchor="e", pady=(14, 0))

        self._refresh_header()
        self.bind("<Escape>", lambda _event: self.destroy())
        show_centered_modal(
            self,
            parent,
            minimum_width=430,
            minimum_height=430,
            initial_focus=self.action_buttons[0],
        )

    def _refresh_header(self) -> None:
        name = self.state.active_wallet_name
        if name is None:
            self.title_label.configure(text="No active wallet")
            self.metadata_label.configure(
                text="Create or import a wallet to use these settings."
            )
            for button in self.action_buttons:
                button.configure(state="disabled")
            return
        security = "Password protected" if self.state.active_wallet_encrypted else "Not encrypted"
        self.title_label.configure(text=name)
        self.metadata_label.configure(text=security)

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
