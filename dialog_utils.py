"""Reusable positioning and presentation helpers for application dialogs."""

from __future__ import annotations

import tkinter as tk


def show_centered_modal(
    dialog: tk.Toplevel,
    parent: tk.Misc,
    *,
    minimum_width: int,
    minimum_height: int,
    initial_focus: tk.Misc | None = None,
) -> None:
    """Reveal a fully laid-out modal without flashing its default geometry."""

    dialog.update_idletasks()
    width = max(minimum_width, dialog.winfo_reqwidth())
    height = max(minimum_height, dialog.winfo_reqheight())
    parent.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
    x = max(0, min(x, dialog.winfo_screenwidth() - width))
    y = max(0, min(y, dialog.winfo_screenheight() - height))
    dialog.geometry(f"{width}x{height}+{x}+{y}")
    dialog.deiconify()
    dialog.lift()
    dialog.grab_set()
    (initial_focus or dialog).focus_set()
