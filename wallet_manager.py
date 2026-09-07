"""Wallet-listing UI for selecting the active wallet."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from app_assets import apply_window_icon
from state import WalletUIState
from theme import Theme
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
        self.geometry("430x500")
        self.minsize(390, 360)

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

        has_wallets = bool(state.wallets)
        RoundedButton(
            panel,
            theme,
            text="OPEN WALLET",
            command=self._open_selected if has_wallets else None,
            fill=theme.PURPLE if has_wallets else theme.TRACK_OFF,
            height=50,
            radius=16,
        ).pack(fill="x", pady=(theme.px(14), 0))

        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Return>", lambda _event: self._open_selected())
        self.grab_set()
        self.after_idle(self.focus_set)

    def _render_wallets(self) -> None:
        for child in self.wallet_list.winfo_children():
            child.destroy()

        if not self.state.wallets:
            tk.Label(
                self.wallet_list,
                text="No wallets yet.\nUse Create New Wallet or Import Wallet from Home.",
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

    def _open_selected(self) -> None:
        if self.selected_name is not None:
            self._open_wallet(self.selected_name)

    def _open_wallet(self, wallet_name: str) -> None:
        try:
            self.state.select_wallet(wallet_name)
        except ValueError as exc:
            messagebox.showerror("Open Wallet", str(exc), parent=self)
            return
        self.destroy()


def show_wallet_manager(parent: tk.Misc, theme: Theme, state: WalletUIState) -> None:
    WalletManagerDialog(parent, theme, state)
