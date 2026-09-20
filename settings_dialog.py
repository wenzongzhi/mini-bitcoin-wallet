"""Application-level settings UI and testable settings orchestration.

The dialog deliberately owns only product preferences. Wallet lifecycle
operations stay in ``wallet_settings_dialog.py`` and Bitcoin/network behavior
continues to be implemented by the bitcoin-tool Platform.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Callable, Protocol

from app_assets import apply_window_icon
from app_settings import (
    ApplicationSettings,
    ApplicationSettingsStore,
    BackendSettings,
    GeneralSettings,
    SettingsError,
    SettingsValidationError,
    StorageSettings,
    unencrypted_http_warning,
    wallet_data_dir_from_selected_file,
)
from btc.chainparams import get_chain_params
from dialog_utils import show_centered_modal
from network import EsploraBackend
from theme import Theme
from wallet import default_wallet_cache_file, default_wallet_file


_DISPLAY_UNITS = ("BTC", "sats")
_FIAT_CURRENCIES = ("USD", "JPY", "CNY", "EUR")
_THEME_LABELS = {"System": "system", "Light": "light", "Dark": "dark"}
_THEME_NAMES = {value: label for label, value in _THEME_LABELS.items()}


class SettingsState(Protocol):
    """Small state boundary used by the settings controller."""

    is_initialized: bool

    def apply_general_settings(self, general: GeneralSettings) -> None: ...

    def request_wallet_refresh(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SettingsDraft:
    """Validated values currently edited by an Application Settings dialog."""

    general: GeneralSettings
    backend: BackendSettings
    storage: StorageSettings

    @classmethod
    def from_settings(
        cls,
        settings: ApplicationSettings,
        network: str,
    ) -> "SettingsDraft":
        return cls(
            general=settings.general,
            backend=settings.network[network],
            storage=settings.storage,
        )


@dataclass(frozen=True, slots=True)
class SettingsSaveResult:
    """Effects of committing one dialog draft."""

    settings: ApplicationSettings
    general_changed: bool
    backend_changed: bool
    restart_required: bool


@dataclass(frozen=True, slots=True)
class BackendConnectionResult:
    """Successful Esplora connection details safe to present in the UI."""

    network: str
    endpoint: str
    block_height: int


def backend_verification_key(
    network: str,
    backend: BackendSettings,
) -> tuple[str, str] | None:
    """Return the identity that must match the last successful custom test."""

    if backend.backend_mode != "custom":
        return None
    assert backend.custom_esplora_url is not None
    return network, backend.custom_esplora_url.rstrip("/")


def validated_custom_storage(
    data_dir: str | Path,
    network: str,
) -> StorageSettings:
    """Validate the network's standard wallet file and store its directory."""

    wallet_file = default_wallet_file(data_dir=data_dir, network=network)
    verified_dir = wallet_data_dir_from_selected_file(wallet_file, network)
    return StorageSettings(verified_dir)


def verify_backend_connection(
    network: str,
    backend: BackendSettings,
    *,
    backend_builder: Callable[..., EsploraBackend] = EsploraBackend,
) -> BackendConnectionResult:
    """Verify the endpoint's genesis block and read its current tip height.

    This function performs network I/O and is intentionally independent from
    Tk. The dialog calls it on a worker thread; unit tests can pass a small
    backend builder without touching the network.
    """

    if backend.backend_mode == "default":
        endpoint = get_chain_params(network).default_esplora_url
        esplora = backend_builder(network=network)
    else:
        assert backend.custom_esplora_url is not None
        endpoint = backend.custom_esplora_url.rstrip("/")
        esplora = backend_builder(base_url=endpoint, network=network)

    esplora.verify_network()
    block_height = esplora.get_tip_height()
    if isinstance(block_height, bool) or not isinstance(block_height, int):
        raise ValueError("The backend returned an invalid block height.")
    return BackendConnectionResult(
        network=network,
        endpoint=endpoint,
        block_height=block_height,
    )


class SettingsController:
    """Own Save/Cancel semantics without depending on Tk widgets."""

    def __init__(
        self,
        settings_store: ApplicationSettingsStore,
        network: str,
        state: SettingsState,
    ):
        self.settings_store = settings_store
        self.network = network
        self.state = state
        self.original_settings = settings_store.load()
        self.original = SettingsDraft.from_settings(self.original_settings, network)

        # A previously saved custom endpoint was verified when it was saved.
        # Editing it changes the key and therefore requires another test.
        self._verified_backend_key = backend_verification_key(
            network, self.original.backend
        )

    def mark_backend_verified(self, backend: BackendSettings) -> None:
        """Remember the exact custom endpoint that passed network validation."""

        self._verified_backend_key = backend_verification_key(self.network, backend)

    def is_backend_verified(self, backend: BackendSettings) -> bool:
        key = backend_verification_key(self.network, backend)
        return key is None or key == self._verified_backend_key

    def save(self, draft: SettingsDraft) -> SettingsSaveResult:
        """Persist a whole draft once, then apply runtime-safe changes."""

        if not self.is_backend_verified(draft.backend):
            raise SettingsValidationError(
                "Test this custom backend successfully before saving."
            )

        # StorageSettings models a data directory, while the UI contract
        # requires selecting an existing standard wallet file. Validate the
        # derived file again at Save time in case it changed after selection.
        if draft.storage.wallet_data_dir is not None:
            draft = SettingsDraft(
                general=draft.general,
                backend=draft.backend,
                storage=validated_custom_storage(
                    draft.storage.wallet_data_dir,
                    self.network,
                ),
            )

        general_changed = draft.general != self.original.general
        backend_changed = draft.backend != self.original.backend
        storage_changed = draft.storage != self.original.storage

        saved = self.settings_store.apply_preferences(
            general=draft.general,
            network=self.network,
            backend=draft.backend,
            storage=draft.storage,
        )
        self.state.apply_general_settings(saved.general)
        if backend_changed and self.state.is_initialized:
            self.state.request_wallet_refresh()

        self.original_settings = saved
        self.original = SettingsDraft.from_settings(saved, self.network)
        self._verified_backend_key = backend_verification_key(
            self.network, saved.network[self.network]
        )
        return SettingsSaveResult(
            settings=saved,
            general_changed=general_changed,
            backend_changed=backend_changed,
            restart_required=storage_changed,
        )

    def cancel(self) -> None:
        """Discard the draft; persistence is intentionally untouched."""


class ApplicationSettingsDialog(tk.Toplevel):
    """Three-page Application Settings window."""

    def __init__(self, parent: tk.Misc, theme: Theme, state):
        super().__init__(parent)
        self.withdraw()
        self.parent = parent
        self.theme = theme
        self.state = state
        self.controller = SettingsController(
            state.settings_store,
            state.network,
            state,
        )
        self.network = state.network
        self._connection_generation = 0
        self._connection_queue: queue.Queue = queue.Queue()
        self._connection_running = False

        self.title("Settings")
        apply_window_icon(self)
        self.transient(parent)
        self.configure(bg=theme.BG)
        self.minsize(theme.px(690), theme.px(520))
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel())

        original = self.controller.original
        self.display_unit = tk.StringVar(self, value=original.general.display_unit)
        self.fiat_currency = tk.StringVar(self, value=original.general.fiat_currency)
        self.theme_name = tk.StringVar(
            self, value=_THEME_NAMES[original.general.theme]
        )
        self.hide_balance = tk.BooleanVar(self, value=original.general.hide_balance)
        self.backend_mode = tk.StringVar(self, value=original.backend.backend_mode)
        self.custom_url = tk.StringVar(
            self, value=original.backend.custom_esplora_url or ""
        )
        self.storage_mode = tk.StringVar(
            self,
            value="custom" if original.storage.wallet_data_dir else "default",
        )
        self.custom_data_dir = tk.StringVar(
            self,
            value=(
                str(original.storage.wallet_data_dir)
                if original.storage.wallet_data_dir is not None
                else ""
            ),
        )
        self.wallet_path_text = tk.StringVar(self)
        self.cache_path_text = tk.StringVar(self)
        self.connection_status = tk.StringVar(
            self, value="Select a backend and test the connection."
        )
        self.http_warning = tk.StringVar(self)
        self.storage_status = tk.StringVar(self)

        self._build_layout()
        self.backend_mode.trace_add("write", self._backend_draft_changed)
        self.custom_url.trace_add("write", self._backend_draft_changed)
        self.storage_mode.trace_add("write", self._storage_changed)
        self._update_backend_controls(reset_status=False)
        self._update_storage_paths()
        self._show_page("General")

        show_centered_modal(
            self,
            parent,
            minimum_width=720,
            minimum_height=570,
            initial_focus=self._nav_buttons["General"],
        )

    def _build_layout(self) -> None:
        shell = tk.Frame(self, bg=self.theme.CARD)
        shell.pack(
            fill="both",
            expand=True,
            padx=self.theme.px(14),
            pady=self.theme.px(14),
        )
        shell.grid_rowconfigure(1, weight=1)
        shell.grid_columnconfigure(1, weight=1)

        tk.Label(
            shell,
            text="Settings",
            bg=self.theme.CARD,
            fg=self.theme.TEXT,
            font=self.theme.font_wallet_title,
            anchor="w",
        ).grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=self.theme.px(22),
            pady=(self.theme.px(18), self.theme.px(12)),
        )

        navigation = tk.Frame(shell, bg=self.theme.INPUT_BG, width=self.theme.px(155))
        navigation.grid(row=1, column=0, sticky="nsew", padx=(16, 0), pady=(0, 8))
        navigation.grid_propagate(False)
        self._nav_buttons: dict[str, tk.Button] = {}
        for name in ("General", "Network", "Storage"):
            button = tk.Button(
                navigation,
                text=name,
                command=lambda page=name: self._show_page(page),
                anchor="w",
                relief="flat",
                bd=0,
                bg=self.theme.INPUT_BG,
                fg=self.theme.TEXT_SOFT,
                activebackground=self.theme.TRACK_OFF,
                activeforeground=self.theme.TEXT,
                font=self.theme.font_body,
                padx=self.theme.px(16),
                pady=self.theme.px(11),
            )
            button.pack(fill="x")
            self._nav_buttons[name] = button

        content = tk.Frame(shell, bg=self.theme.CARD)
        content.grid(
            row=1,
            column=1,
            sticky="nsew",
            padx=(self.theme.px(28), self.theme.px(24)),
            pady=(0, self.theme.px(8)),
        )
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)
        self._pages = {
            name: tk.Frame(content, bg=self.theme.CARD)
            for name in ("General", "Network", "Storage")
        }
        for page in self._pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        self._build_general_page(self._pages["General"])
        self._build_network_page(self._pages["Network"])
        self._build_storage_page(self._pages["Storage"])

        actions = tk.Frame(shell, bg=self.theme.CARD)
        actions.grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="e",
            padx=self.theme.px(24),
            pady=(self.theme.px(6), self.theme.px(18)),
        )
        self._plain_button(actions, "Cancel", self._cancel).pack(side="left", padx=4)
        self._plain_button(
            actions,
            "Save",
            self._save,
            emphasized=True,
        ).pack(side="left", padx=4)

    def _page_heading(self, page: tk.Misc, title: str, description: str) -> None:
        tk.Label(
            page,
            text=title,
            bg=self.theme.CARD,
            fg=self.theme.TEXT,
            font=self.theme.font_body_bold,
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            page,
            text=description,
            bg=self.theme.CARD,
            fg=self.theme.MUTED,
            font=self.theme.font_small,
            justify="left",
            anchor="w",
            wraplength=self.theme.px(440),
        ).pack(fill="x", pady=(3, self.theme.px(18)))

    def _field_label(self, parent: tk.Misc, text: str) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            bg=self.theme.CARD,
            fg=self.theme.TEXT_SOFT,
            font=self.theme.font_small_bold,
            anchor="w",
        )

    def _plain_button(
        self,
        parent: tk.Misc,
        text: str,
        command,
        *,
        emphasized: bool = False,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            relief="flat",
            bd=0,
            bg=self.theme.PURPLE if emphasized else self.theme.INPUT_BG,
            fg="#FFFFFF" if emphasized else self.theme.TEXT,
            activebackground=(
                self.theme.PURPLE_SOFT if emphasized else self.theme.TRACK_OFF
            ),
            activeforeground="#FFFFFF" if emphasized else self.theme.TEXT,
            font=self.theme.font_small_bold,
            padx=self.theme.px(16),
            pady=self.theme.px(8),
            cursor="hand2",
        )

    def _option_menu(
        self,
        parent: tk.Misc,
        variable: tk.StringVar,
        values: tuple[str, ...],
    ) -> tk.OptionMenu:
        menu = tk.OptionMenu(parent, variable, *values)
        menu.configure(
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.theme.BORDER,
            bg=self.theme.INPUT_BG,
            fg=self.theme.TEXT,
            activebackground=self.theme.TRACK_OFF,
            activeforeground=self.theme.TEXT,
            font=self.theme.font_small,
            anchor="w",
        )
        menu["menu"].configure(
            bg=self.theme.CARD,
            fg=self.theme.TEXT,
            activebackground=self.theme.PURPLE,
            activeforeground="#FFFFFF",
            font=self.theme.font_small,
        )
        return menu

    def _radio(self, parent: tk.Misc, text: str, variable, value: str) -> tk.Radiobutton:
        return tk.Radiobutton(
            parent,
            text=text,
            variable=variable,
            value=value,
            bg=self.theme.CARD,
            fg=self.theme.TEXT,
            activebackground=self.theme.CARD,
            activeforeground=self.theme.TEXT,
            selectcolor=self.theme.INPUT_BG,
            font=self.theme.font_small,
            anchor="w",
        )

    def _build_general_page(self, page: tk.Frame) -> None:
        self._page_heading(
            page,
            "General",
            "Display and privacy preferences apply immediately after Save.",
        )
        self._field_label(page, "Bitcoin unit").pack(fill="x")
        units = tk.Frame(page, bg=self.theme.CARD)
        units.pack(fill="x", pady=(3, 14))
        for value in _DISPLAY_UNITS:
            self._radio(units, value, self.display_unit, value).pack(
                side="left", padx=(0, 18)
            )

        self._field_label(page, "Fiat currency").pack(fill="x")
        self._option_menu(page, self.fiat_currency, _FIAT_CURRENCIES).pack(
            fill="x", pady=(3, 14)
        )

        self._field_label(page, "Theme").pack(fill="x")
        self._option_menu(page, self.theme_name, tuple(_THEME_LABELS)).pack(
            fill="x", pady=(3, 18)
        )

        self._field_label(page, "Privacy").pack(fill="x", pady=(0, 4))
        tk.Checkbutton(
            page,
            text="Hide wallet balance",
            variable=self.hide_balance,
            bg=self.theme.CARD,
            fg=self.theme.TEXT,
            activebackground=self.theme.CARD,
            activeforeground=self.theme.TEXT,
            selectcolor=self.theme.INPUT_BG,
            font=self.theme.font_small,
            anchor="w",
        ).pack(fill="x")

    def _build_network_page(self, page: tk.Frame) -> None:
        self._page_heading(
            page,
            "Network",
            "Choose an Esplora-compatible API for this application network.",
        )
        self._field_label(page, "Bitcoin network").pack(fill="x")
        tk.Label(
            page,
            text="Mainnet" if self.network == "mainnet" else "Testnet4",
            bg=self.theme.INPUT_BG,
            fg=self.theme.TEXT,
            font=self.theme.font_small,
            anchor="w",
            padx=10,
            pady=7,
        ).pack(fill="x", pady=(3, 14))

        self._field_label(page, "Backend").pack(fill="x")
        self._radio(page, "Use bitcoin-tool default", self.backend_mode, "default").pack(
            fill="x", pady=(3, 0)
        )
        tk.Label(
            page,
            text=get_chain_params(self.network).default_esplora_url,
            bg=self.theme.CARD,
            fg=self.theme.MUTED,
            font=self.theme.font_small,
            anchor="w",
        ).pack(fill="x", padx=(24, 0), pady=(0, 8))
        self._radio(
            page,
            "Custom Esplora-compatible API",
            self.backend_mode,
            "custom",
        ).pack(fill="x")

        self.custom_url_entry = tk.Entry(
            page,
            textvariable=self.custom_url,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.theme.BORDER,
            highlightcolor=self.theme.PURPLE,
            bg=self.theme.INPUT_BG,
            fg=self.theme.TEXT,
            insertbackground=self.theme.TEXT,
            disabledbackground=self.theme.TRACK_OFF,
            disabledforeground=self.theme.MUTED,
            font=self.theme.font_small,
        )
        self.custom_url_entry.pack(fill="x", padx=(24, 0), pady=(5, 5), ipady=7)
        tk.Label(
            page,
            textvariable=self.http_warning,
            bg=self.theme.CARD,
            fg=self.theme.ORANGE,
            font=self.theme.font_small,
            justify="left",
            anchor="w",
            wraplength=self.theme.px(420),
        ).pack(fill="x", padx=(24, 0))

        self.test_connection_button = self._plain_button(
            page,
            "Test Connection",
            self._test_connection,
        )
        self.test_connection_button.pack(anchor="w", padx=(24, 0), pady=(9, 7))
        tk.Label(
            page,
            textvariable=self.connection_status,
            bg=self.theme.CARD,
            fg=self.theme.MUTED,
            font=self.theme.font_small,
            justify="left",
            anchor="w",
            wraplength=self.theme.px(420),
        ).pack(fill="x", padx=(24, 0))

    def _build_storage_page(self, page: tk.Frame) -> None:
        self._page_heading(
            page,
            "Storage",
            "Choose the wallet data location used after the application restarts.",
        )
        self._field_label(page, "Wallet data").pack(fill="x")
        self._radio(
            page,
            "Use bitcoin-tool default",
            self.storage_mode,
            "default",
        ).pack(fill="x", pady=(3, 0))
        self._radio(page, "Custom location", self.storage_mode, "custom").pack(
            fill="x", pady=(0, 10)
        )

        self._field_label(page, "Wallet file").pack(fill="x")
        tk.Label(
            page,
            textvariable=self.wallet_path_text,
            bg=self.theme.INPUT_BG,
            fg=self.theme.TEXT_SOFT,
            font=self.theme.font_small,
            justify="left",
            anchor="w",
            wraplength=self.theme.px(420),
            padx=9,
            pady=7,
        ).pack(fill="x", pady=(3, 10))
        self._field_label(page, "Cache file (managed automatically)").pack(fill="x")
        tk.Label(
            page,
            textvariable=self.cache_path_text,
            bg=self.theme.INPUT_BG,
            fg=self.theme.TEXT_SOFT,
            font=self.theme.font_small,
            justify="left",
            anchor="w",
            wraplength=self.theme.px(420),
            padx=9,
            pady=7,
        ).pack(fill="x", pady=(3, 10))

        controls = tk.Frame(page, bg=self.theme.CARD)
        controls.pack(fill="x")
        self._plain_button(
            controls,
            "Choose Wallet File…",
            self._choose_wallet_file,
        ).pack(side="left", padx=(0, 6))
        self._plain_button(controls, "Use Default", self._use_default_storage).pack(
            side="left"
        )
        tk.Label(
            page,
            textvariable=self.storage_status,
            bg=self.theme.CARD,
            fg=self.theme.ORANGE,
            font=self.theme.font_small,
            justify="left",
            anchor="w",
            wraplength=self.theme.px(420),
        ).pack(fill="x", pady=(10, 0))

    def _show_page(self, name: str) -> None:
        self._pages[name].tkraise()
        for page_name, button in self._nav_buttons.items():
            selected = page_name == name
            button.configure(
                bg=self.theme.PURPLE if selected else self.theme.INPUT_BG,
                fg="#FFFFFF" if selected else self.theme.TEXT_SOFT,
            )

    def _selected_backend(self) -> BackendSettings:
        mode = self.backend_mode.get()
        if mode == "default":
            return BackendSettings()
        url = self.custom_url.get().strip().rstrip("/")
        return BackendSettings(backend_mode="custom", custom_esplora_url=url)

    def _backend_draft_changed(self, *_args) -> None:
        self._connection_generation += 1
        self._update_backend_controls(reset_status=True)

    def _update_backend_controls(self, *, reset_status: bool) -> None:
        custom = self.backend_mode.get() == "custom"
        self.custom_url_entry.configure(state="normal" if custom else "disabled")
        if reset_status:
            self.connection_status.set("Test this backend before saving changes.")
        if not custom:
            self.http_warning.set("")
            return
        try:
            self.http_warning.set(unencrypted_http_warning(self.custom_url.get().strip()) or "")
        except SettingsValidationError:
            self.http_warning.set("")

    def _test_connection(self) -> None:
        try:
            backend = self._selected_backend()
        except SettingsValidationError as exc:
            self.connection_status.set(str(exc))
            return

        self._connection_generation += 1
        generation = self._connection_generation
        key = backend_verification_key(self.network, backend)
        self._connection_running = True
        self.test_connection_button.configure(state="disabled", cursor="arrow")
        self.connection_status.set("Testing connection…")

        def worker() -> None:
            try:
                result = verify_backend_connection(self.network, backend)
            except Exception as exc:  # Worker boundary: report, never crash Tk.
                self._connection_queue.put((generation, key, None, exc))
            else:
                self._connection_queue.put((generation, key, result, None))

        threading.Thread(
            target=worker,
            name="settings-backend-test",
            daemon=True,
        ).start()
        self.after(75, self._poll_connection)

    def _poll_connection(self) -> None:
        try:
            generation, key, result, error = self._connection_queue.get_nowait()
        except queue.Empty:
            if self.winfo_exists():
                self.after(75, self._poll_connection)
            return

        self._connection_running = False
        self.test_connection_button.configure(state="normal", cursor="hand2")
        if generation != self._connection_generation:
            return
        try:
            current_backend = self._selected_backend()
        except SettingsValidationError:
            return
        if key != backend_verification_key(self.network, current_backend):
            return
        if error is not None:
            self.connection_status.set(self._friendly_connection_error(error))
            return
        self.controller.mark_backend_verified(current_backend)
        self.connection_status.set(
            "Connected\nNetwork verified\n"
            f"Block height: {result.block_height:,}"
        )

    def _friendly_connection_error(self, error: Exception) -> str:
        detail = " ".join(str(error).split())
        if "not connected to network" in detail.lower():
            network_name = "Bitcoin Mainnet" if self.network == "mainnet" else "Bitcoin Testnet4"
            return f"The selected backend is not {network_name}."
        if "invalid block height" in detail.lower():
            return "The backend returned an invalid block height."
        # Transport exceptions can contain a private hostname, URL, proxy, or
        # local path. Keep those details out of the product UI and logs.
        return "Connection to the backend failed. Check the URL and try again."

    def _storage_changed(self, *_args) -> None:
        self._update_storage_paths()

    def _selected_data_dir(self) -> Path | None:
        if self.storage_mode.get() == "default":
            return None
        value = self.custom_data_dir.get().strip()
        return Path(value) if value else None

    def _update_storage_paths(self) -> None:
        data_dir = self._selected_data_dir()
        if self.storage_mode.get() == "custom" and data_dir is None:
            self.wallet_path_text.set("Choose a standard wallet file.")
            self.cache_path_text.set("The cache path will be selected automatically.")
        else:
            self.wallet_path_text.set(
                str(default_wallet_file(data_dir=data_dir, network=self.network))
            )
            self.cache_path_text.set(
                str(default_wallet_cache_file(data_dir=data_dir, network=self.network))
            )
        self.storage_status.set(
            "Wallet storage changes take effect after restarting Mini Bitcoin Wallet."
        )

    def _choose_wallet_file(self) -> None:
        expected = default_wallet_file(network=self.network)
        initial_dir = self.custom_data_dir.get() or str(expected.parent)
        selected = filedialog.askopenfilename(
            parent=self,
            title="Choose Wallet File",
            initialdir=initial_dir,
            initialfile=expected.name,
            filetypes=(("Bitcoin wallet JSON", expected.name), ("JSON files", "*.json")),
        )
        if not selected:
            return
        try:
            data_dir = wallet_data_dir_from_selected_file(selected, self.network)
        except SettingsValidationError as exc:
            self.storage_status.set(str(exc))
            return
        self.custom_data_dir.set(str(data_dir))
        self.storage_mode.set("custom")
        self._update_storage_paths()

    def _use_default_storage(self) -> None:
        self.storage_mode.set("default")
        self._update_storage_paths()

    def _build_draft(self) -> SettingsDraft:
        backend = self._selected_backend()
        if self.storage_mode.get() == "custom":
            data_dir = self._selected_data_dir()
            if data_dir is None:
                raise SettingsValidationError(
                    "Choose a standard wallet file for the custom location."
                )
            storage = validated_custom_storage(data_dir, self.network)
        else:
            storage = StorageSettings()
        return SettingsDraft(
            general=GeneralSettings(
                display_unit=self.display_unit.get(),
                fiat_currency=self.fiat_currency.get(),
                theme=_THEME_LABELS[self.theme_name.get()],
                hide_balance=bool(self.hide_balance.get()),
            ),
            backend=backend,
            storage=storage,
        )

    def _save(self) -> None:
        try:
            draft = self._build_draft()
            result = self.controller.save(draft)
        except (SettingsError, KeyError, ValueError) as exc:
            if "Test this custom backend" in str(exc):
                self._show_page("Network")
                self.connection_status.set(str(exc))
                return
            messagebox.showerror(
                "Save Settings",
                str(exc) or "Settings could not be saved.",
                parent=self,
            )
            return

        self.theme.apply_mode(result.settings.general.theme)
        if result.restart_required:
            messagebox.showinfo(
                "Restart Required",
                "Wallet storage location saved.\n\n"
                "Restart Mini Bitcoin Wallet to use the selected wallet file.",
                parent=self,
            )
        self.destroy()

    def _cancel(self) -> None:
        self._connection_generation += 1
        self.controller.cancel()
        self.destroy()


def show_application_settings(
    parent: tk.Misc,
    theme: Theme,
    state,
) -> None:
    ApplicationSettingsDialog(parent, theme, state)
