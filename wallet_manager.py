"""Wallet-listing UI for selecting the active wallet."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from app_assets import apply_window_icon
from state import WalletUIState
from theme import Theme
from wallet_dialogs import create_new_wallet, import_wallet
from wallet_settings_dialog import show_wallet_settings
from widgets import RoundedButton, ScrollableFrame


def _network_label(network: str) -> str:
    return "Testnet4" if network == "testnet4" else "Mainnet"


class WalletManagerDialog(tk.Toplevel):
    """List public wallet metadata and open one wallet without a password."""

    def __init__(self, parent: tk.Misc, theme: Theme, state: WalletUIState):
        super().__init__(parent)
        self.theme = theme
        self.state = state
        self.selected_name = state.active_wallet_name

        self.title("Wallets")
        apply_window_icon(self)
        self.transient(parent)
        self.configure(bg=theme.BG)
        self.resizable(False, True)
        self.geometry("460x590")
        self.minsize(410, 440)

        panel = tk.Frame(self, bg=theme.CARD, padx=theme.px(22), pady=theme.px(20))
        panel.pack(fill="both", expand=True, padx=theme.px(14), pady=theme.px(14))
        tk.Label(
            panel,
            text="Wallets",
            bg=theme.CARD,
            fg=theme.TEXT,
            font=theme.font_wallet_title,
            anchor="w",
        ).pack(fill="x", pady=(0, theme.px(14)))

        self.wallet_scroller = ScrollableFrame(panel, theme)
        self.wallet_scroller.pack(fill="both", expand=True)
        self.wallet_list = self.wallet_scroller.content
        self._render_wallets()

        action_row = tk.Frame(panel, bg=theme.CARD, bd=0)
        action_row.pack(fill="x", pady=(theme.px(12), 0))
        for column in range(3):
            action_row.grid_columnconfigure(column, weight=1)
        RoundedButton(
            action_row,
            theme,
            text="CREATE",
            command=self._create_wallet,
            fill=theme.PURPLE_SOFT,
            height=42,
            radius=14,
        ).grid(row=0, column=0, sticky="ew", padx=(0, theme.px(4)))
        RoundedButton(
            action_row,
            theme,
            text="IMPORT",
            command=self._import_wallet,
            fill=theme.PURPLE_SOFT,
            height=42,
            radius=14,
        ).grid(row=0, column=1, sticky="ew", padx=theme.px(4))
        self.wallet_settings_button = RoundedButton(
            action_row,
            theme,
            text="SETTINGS",
            command=self._open_wallet_settings,
            fill=theme.PURPLE_SOFT,
            height=42,
            radius=14,
        )
        self.wallet_settings_button.grid(
            row=0,
            column=2,
            sticky="ew",
            padx=(theme.px(4), 0),
        )

        self.open_button = RoundedButton(
            panel,
            theme,
            text="OPEN WALLET",
            command=self._open_selected,
            height=50,
            radius=16,
        )
        self.open_button.pack(fill="x", pady=(theme.px(10), 0))

        self._revision_trace = state.revision.trace_add(
            "write", lambda *_: self._wallet_state_changed()
        )
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._update_actions()

        self.bind("<Escape>", lambda _event: self._close())
        self.bind("<Return>", lambda _event: self._open_selected())
        self.grab_set()
        self.after_idle(self.focus_set)

    def _render_wallets(self) -> None:
        for child in self.wallet_list.winfo_children():
            child.destroy()

        if not self.state.wallets:
            tk.Label(
                self.wallet_list,
                text="No wallets yet.\nUse Create or Import below to add one.",
                bg=self.theme.CARD,
                fg=self.theme.MUTED,
                font=self.theme.font_body,
                justify="center",
            ).pack(expand=True)
            return

        if self.selected_name not in {wallet.name for wallet in self.state.wallets}:
            self.selected_name = self.state.wallets[0].name

        for wallet in self.state.wallets:
            selected = wallet.name == self.selected_name
            row = tk.Frame(
                self.wallet_list,
                bg=self.theme.INPUT_BG if selected else self.theme.CARD,
                padx=self.theme.px(12),
                pady=self.theme.px(10),
                cursor="hand2",
            )
            row.pack(fill="x", pady=self.theme.px(3))
            row.grid_columnconfigure(1, weight=1)

            marker = "●" if wallet.name == self.state.active_wallet_name else "○"
            tk.Label(
                row,
                text=marker,
                bg=row.cget("bg"),
                fg=self.theme.PURPLE if marker == "●" else self.theme.MUTED_2,
                font=self.theme.font_body,
            ).grid(row=0, column=0, rowspan=2, padx=(0, self.theme.px(10)))
            tk.Label(
                row,
                text=wallet.name,
                bg=row.cget("bg"),
                fg=self.theme.TEXT,
                font=self.theme.font_body_bold,
                anchor="w",
            ).grid(row=0, column=1, sticky="ew")
            security = "Encrypted" if wallet.encrypted else "Not encrypted"
            tk.Label(
                row,
                text=f"{_network_label(wallet.network)} · {security}",
                bg=row.cget("bg"),
                fg=self.theme.MUTED,
                font=self.theme.font_small,
                anchor="w",
            ).grid(row=1, column=1, sticky="ew", pady=(self.theme.px(2), 0))
            if wallet.name == self.state.active_wallet_name:
                tk.Label(
                    row,
                    text="Active",
                    bg=row.cget("bg"),
                    fg=self.theme.PURPLE,
                    font=self.theme.font_small_bold,
                ).grid(row=0, column=2, rowspan=2, padx=(self.theme.px(8), 0))

            self._bind_row(row, wallet.name)
            self.wallet_scroller.bind_mousewheel_tree(row)

    def _bind_row(self, widget: tk.Misc, wallet_name: str) -> None:
        widget.bind(
            "<Button-1>",
            lambda _event, name=wallet_name: self._select_row(name),
            add="+",
        )
        widget.bind(
            "<Double-Button-1>",
            lambda _event, name=wallet_name: self._open_wallet(name),
            add="+",
        )
        for child in widget.winfo_children():
            self._bind_row(child, wallet_name)

    def _select_row(self, wallet_name: str) -> None:
        self.selected_name = wallet_name
        self._render_wallets()
        self._update_actions()

    def _open_selected(self) -> None:
        if self.selected_name is not None:
            self._open_wallet(self.selected_name)

    def _open_wallet(self, wallet_name: str) -> None:
        try:
            self.state.select_wallet(wallet_name)
        except ValueError as exc:
            messagebox.showerror("Open Wallet", str(exc), parent=self)
            return
        self._close()

    def _create_wallet(self) -> None:
        if create_new_wallet(self, self.state):
            self.selected_name = self.state.active_wallet_name
            self._wallet_state_changed()

    def _import_wallet(self) -> None:
        import_wallet(self, self.state)

    def _open_wallet_settings(self) -> None:
        if self.selected_name is None:
            return
        if self.selected_name != self.state.active_wallet_name:
            try:
                self.state.select_wallet(self.selected_name)
            except ValueError as exc:
                messagebox.showerror("Open Wallet", str(exc), parent=self)
                return
        dialog = show_wallet_settings(self, self.theme, self.state)
        self.wait_window(dialog)
        if self.winfo_exists():
            self._wallet_state_changed()
            self.grab_set()

    def _wallet_state_changed(self) -> None:
        if not self.winfo_exists():
            return
        if self.state.active_wallet_name is not None:
            self.selected_name = self.state.active_wallet_name
        self._render_wallets()
        self._update_actions()

    def _update_actions(self) -> None:
        has_selection = self.selected_name is not None and bool(self.state.wallets)
        fill = self.theme.PURPLE if has_selection else self.theme.TRACK_OFF
        for button in (self.open_button, self.wallet_settings_button):
            button.set_fill(fill)
            button.configure(cursor="hand2" if has_selection else "arrow")

    def _close(self) -> None:
        try:
            self.state.revision.trace_remove("write", self._revision_trace)
        except tk.TclError:
            pass
        self.destroy()


def show_wallet_manager(parent: tk.Misc, theme: Theme, state: WalletUIState) -> None:
    WalletManagerDialog(parent, theme, state)
