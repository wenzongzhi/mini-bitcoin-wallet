"""About dialog for public product, author, and license information."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
import webbrowser

from app_assets import apply_window_icon
from app_metadata import (
    APP_AUTHOR,
    APP_COPYRIGHT,
    APP_DESCRIPTION,
    APP_LICENSE,
    APP_NAME,
    APP_SECURITY_NOTICE,
    APP_SOURCE_URL,
    APP_VERSION,
)
from dialog_utils import show_centered_modal
from theme import Theme
from widgets import BitcoinLogo, RoundedButton


class AboutWalletDialog(tk.Toplevel):
    """Display stable release metadata without reading wallet secrets."""

    def __init__(self, parent: tk.Misc, theme: Theme):
        super().__init__(parent)
        self.withdraw()
        self.theme = theme
        self.title(f"About {APP_NAME}")
        apply_window_icon(self)
        self.transient(parent)
        self.configure(bg=theme.BG)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())

        panel = tk.Frame(
            self,
            bg=theme.CARD,
            padx=theme.px(28),
            pady=theme.px(24),
        )
        panel.pack(
            fill="both",
            expand=True,
            padx=theme.px(14),
            pady=theme.px(14),
        )

        BitcoinLogo(panel, theme).pack(pady=(0, theme.px(12)))
        tk.Label(
            panel,
            text=APP_NAME,
            bg=theme.CARD,
            fg=theme.TEXT,
            font=theme.font_wallet_title,
        ).pack()
        tk.Label(
            panel,
            text=f"Version {APP_VERSION}",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=theme.font_small,
        ).pack(pady=(theme.px(3), theme.px(12)))
        tk.Label(
            panel,
            text=APP_DESCRIPTION,
            bg=theme.CARD,
            fg=theme.TEXT_SOFT,
            font=theme.font_small,
            justify="center",
            wraplength=theme.px(360),
        ).pack(pady=(0, theme.px(15)))

        details = tk.Frame(panel, bg=theme.CARD)
        details.pack(fill="x")
        details.grid_columnconfigure(1, weight=1)
        for row, (label, value) in enumerate(
            (
                ("Author", APP_AUTHOR),
                ("Copyright", APP_COPYRIGHT),
                ("License", APP_LICENSE),
            )
        ):
            tk.Label(
                details,
                text=label,
                bg=theme.CARD,
                fg=theme.MUTED,
                font=theme.font_small,
                anchor="w",
            ).grid(row=row, column=0, sticky="nw", padx=(0, theme.px(18)), pady=3)
            tk.Label(
                details,
                text=value,
                bg=theme.CARD,
                fg=theme.TEXT,
                font=theme.font_small,
                anchor="w",
            ).grid(row=row, column=1, sticky="ew", pady=3)

        source_link = tk.Label(
            panel,
            text="View source code on GitHub ↗",
            bg=theme.CARD,
            fg=theme.PURPLE,
            font=theme.font_small_bold,
            cursor="hand2",
            takefocus=True,
        )
        source_link.pack(pady=(theme.px(14), theme.px(12)))
        source_link.bind("<Button-1>", self._open_source)
        source_link.bind("<Return>", self._open_source)
        source_link.bind("<space>", self._open_source)

        tk.Label(
            panel,
            text=APP_SECURITY_NOTICE,
            bg=theme.INPUT_BG,
            fg=theme.MUTED,
            font=theme.font_small,
            justify="center",
            wraplength=theme.px(350),
            padx=theme.px(12),
            pady=theme.px(9),
        ).pack(fill="x", pady=(0, theme.px(14)))

        close_button = RoundedButton(
            panel,
            theme,
            text="CLOSE",
            command=self.destroy,
            height=44,
            radius=14,
        )
        close_button.pack(fill="x")
        show_centered_modal(
            self,
            parent,
            minimum_width=440,
            minimum_height=570,
            initial_focus=source_link,
        )

    def _open_source(self, _event=None):
        try:
            opened = webbrowser.open_new_tab(APP_SOURCE_URL)
        except webbrowser.Error as exc:
            messagebox.showerror("Open Source Code", str(exc), parent=self)
            return "break"
        if not opened:
            messagebox.showerror(
                "Open Source Code",
                "No web browser could be opened.",
                parent=self,
            )
        return "break"


def show_about_wallet(parent: tk.Misc, theme: Theme) -> None:
    AboutWalletDialog(parent, theme)
