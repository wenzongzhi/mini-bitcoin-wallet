"""Central paths and helpers for application-owned visual assets."""

from pathlib import Path
import tkinter as tk


ICON_DIRECTORY = Path(__file__).resolve().parent / "icon"
APP_ICON_PATH = ICON_DIRECTORY / "bitcoin_tool_titlebar_icon.ico"
APP_ICON_FALLBACK_PATH = ICON_DIRECTORY / "bitcoin_titlebar_icon.png"
BITCOIN_LOGO_PATH = ICON_DIRECTORY / "bitcoin_logo.png"


def apply_window_icon(window: tk.Misc) -> None:
    """Apply the Bitcoin icon now and as the default for future dialogs."""

    try:
        window.iconbitmap(str(APP_ICON_PATH))
        window.iconbitmap(default=str(APP_ICON_PATH))
    except (tk.TclError, TypeError):
        # ``.ico`` is Windows-native; other window managers use the PNG copy.
        try:
            icon = tk.PhotoImage(file=str(APP_ICON_FALLBACK_PATH))
            window.iconphoto(True, icon)
            # Tk does not retain the Python object after ``iconphoto`` returns.
            setattr(window, "_app_icon_photo", icon)
        except (tk.TclError, TypeError):
            pass
