"""Transaction time formatting and accessible detail presentation."""

from __future__ import annotations

import re
import tkinter as tk
import webbrowser
from tkinter import messagebox
from urllib.parse import urlparse

from theme import Theme
from wallet_core import DisplayUnit, TransactionDirection, TransactionSummary
from app_assets import apply_window_icon
from dialog_utils import show_centered_modal


_DIRECTION_LABELS = {
    TransactionDirection.INCOMING: "Received",
    TransactionDirection.OUTGOING: "Sent",
    TransactionDirection.SELF: "Internal transfer",
}


def format_transaction_time(transaction: TransactionSummary, *, detailed=False) -> str:
    if transaction.block_time is None:
        return "Unconfirmed"
    local_time = transaction.block_time.astimezone()
    pattern = "%Y-%m-%d %H:%M:%S %Z" if detailed else "%Y-%m-%d %H:%M"
    return local_time.strftime(pattern).strip()


class TransactionDetailsDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, theme: Theme, transaction: TransactionSummary):
        super().__init__(parent)
        # A new Toplevel is otherwise mapped at its default 1x1 geometry before
        # Tk has measured the real content, which produces a visible flash.
        self.withdraw()
        self.transaction = transaction
        self.theme = theme
        self.title("Transaction Details")
        apply_window_icon(self)
        self.configure(bg=theme.CARD)
        self.transient(parent)
        self.resizable(True, True)
        self.minsize(500, 520)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())

        content = tk.Frame(self, bg=theme.CARD, padx=24, pady=20)
        content.pack(fill="both", expand=True)
        content.grid_columnconfigure(1, weight=1)

        rows = (
            ("Type", _DIRECTION_LABELS[transaction.direction]),
            ("Net amount", f"{transaction.amount.format(DisplayUnit.BTC, signed=True)} BTC"),
            ("Received", f"{transaction.received.format(DisplayUnit.BTC)} BTC"),
            ("Sent", f"{transaction.sent.format(DisplayUnit.BTC)} BTC"),
            ("Fee", f"{transaction.fee.format(DisplayUnit.SATS)} sats"),
            ("Time", format_transaction_time(transaction, detailed=True)),
            ("Status", "Confirmed" if transaction.confirmed else "Unconfirmed"),
            ("Confirmations", str(transaction.confirmations)),
            (
                "Block height",
                str(transaction.block_height)
                if transaction.block_height is not None
                else "—",
            ),
            ("Account", ", ".join(transaction.account_ids) or "—"),
            ("Address type", ", ".join(transaction.address_types) or "—"),
        )
        for row, (label, value) in enumerate(rows):
            tk.Label(
                content, text=label, bg=theme.CARD, fg=theme.MUTED,
                font=theme.font_small, anchor="w",
            ).grid(row=row, column=0, sticky="nw", padx=(0, 18), pady=4)
            tk.Label(
                content, text=value, bg=theme.CARD, fg=theme.TEXT,
                font=theme.font_small, anchor="w", justify="left",
            ).grid(row=row, column=1, sticky="ew", pady=4)

        next_row = len(rows)
        tk.Label(
            content, text="Addresses", bg=theme.CARD, fg=theme.MUTED,
            font=theme.font_small, anchor="nw",
        ).grid(row=next_row, column=0, sticky="nw", padx=(0, 18), pady=(8, 4))
        addresses = tk.Text(
            content,
            height=min(max(len(transaction.addresses), 2), 5),
            wrap="char",
            bg=theme.INPUT_BG,
            fg=theme.TEXT,
            font=theme.font_address,
            relief="flat",
            padx=8,
            pady=6,
        )
        addresses.insert("1.0", "\n".join(transaction.addresses) or "—")
        addresses.configure(state="disabled")
        addresses.grid(row=next_row, column=1, sticky="nsew", pady=(8, 4))
        content.grid_rowconfigure(next_row, weight=1)

        next_row += 1
        tk.Label(
            content, text="TXID", bg=theme.CARD, fg=theme.MUTED,
            font=theme.font_small, anchor="w",
        ).grid(row=next_row, column=0, sticky="w", padx=(0, 18), pady=(12, 4))
        txid_row = tk.Frame(content, bg=theme.CARD)
        txid_row.grid(row=next_row, column=1, sticky="ew", pady=(12, 4))
        txid_row.grid_columnconfigure(0, weight=1)
        txid_value = tk.StringVar(txid_row, value=transaction.txid)
        txid_entry = tk.Entry(
            txid_row,
            textvariable=txid_value,
            state="readonly",
            readonlybackground=theme.INPUT_BG,
            fg=theme.TEXT,
            font=theme.font_address,
            relief="flat",
        )
        txid_entry.grid(row=0, column=0, sticky="ew", ipady=7)
        txid_entry.bind("<Double-Button-1>", self._open_explorer)
        self.copy_button = tk.Button(
            txid_row,
            text="Copy",
            command=self._copy_txid,
            relief="flat",
            cursor="hand2",
            takefocus=True,
        )
        self.copy_button.grid(row=0, column=1, padx=(8, 0), ipady=4)

        next_row += 1
        explorer_link = tk.Label(
            content,
            text="View on mempool.space ↗",
            bg=theme.CARD,
            fg=theme.PURPLE,
            font=theme.font_small_bold,
            cursor="hand2",
            takefocus=True,
        )
        explorer_link.grid(row=next_row, column=1, sticky="w", pady=(8, 2))
        explorer_link.bind("<Button-1>", self._open_explorer)
        explorer_link.bind("<Return>", self._open_explorer)
        explorer_link.bind("<space>", self._open_explorer)

        next_row += 1
        close_button = tk.Button(
            content, text="Close", command=self.destroy, relief="flat", takefocus=True
        )
        close_button.grid(row=next_row, column=1, sticky="e", pady=(14, 0))

        show_centered_modal(
            self,
            parent,
            minimum_width=500,
            minimum_height=520,
            initial_focus=explorer_link,
        )

    def _copy_txid(self):
        self.clipboard_clear()
        self.clipboard_append(self.transaction.txid)
        self.update_idletasks()
        self.copy_button.configure(text="Copied")
        self.after(1000, lambda: self.copy_button.configure(text="Copy"))

    def _open_explorer(self, _event=None):
        url = self.transaction.explorer_url
        parsed = urlparse(url)
        if (
            not re.fullmatch(r"[0-9a-fA-F]{64}", self.transaction.txid)
            or parsed.scheme != "https"
            or parsed.hostname != "mempool.space"
        ):
            messagebox.showerror("Open Transaction", "Invalid transaction link.", parent=self)
            return "break"
        try:
            opened = webbrowser.open_new_tab(url)
        except webbrowser.Error as exc:
            messagebox.showerror("Open Transaction", str(exc), parent=self)
            return "break"
        if not opened:
            messagebox.showerror(
                "Open Transaction", "No web browser could be opened.", parent=self
            )
        return "break"


def show_transaction_details(
    parent: tk.Misc, theme: Theme, transaction: TransactionSummary
) -> TransactionDetailsDialog:
    return TransactionDetailsDialog(parent, theme, transaction)
