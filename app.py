import tkinter as tk
from tkinter import ttk
from pathlib import Path
import sys

from app_settings import (
    ApplicationSettingsStore,
    SettingsError,
    create_backend_factory,
    default_settings,
    detect_legacy_wallet_data_dir,
)
from theme import Theme
from state import WalletUIState
from pages import HomePage, ReceivePage, SendPage
from adapters import BitcoinToolWalletService
from btc.chainparams import NETWORK_MAINNET
from wallet import default_wallet_cache_file, default_wallet_file
from wallet_core import WalletApplication, WalletService
from wallet_dialogs import create_new_wallet
from app_assets import apply_window_icon
from sync_coordinator import WalletSyncCoordinator


class BitcoinWalletApp(tk.Tk):
    """Desktop composition root where a concrete wallet backend is selected."""

    def __init__(
        self,
        wallet_service: WalletService,
        settings_store: ApplicationSettingsStore,
        network: str,
        window_title="Bitcoin Wallet",
    ):
        super().__init__()

        # Hide the window until the first complete layout/render pass finishes.
        self.withdraw()
        self.title(window_title)
        apply_window_icon(self)

        general_settings = settings_store.general()
        self.theme = Theme(self, mode=general_settings.theme)
        self.configure(bg=self.theme.BG)

        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        initial_w = min(520, max(440, int(sw * 0.34)))
        initial_h = min(840, max(680, int(sh * 0.82)))
        initial_w = min(initial_w, max(390, sw - 80))
        initial_h = min(initial_h, max(640, sh - 110))
        min_w = min(410, max(370, sw - 80))
        min_h = min(660, max(610, sh - 110))
        self.geometry(f"{initial_w}x{initial_h}")
        self.minsize(min_w, min_h)

        application = WalletApplication(wallet_service)
        self.state = WalletUIState(
            self,
            application,
            settings_store,
            network,
        )
        self.sync_coordinator = WalletSyncCoordinator(self, self.state)
        self.protocol("WM_DELETE_WINDOW", self._close_application)
        self.bind(
            "<<ApplicationSettingsChanged>>",
            self._apply_application_settings,
            add="+",
        )

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        container = tk.Frame(self, bg=self.theme.BG, bd=0)
        container.pack(fill="both", expand=True)

        self.pages = {
            "home": HomePage(container, self.theme, self.state,
                             on_receive=self.show_receive, on_send=self.show_send),
            "receive": ReceivePage(container, self.theme, self.state, on_back=self.show_home),
            "send": SendPage(container, self.theme, self.state, on_back=self.show_home),
        }
        for page in self.pages.values():
            page.place(relx=0, rely=0, relwidth=1, relheight=1)

        self.show_home()

        # Force geometry + Configure handlers to finish while hidden, then reveal.
        self.update_idletasks()
        self.update()
        self.after_idle(self._show_ready_window)

    def _apply_application_settings(self, _event=None) -> None:
        """Apply settings that are safe to change while the app is running."""

        self.theme.apply_mode(self.state.theme_mode.get())
        self.configure(bg=self.theme.BG)

    def _show_ready_window(self):
        self.deiconify()
        self.lift()
        self.sync_coordinator.start()
        if not self.state.is_initialized:
            self.after(150, self._offer_wallet_creation)

    def _offer_wallet_creation(self):
        from tkinter import messagebox

        # The user may have imported a wallet from the menu before this delayed
        # onboarding callback runs.
        if self.state.is_initialized:
            return
        should_create = messagebox.askyesno(
            "Set Up Wallet",
            "No wallet was found. Create a new encrypted wallet now?\n\n"
            "Choose No to import secret words from the top-right menu.",
            parent=self,
        )
        if should_create:
            create_new_wallet(self, self.state)

    def _close_application(self):
        self.sync_coordinator.close()
        self.destroy()

    def _show(self, name):
        self.pages[name].lift()

    def show_home(self):
        self._show("home")

    def show_receive(self):
        self._show("receive")

    def show_send(self):
        self._show("send")


def run_wallet_app(
    *,
    network: str,
    window_title: str,
):
    """Build and run one network-specific wallet application."""

    try:
        settings_store = create_settings_store(network=network)
    except SettingsError as exc:
        _show_startup_error(str(exc))
        return

    data_directory = settings_store.wallet_data_dir()
    service = BitcoinToolWalletService(
        wallet_file=default_wallet_file(data_directory, network),
        cache_file=default_wallet_cache_file(data_directory, network),
        network=network,
        backend_factory=create_backend_factory(settings_store),
        settings_store=settings_store,
    )
    BitcoinWalletApp(
        service,
        settings_store,
        network,
        window_title=window_title,
    ).mainloop()


def create_settings_store(
    *,
    network: str,
    settings_path: str | Path | None = None,
    legacy_data_directory: str | Path | None = None,
) -> ApplicationSettingsStore:
    """Create the one shared settings store used by the desktop application.

    Legacy wallet discovery runs only while the Version 2 settings file is
    first created. It selects the old directory by reference and never reads,
    copies, moves, or deletes wallet contents.
    """

    store = ApplicationSettingsStore(settings_path)
    if store.path.exists():
        store.load()
        return store

    legacy_directory = detect_legacy_wallet_data_dir(
        legacy_data_directory or legacy_wallet_data_directory(),
        network,
    )
    store.load_or_create(default_settings(legacy_directory))
    return store


def legacy_wallet_data_directory() -> Path:
    """Return the source/EXE directory used by older mini-wallet releases."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _show_startup_error(detail: str) -> None:
    """Show a readable startup error without exposing a Python traceback."""

    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    apply_window_icon(root)
    messagebox.showerror(
        "Bitcoin Wallet Settings",
        f"{detail}\n\n"
        "If this is an old development settings file, delete it and restart "
        "Mini Bitcoin Wallet.",
        parent=root,
    )
    root.destroy()


def main():
    run_wallet_app(
        network=NETWORK_MAINNET,
        window_title="Bitcoin Wallet — MAINNET",
    )


if __name__ == "__main__":
    main()
