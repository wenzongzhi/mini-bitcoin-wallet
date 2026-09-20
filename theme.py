"""Application colours, fonts, and responsive UI scaling.

The product stores a user-facing theme mode (``system``, ``light``, or
``dark``). ``system`` intentionally resolves to the light palette for now;
keeping the stored mode distinct lets native OS-theme detection be added
later without changing the settings schema.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Iterator, Mapping


BASE_WIDTH = 460
BASE_HEIGHT = 780

SUPPORTED_THEME_MODES = ("system", "light", "dark")

LIGHT_PALETTE = {
    "BG": "#EEF2F6",
    "CARD": "#FFFFFF",
    "TEXT": "#11151A",
    "TEXT_SOFT": "#565D64",
    "MUTED": "#747B82",
    "MUTED_2": "#92989E",
    "BORDER": "#D8DDE2",
    "INPUT_BG": "#F4F7F7",
    "PURPLE": "#5754F8",
    "PURPLE_SOFT": "#8987F6",
    "GREEN": "#16B97B",
    "ORANGE": "#F59B45",
    "TRACK_OFF": "#E1E4E7",
    "SCROLL_THUMB": "#E2E3E5",
    "SCROLL_THUMB_ACTIVE": "#8F9398",
    "SCROLL_ARROW": "#D7D9DC",
}

DARK_PALETTE = {
    "BG": "#171A1F",
    "CARD": "#22262C",
    "TEXT": "#F5F7FA",
    "TEXT_SOFT": "#D7DBE0",
    "MUTED": "#AAB0B7",
    "MUTED_2": "#838A92",
    "BORDER": "#3B4149",
    "INPUT_BG": "#2B3037",
    "PURPLE": "#8583FF",
    "PURPLE_SOFT": "#6F6DE8",
    "GREEN": "#20C88A",
    "ORANGE": "#F7A85B",
    "TRACK_OFF": "#3A4048",
    "SCROLL_THUMB": "#555C65",
    "SCROLL_THUMB_ACTIVE": "#9AA1AA",
    "SCROLL_ARROW": "#6E757D",
}


def resolve_theme_mode(mode: str) -> str:
    """Return the concrete palette name for a persisted theme mode."""

    if mode not in SUPPORTED_THEME_MODES:
        supported = ", ".join(SUPPORTED_THEME_MODES)
        raise ValueError(f"Unsupported theme mode {mode!r}; expected {supported}.")
    return "light" if mode == "system" else mode


def palette_for_mode(mode: str) -> Mapping[str, str]:
    """Return the semantic colour palette selected by ``mode``."""

    return LIGHT_PALETTE if resolve_theme_mode(mode) == "light" else DARK_PALETTE


class Theme:
    """Mutable presentation theme shared by all Tk widgets in one window."""

    # Class defaults keep imports and type inspection predictable. Every
    # instance shadows these values with its selected palette at startup.
    BG = LIGHT_PALETTE["BG"]
    CARD = LIGHT_PALETTE["CARD"]
    TEXT = LIGHT_PALETTE["TEXT"]
    TEXT_SOFT = LIGHT_PALETTE["TEXT_SOFT"]
    MUTED = LIGHT_PALETTE["MUTED"]
    MUTED_2 = LIGHT_PALETTE["MUTED_2"]
    BORDER = LIGHT_PALETTE["BORDER"]
    INPUT_BG = LIGHT_PALETTE["INPUT_BG"]
    PURPLE = LIGHT_PALETTE["PURPLE"]
    PURPLE_SOFT = LIGHT_PALETTE["PURPLE_SOFT"]
    GREEN = LIGHT_PALETTE["GREEN"]
    ORANGE = LIGHT_PALETTE["ORANGE"]
    TRACK_OFF = LIGHT_PALETTE["TRACK_OFF"]
    SCROLL_THUMB = LIGHT_PALETTE["SCROLL_THUMB"]
    SCROLL_THUMB_ACTIVE = LIGHT_PALETTE["SCROLL_THUMB_ACTIVE"]
    SCROLL_ARROW = LIGHT_PALETTE["SCROLL_ARROW"]

    _TK_COLOUR_OPTIONS = (
        "background",
        "foreground",
        "activebackground",
        "activeforeground",
        "disabledbackground",
        "disabledforeground",
        "highlightbackground",
        "highlightcolor",
        "insertbackground",
        "readonlybackground",
        "selectbackground",
        "selectcolor",
        "selectforeground",
        "troughcolor",
    )

    def __init__(self, root: tk.Misc, mode: str = "system"):
        self.root = root
        self.scale = 1.0
        self._after_id = None
        self.mode = mode
        self.resolved_mode = resolve_theme_mode(mode)
        self._palette = dict(palette_for_mode(mode))
        self._set_palette_attributes(self._palette)

        # The root may have been configured with the legacy light colours
        # before Theme was constructed. Translate those colours before the
        # first window is shown to avoid a light-theme flash in dark mode.
        self._replace_widget_tree_colours(LIGHT_PALETTE, self._palette)

        default = tkfont.nametofont("TkDefaultFont")
        fixed = tkfont.nametofont("TkFixedFont")
        self.family = default.actual("family")
        self.fixed_family = fixed.actual("family")

        self.font_wallet_title = tkfont.Font(root=root, family=self.family, size=22)
        self.font_balance = tkfont.Font(
            root=root, family=self.family, size=40, weight="bold"
        )
        self.font_unit = tkfont.Font(root=root, family=self.family, size=18)
        self.font_body = tkfont.Font(root=root, family=self.family, size=16)
        self.font_body_bold = tkfont.Font(
            root=root, family=self.family, size=16, weight="bold"
        )
        self.font_small = tkfont.Font(root=root, family=self.family, size=12)
        self.font_small_bold = tkfont.Font(
            root=root, family=self.family, size=12, weight="bold"
        )
        self.font_field = tkfont.Font(root=root, family=self.family, size=17)
        self.font_button = tkfont.Font(root=root, family=self.family, size=16)
        self.font_back = tkfont.Font(root=root, family=self.family, size=19)
        self.font_address = tkfont.Font(
            root=root, family=self.fixed_family, size=11
        )
        self.font_menu = tkfont.Font(
            root=root, family=self.family, size=20, weight="bold"
        )
        self.font_tooltip = tkfont.Font(
            root=root, family=self.family, size=11, weight="bold"
        )
        self.font_logo = tkfont.Font(
            root=root, family=self.family, size=32, weight="bold"
        )

        self._base_sizes = [
            (self.font_wallet_title, 22),
            (self.font_balance, 40),
            (self.font_unit, 18),
            (self.font_body, 16),
            (self.font_body_bold, 16),
            (self.font_small, 12),
            (self.font_small_bold, 12),
            (self.font_field, 17),
            (self.font_button, 16),
            (self.font_back, 19),
            (self.font_address, 11),
            (self.font_menu, 20),
            (self.font_tooltip, 11),
            (self.font_logo, 32),
        ]

        root.bind("<Configure>", self._on_root_configure, add="+")

    def apply_mode(self, mode: str) -> bool:
        """Apply ``mode`` to live Tk widgets and announce custom redraws.

        Standard Tk widget options that still contain a colour from the old
        semantic palette are translated recursively. Custom Canvas widgets
        receive ``<<ThemeChanged>>`` and remain responsible for redrawing
        their own canvas items.

        Returns ``True`` when the selected mode changed.
        """

        resolved_mode = resolve_theme_mode(mode)
        if mode == self.mode:
            return False

        old_palette = self._palette
        new_palette = dict(palette_for_mode(mode))
        self.mode = mode
        self.resolved_mode = resolved_mode
        self._palette = new_palette
        self._set_palette_attributes(new_palette)

        widgets = tuple(self._widget_tree())
        self._replace_widgets_colours(widgets, old_palette, new_palette)
        for widget in widgets:
            try:
                widget.event_generate("<<ThemeChanged>>", when="tail")
            except tk.TclError:
                # A modal may close while queued theme work is being applied.
                continue
        return True

    def px(self, value: float) -> int:
        return max(1, int(round(value * self.scale)))

    def _set_palette_attributes(self, palette: Mapping[str, str]) -> None:
        for name, colour in palette.items():
            setattr(self, name, colour)

    def _replace_widget_tree_colours(
        self,
        old_palette: Mapping[str, str],
        new_palette: Mapping[str, str],
    ) -> None:
        self._replace_widgets_colours(
            tuple(self._widget_tree()), old_palette, new_palette
        )

    def _widget_tree(self) -> Iterator[tk.Misc]:
        pending = [self.root]
        while pending:
            widget = pending.pop()
            yield widget
            try:
                pending.extend(widget.winfo_children())
            except tk.TclError:
                continue

    @classmethod
    def _replace_widgets_colours(
        cls,
        widgets: tuple[tk.Misc, ...],
        old_palette: Mapping[str, str],
        new_palette: Mapping[str, str],
    ) -> None:
        colour_translation = {
            old_palette[name].lower(): new_palette[name]
            for name in old_palette.keys() & new_palette.keys()
        }
        for widget in widgets:
            changes = {}
            for option in cls._TK_COLOUR_OPTIONS:
                try:
                    current = str(widget.cget(option))
                except tk.TclError:
                    continue
                replacement = colour_translation.get(current.lower())
                if replacement is not None and replacement != current:
                    changes[option] = replacement
            if not changes:
                continue
            try:
                widget.configure(**changes)
            except tk.TclError:
                # Some platform-native widgets expose a read-only colour
                # option. Other widgets in the tree should still update.
                continue

    def _on_root_configure(self, event):
        if event.widget is not self.root:
            return
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
        self._after_id = self.root.after(
            60, lambda: self._update_scale(event.width, event.height)
        )

    def _update_scale(self, width: int, height: int):
        self._after_id = None
        new_scale = min(width / BASE_WIDTH, height / BASE_HEIGHT)
        new_scale = max(0.88, min(1.28, new_scale))
        if abs(new_scale - self.scale) < 0.03:
            return
        self.scale = new_scale
        for font, base_size in self._base_sizes:
            font.configure(size=max(9, int(round(base_size * self.scale))))
        self.root.event_generate("<<UIScaleChanged>>", when="tail")
