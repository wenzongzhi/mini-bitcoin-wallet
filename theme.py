import tkinter as tk
import tkinter.font as tkfont

BASE_WIDTH = 460
BASE_HEIGHT = 780


class Theme:
    BG = "#EEF2F6"
    CARD = "#FFFFFF"
    TEXT = "#11151A"
    TEXT_SOFT = "#565D64"
    MUTED = "#747B82"
    MUTED_2 = "#92989E"
    BORDER = "#D8DDE2"
    INPUT_BG = "#F4F7F7"
    PURPLE = "#5754F8"
    PURPLE_SOFT = "#8987F6"
    GREEN = "#16B97B"
    ORANGE = "#F59B45"
    TRACK_OFF = "#E1E4E7"
    SCROLL_THUMB = "#E2E3E5"
    SCROLL_THUMB_ACTIVE = "#8F9398"
    SCROLL_ARROW = "#D7D9DC"

    def __init__(self, root: tk.Misc):
        self.root = root
        self.scale = 1.0
        self._after_id = None

        default = tkfont.nametofont("TkDefaultFont")
        fixed = tkfont.nametofont("TkFixedFont")
        self.family = default.actual("family")
        self.fixed_family = fixed.actual("family")

        self.font_wallet_title = tkfont.Font(root=root, family=self.family, size=22)
        self.font_balance = tkfont.Font(root=root, family=self.family, size=40, weight="bold")
        self.font_unit = tkfont.Font(root=root, family=self.family, size=18)
        self.font_body = tkfont.Font(root=root, family=self.family, size=16)
        self.font_body_bold = tkfont.Font(root=root, family=self.family, size=16, weight="bold")
        self.font_small = tkfont.Font(root=root, family=self.family, size=12)
        self.font_small_bold = tkfont.Font(root=root, family=self.family, size=12, weight="bold")
        self.font_field = tkfont.Font(root=root, family=self.family, size=17)
        self.font_button = tkfont.Font(root=root, family=self.family, size=16)
        self.font_back = tkfont.Font(root=root, family=self.family, size=19)
        self.font_address = tkfont.Font(root=root, family=self.fixed_family, size=11)
        self.font_menu = tkfont.Font(root=root, family=self.family, size=20, weight="bold")
        self.font_tooltip = tkfont.Font(root=root, family=self.family, size=11, weight="bold")
        self.font_logo = tkfont.Font(root=root, family=self.family, size=32, weight="bold")

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

    def px(self, value: float) -> int:
        return max(1, int(round(value * self.scale)))

    def _on_root_configure(self, event):
        if event.widget is not self.root:
            return
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
        self._after_id = self.root.after(60, lambda: self._update_scale(event.width, event.height))

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
