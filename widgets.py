import tkinter as tk
from PIL import Image, ImageChops, ImageDraw, ImageTk

from theme import Theme
from app_assets import BITCOIN_LOGO_PATH

SUPERSAMPLE = 4


def _scroll_thumb_bounds(first, last, track_top, track_bottom, minimum_length):
    """Calculate thumb coordinates while preserving a usable drag target."""

    track_length = max(0.0, track_bottom - track_top)
    first = max(0.0, min(1.0, float(first)))
    last = max(first, min(1.0, float(last)))
    thumb_top = track_top + track_length * first
    thumb_bottom = track_top + track_length * last

    if thumb_bottom - thumb_top < minimum_length:
        center = (thumb_top + thumb_bottom) / 2
        thumb_top = center - minimum_length / 2
        thumb_top = max(track_top, min(thumb_top, track_bottom - minimum_length))
        thumb_bottom = min(track_bottom, thumb_top + minimum_length)
    return thumb_top, thumb_bottom


def _make_corner_images(radius: int, fill: str):
    radius = max(1, int(radius))
    s = SUPERSAMPLE
    size = radius * 2
    image = Image.new("RGBA", (size * s, size * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse([0, 0, size * s - 1, size * s - 1], fill=fill)
    image = image.resize((size, size), Image.Resampling.LANCZOS)
    return (
        image.crop((0, 0, radius, radius)),
        image.crop((radius, 0, size, radius)),
        image.crop((0, radius, radius, size)),
        image.crop((radius, radius, size, size)),
    )


class RoundedScrollbar(tk.Canvas):
    """Minimal vertical scrollbar with a pill thumb and triangle buttons."""

    def __init__(self, master, theme: Theme, command):
        self.theme = theme
        self.command = command
        super().__init__(
            master,
            width=theme.px(16),
            bg=theme.CARD,
            highlightthickness=0,
            bd=0,
            takefocus=True,
            cursor="hand2",
        )
        self._first = 0.0
        self._last = 1.0
        self._drag_offset = None
        self._thumb_bounds = (0.0, 0.0)
        self._selected = False
        self._photo = None

        self.bind("<Configure>", self._draw)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", self._select)
        self.bind("<FocusOut>", self._deselect)
        self.bind("<MouseWheel>", self._on_mousewheel)
        self.bind("<Button-4>", self._on_mousewheel)
        self.bind("<Button-5>", self._on_mousewheel)
        self.bind("<Up>", lambda _event: self._scroll(-1, "units"))
        self.bind("<Down>", lambda _event: self._scroll(1, "units"))
        self.bind("<Prior>", lambda _event: self._scroll(-1, "pages"))
        self.bind("<Next>", lambda _event: self._scroll(1, "pages"))
        self.bind("<Home>", lambda _event: self._move_to(0.0))
        self.bind("<End>", lambda _event: self._move_to(1.0))
        self.bind("<<UIScaleChanged>>", self._rescale, add="+")

    def set(self, first, last):
        """Receive the visible content fractions from ``Canvas.yview``."""

        self._first = float(first)
        self._last = float(last)
        self._draw()

    def _geometry(self):
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        arrow_zone = min(self.theme.px(15), height / 3)
        gap = self.theme.px(2)
        track_top = arrow_zone + gap
        track_bottom = max(track_top, height - arrow_zone - gap)
        minimum_length = min(self.theme.px(28), track_bottom - track_top)
        return width, height, arrow_zone, track_top, track_bottom, minimum_length

    def _draw(self, _event=None):
        width, height, arrow_zone, track_top, track_bottom, minimum = self._geometry()
        self._thumb_bounds = _scroll_thumb_bounds(
            self._first, self._last, track_top, track_bottom, minimum
        )

        scale = SUPERSAMPLE
        image = Image.new(
            "RGBA", (width * scale, height * scale), self.theme.CARD
        )
        draw = ImageDraw.Draw(image)
        arrow_half_width = self.theme.px(4)
        arrow_half_height = self.theme.px(3)
        center_x = width / 2
        top_y = arrow_zone / 2
        bottom_y = height - arrow_zone / 2
        draw.polygon(
            [
                ((center_x - arrow_half_width) * scale, (top_y + arrow_half_height) * scale),
                ((center_x + arrow_half_width) * scale, (top_y + arrow_half_height) * scale),
                (center_x * scale, (top_y - arrow_half_height) * scale),
            ],
            fill=self.theme.SCROLL_ARROW,
        )
        draw.polygon(
            [
                ((center_x - arrow_half_width) * scale, (bottom_y - arrow_half_height) * scale),
                ((center_x + arrow_half_width) * scale, (bottom_y - arrow_half_height) * scale),
                (center_x * scale, (bottom_y + arrow_half_height) * scale),
            ],
            fill=self.theme.SCROLL_ARROW,
        )

        thumb_top, thumb_bottom = self._thumb_bounds
        thumb_width = self.theme.px(8)
        left = (width - thumb_width) // 2
        right = left + thumb_width
        fill = (
            self.theme.SCROLL_THUMB_ACTIVE
            if self._selected
            else self.theme.SCROLL_THUMB
        )
        draw.rounded_rectangle(
            (
                left * scale,
                round(thumb_top * scale),
                right * scale - 1,
                max(round(thumb_top * scale), round(thumb_bottom * scale) - 1),
            ),
            radius=thumb_width * scale // 2,
            fill=fill,
        )
        image = image.resize((width, height), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(image)
        self.delete("all")
        self.create_image(0, 0, image=self._photo, anchor="nw")

    def _on_press(self, event):
        self.focus_set()
        _, height, arrow_zone, _, _, _ = self._geometry()
        thumb_top, thumb_bottom = self._thumb_bounds
        if event.y <= arrow_zone:
            self.command("scroll", -1, "units")
        elif event.y >= height - arrow_zone:
            self.command("scroll", 1, "units")
        elif thumb_top <= event.y <= thumb_bottom:
            self._drag_offset = event.y - thumb_top
        elif event.y < thumb_top:
            self.command("scroll", -1, "pages")
        else:
            self.command("scroll", 1, "pages")
        return "break"

    def _on_drag(self, event):
        if self._drag_offset is None:
            return "break"
        _, _, _, track_top, track_bottom, _ = self._geometry()
        thumb_top, thumb_bottom = self._thumb_bounds
        travel = (track_bottom - track_top) - (thumb_bottom - thumb_top)
        if travel > 0:
            requested_top = event.y - self._drag_offset
            travel_fraction = max(
                0.0, min(1.0, (requested_top - track_top) / travel)
            )
            maximum_first = max(0.0, 1.0 - (self._last - self._first))
            self.command("moveto", travel_fraction * maximum_first)
        return "break"

    def _on_release(self, _event):
        self._drag_offset = None
        return "break"

    def _on_mousewheel(self, event):
        if getattr(event, "num", None) == 4:
            units = -1
        elif getattr(event, "num", None) == 5:
            units = 1
        else:
            units = -1 if getattr(event, "delta", 0) > 0 else 1
        return self._scroll(units, "units")

    def _scroll(self, amount, mode):
        self.command("scroll", amount, mode)
        return "break"

    def _move_to(self, fraction):
        self.command("moveto", fraction)
        return "break"

    def _select(self, _event=None):
        self._selected = True
        self._draw()

    def _deselect(self, _event=None):
        self._selected = False
        self._drag_offset = None
        self._draw()

    def _rescale(self, _event=None):
        self.configure(width=self.theme.px(16))
        self._draw()


class ScrollableFrame(tk.Frame):
    """Vertical content area supporting wheel, scrollbar, touch, and keyboard."""

    def __init__(self, master, theme: Theme):
        super().__init__(master, bg=theme.CARD, bd=0)
        self.theme = theme
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            self, bg=theme.CARD, highlightthickness=0, bd=0, takefocus=True
        )
        self.scrollbar = RoundedScrollbar(self, theme, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(
            row=0, column=1, sticky="ns", padx=(theme.px(7), 0)
        )
        self.content = tk.Frame(self.canvas, bg=theme.CARD, bd=0)
        self.content.grid_columnconfigure(0, weight=1)
        self._content_window = self.canvas.create_window(
            0, 0, anchor="nw", window=self.content
        )
        self.content.bind("<Configure>", self._update_scroll_region)
        self.canvas.bind("<Configure>", self._resize_content)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", self._on_mousewheel)
        self.canvas.bind("<Button-5>", self._on_mousewheel)
        self.canvas.bind("<Up>", lambda _event: self._scroll_units(-1))
        self.canvas.bind("<Down>", lambda _event: self._scroll_units(1))
        self.canvas.bind("<Prior>", lambda _event: self._scroll_pages(-1))
        self.canvas.bind("<Next>", lambda _event: self._scroll_pages(1))
        self.canvas.bind("<Home>", lambda _event: self._scroll_to(0.0))
        self.canvas.bind("<End>", lambda _event: self._scroll_to(1.0))
        self.canvas.bind("<Button-1>", lambda _event: self.canvas.focus_set())

    def _update_scroll_region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_content(self, event):
        self.canvas.itemconfigure(self._content_window, width=event.width)

    def bind_mousewheel_tree(self, widget):
        """Route wheel gestures from an item and all of its child labels."""

        widget.bind("<MouseWheel>", self._on_mousewheel, add="+")
        widget.bind("<Button-4>", self._on_mousewheel, add="+")
        widget.bind("<Button-5>", self._on_mousewheel, add="+")
        for child in widget.winfo_children():
            self.bind_mousewheel_tree(child)

    def _on_mousewheel(self, event):
        if getattr(event, "num", None) == 4:
            units = -1
        elif getattr(event, "num", None) == 5:
            units = 1
        else:
            units = -1 if getattr(event, "delta", 0) > 0 else 1
        return self._scroll_units(units)

    def _scroll_units(self, units):
        self.canvas.yview_scroll(units, "units")
        return "break"

    def _scroll_pages(self, pages):
        self.canvas.yview_scroll(pages, "pages")
        return "break"

    def _scroll_to(self, fraction):
        self.canvas.yview_moveto(fraction)
        return "break"


class AARoundedCanvas(tk.Canvas):
    """Large rounded surfaces use only tiny anti-aliased corner images."""

    def __init__(self, master, theme: Theme, parent_bg=None, **kwargs):
        self.theme = theme
        self.parent_bg = parent_bg or theme.CARD
        self._corner_cache = {}
        super().__init__(master, bg=self.parent_bg, highlightthickness=0, bd=0, **kwargs)

    def _corners(self, radius, fill):
        key = (int(radius), fill)
        if key not in self._corner_cache:
            self._corner_cache[key] = [ImageTk.PhotoImage(i) for i in _make_corner_images(*key)]
        return self._corner_cache[key]

    def draw_roundrect(self, x1, y1, x2, y2, radius, fill, tag="roundrect"):
        self.delete(tag)
        x1, y1, x2, y2 = map(int, (x1, y1, x2, y2))
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        radius = max(1, min(int(radius), w // 2, h // 2))
        tl, tr, bl, br = self._corners(radius, fill)

        # Native rectangles paint immediately during live resize/maximize.
        self.create_rectangle(x1 + radius, y1, x2 - radius, y2, fill=fill, outline="", tags=tag)
        self.create_rectangle(x1, y1 + radius, x2, y2 - radius, fill=fill, outline="", tags=tag)
        self.create_image(x1, y1, image=tl, anchor="nw", tags=tag)
        self.create_image(x2 - radius, y1, image=tr, anchor="nw", tags=tag)
        self.create_image(x1, y2 - radius, image=bl, anchor="nw", tags=tag)
        self.create_image(x2 - radius, y2 - radius, image=br, anchor="nw", tags=tag)


class RoundedPanel(AARoundedCanvas):
    def __init__(self, master, theme: Theme, radius=28):
        super().__init__(master, theme, parent_bg=theme.BG)
        self.base_radius = radius
        self.content = tk.Frame(self, bg=theme.CARD, bd=0, highlightthickness=0)
        self._content_window = self.create_window(0, 0, anchor="nw", window=self.content)
        self.bind("<Configure>", self._layout)
        self.bind("<<UIScaleChanged>>", self._layout, add="+")

    def _layout(self, event=None):
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        self.draw_roundrect(0, 0, w, h, self.theme.px(self.base_radius), self.theme.CARD, "panel")
        inset = max(10, self.theme.px(12))
        self.coords(self._content_window, inset, inset)
        self.itemconfigure(self._content_window, width=max(1, w - 2 * inset), height=max(1, h - 2 * inset))


class PillowToggle(tk.Canvas):
    def __init__(self, master, theme: Theme, checked=False, command=None):
        self.theme = theme
        self.checked = bool(checked)
        self.command = command
        self._photo = None
        super().__init__(master, bg=theme.CARD, highlightthickness=0, bd=0, cursor="hand2",
                         width=theme.px(42), height=theme.px(24))
        self.bind("<Button-1>", self._toggle)
        self.bind("<Configure>", self._draw)
        self.bind("<<UIScaleChanged>>", self._rescale, add="+")

    def set_checked(self, checked):
        self.checked = bool(checked)
        self._draw()

    def _rescale(self, event=None):
        self.configure(width=self.theme.px(42), height=self.theme.px(24))
        self._draw()

    def _toggle(self, event=None):
        self.checked = not self.checked
        self._draw()
        if self.command:
            self.command(self.checked)

    def _draw(self, event=None):
        w = max(12, self.winfo_width())
        h = max(10, self.winfo_height())
        s = SUPERSAMPLE
        image = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
        d = ImageDraw.Draw(image)
        d.rounded_rectangle([0, 0, w * s - 1, h * s - 1], radius=h * s // 2,
                            fill=self.theme.PURPLE if self.checked else "#B8C0C8")
        margin = max(2, int(round(3 * self.theme.scale)))
        knob = h - margin * 2
        x = w - knob - margin if self.checked else margin
        d.ellipse([x * s, margin * s, (x + knob) * s, (margin + knob) * s], fill="#FFFFFF")
        image = image.resize((w, h), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(image)
        self.delete("all")
        self.create_image(0, 0, image=self._photo, anchor="nw")


class RoundedButton(AARoundedCanvas):
    def __init__(self, master, theme: Theme, text, command=None, fill=None, text_fill="#FFFFFF",
                 radius=18, height=62):
        self.text = text
        self.command = command
        self.fill = fill or theme.PURPLE
        self.text_fill = text_fill
        self.base_radius = radius
        self.base_height = height
        super().__init__(master, theme, parent_bg=theme.CARD, cursor="hand2", height=theme.px(height))
        self.bind("<Configure>", self._draw)
        self.bind("<Button-1>", lambda e: self.command() if self.command else None)
        self.bind("<<UIScaleChanged>>", self._rescale, add="+")

    def _rescale(self, event=None):
        self.configure(height=self.theme.px(self.base_height))
        self._draw()

    def _draw(self, event=None):
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        self.delete("text")
        self.draw_roundrect(0, 0, w, h, self.theme.px(self.base_radius), self.fill, "buttonbg")
        self.create_text(w / 2, h / 2, text=self.text, fill=self.text_fill,
                         font=self.theme.font_button, tags="text")


class DualActionBar(AARoundedCanvas):
    def __init__(self, master, theme: Theme, left_command=None, right_command=None):
        self.left_command = left_command
        self.right_command = right_command
        self.base_height = 66
        self.base_radius = 18
        super().__init__(master, theme, parent_bg=theme.CARD, cursor="hand2", height=theme.px(self.base_height))
        self.bind("<Configure>", self._draw)
        self.bind("<Button-1>", self._click)
        self.bind("<<UIScaleChanged>>", self._rescale, add="+")

    def _rescale(self, event=None):
        self.configure(height=self.theme.px(self.base_height))
        self._draw()

    def _click(self, event):
        cmd = self.left_command if event.x < self.winfo_width() / 2 else self.right_command
        if cmd:
            cmd()

    def _draw(self, event=None):
        self.delete("all")
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        mid = w // 2
        r = max(1, min(self.theme.px(self.base_radius), h // 2, mid))
        lp = self._corners(r, self.theme.PURPLE)
        rp = self._corners(r, self.theme.GREEN)

        self.create_rectangle(r, 0, mid, h, fill=self.theme.PURPLE, outline="")
        self.create_rectangle(0, r, mid, h-r, fill=self.theme.PURPLE, outline="")
        self.create_image(0, 0, image=lp[0], anchor="nw")
        self.create_image(0, h-r, image=lp[2], anchor="nw")

        self.create_rectangle(mid, 0, w-r, h, fill=self.theme.GREEN, outline="")
        self.create_rectangle(mid, r, w, h-r, fill=self.theme.GREEN, outline="")
        self.create_image(w-r, 0, image=rp[1], anchor="nw")
        self.create_image(w-r, h-r, image=rp[3], anchor="nw")

        self.create_text(w * 0.25, h / 2, text="↗  WITHDRAW", fill="#FFFFFF", font=self.theme.font_button)
        self.create_text(w * 0.75, h / 2, text="↘  DEPOSIT", fill="#FFFFFF", font=self.theme.font_button)


class BitcoinLogo(AARoundedCanvas):
    def __init__(self, master, theme: Theme):
        self.base_size = 82
        self._source_image = Image.open(BITCOIN_LOGO_PATH).convert("RGBA")
        self._photo = None
        self._render_key = None
        super().__init__(master, theme, parent_bg=theme.CARD,
                         width=theme.px(self.base_size), height=theme.px(self.base_size))
        self.bind("<Configure>", self._draw)
        self.bind("<<UIScaleChanged>>", self._rescale, add="+")

    def _rescale(self, event=None):
        s = self.theme.px(self.base_size)
        self.configure(width=s, height=s)
        self._draw()

    def _draw(self, event=None):
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        key = (w, h)
        if key != self._render_key:
            background = self._source_image.getpixel((0, 0))
            straightened = self._source_image.rotate(
                13,
                resample=Image.Resampling.BICUBIC,
                expand=False,
                fillcolor=background,
            )
            image = straightened.resize((w, h), Image.Resampling.LANCZOS)
            rounded_mask = Image.new("L", (w, h), 0)
            radius = max(3, int(min(w, h) * 0.20))
            ImageDraw.Draw(rounded_mask).rounded_rectangle(
                (0, 0, w - 1, h - 1), radius=radius, fill=255
            )
            rounded_mask = ImageChops.multiply(image.getchannel("A"), rounded_mask)
            image.putalpha(rounded_mask)
            self._photo = ImageTk.PhotoImage(image)
            self._render_key = key
        self.delete("all")
        self.create_image(w / 2, h / 2, image=self._photo, anchor="center")


class RoundedEntry(AARoundedCanvas):
    def __init__(
        self,
        master,
        theme: Theme,
        textvariable=None,
        fixed_font=False,
        readonly=False,
        action_text=None,
        action_command=None,
    ):
        self.base_height = 58
        self.base_radius = 10
        self.textvariable = textvariable
        self.action_command = action_command
        self._action_text = action_text
        super().__init__(master, theme, parent_bg=theme.CARD, height=theme.px(self.base_height))
        self.entry = tk.Entry(self, textvariable=textvariable, bd=0, relief="flat",
                              bg=theme.INPUT_BG, fg=theme.TEXT, insertbackground=theme.TEXT,
                              font=theme.font_address if fixed_font else theme.font_field)
        if readonly:
            self.entry.configure(state="readonly", readonlybackground=theme.INPUT_BG)
        self._entry_window = self.create_window(0, 0, anchor="nw", window=self.entry)
        self.action_button = None
        self._action_window = None
        if action_text is not None:
            self.action_button = tk.Button(
                self,
                text=action_text,
                command=self._invoke_action,
                bg=theme.BORDER,
                activebackground=theme.MUTED_2,
                fg=theme.TEXT_SOFT,
                activeforeground=theme.TEXT,
                font=theme.font_body,
                relief="flat",
                bd=0,
                highlightthickness=0,
                cursor="hand2",
                takefocus=True,
            )
            self._action_window = self.create_window(
                0, 0, anchor="e", window=self.action_button
            )
            if textvariable is not None:
                textvariable.trace_add("write", self._text_changed)
        self.bind("<Configure>", self._layout)
        self.bind("<<UIScaleChanged>>", self._rescale, add="+")

    def _text_changed(self, *_):
        self._layout()

    def _invoke_action(self):
        if self.action_command is None:
            return
        self.action_command()
        if self.action_button is not None:
            self.action_button.configure(text="✓")
            self.after(1000, self._restore_action_text)

    def _restore_action_text(self):
        if self.action_button is not None and self.action_button.winfo_exists():
            self.action_button.configure(text=self._action_text)

    def _rescale(self, event=None):
        self.configure(height=self.theme.px(self.base_height))
        self._layout()

    def _layout(self, event=None):
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        self.draw_roundrect(0, 0, w, h, self.theme.px(self.base_radius), self.theme.INPUT_BG, "entrybg")
        self.tag_lower("entrybg")
        px = self.theme.px(14)
        py = self.theme.px(7)
        action_size = self.theme.px(36) if self._action_window is not None else 0
        action_gap = self.theme.px(8) if self._action_window is not None else 0
        self.coords(self._entry_window, px, py)
        self.itemconfigure(
            self._entry_window,
            width=max(20, w - 2 * px - action_size - action_gap),
            height=max(20, h - 2 * py),
        )
        if self._action_window is not None:
            has_value = bool(self.textvariable and self.textvariable.get().strip())
            self.coords(self._action_window, w - px, h / 2)
            self.itemconfigure(
                self._action_window,
                width=action_size,
                height=action_size,
                state="normal" if has_value else "hidden",
            )


class SegmentedControl(tk.Canvas):
    def __init__(self, master, theme: Theme, value="BTC", command=None):
        self.theme = theme
        self.value = value
        self.command = command
        self._photo = None
        super().__init__(master, bg=theme.INPUT_BG, highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Button-1>", self._click)
        self.bind("<Configure>", self._draw)
        self.bind("<<UIScaleChanged>>", self._draw, add="+")

    def set_value(self, value):
        if value in ("Sats", "BTC"):
            self.value = value
            self._draw()

    def _click(self, event):
        value = "Sats" if event.x < self.winfo_width() / 2 else "BTC"
        if value != self.value:
            self.value = value
            self._draw()
            if self.command:
                self.command(value)

    def _draw(self, event=None):
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        s = SUPERSAMPLE
        img = Image.new("RGBA", (w*s, h*s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        r = h*s//2
        d.rounded_rectangle([0, 0, w*s-1, h*s-1], radius=r, fill="#E9ECEC")
        half = w*s//2
        box = [2*s, 2*s, half+2*s, h*s-2*s] if self.value == "Sats" else [half-2*s, 2*s, w*s-2*s, h*s-2*s]
        d.rounded_rectangle(box, radius=max(1, r-2*s), fill="#FFFFFF")
        img = img.resize((w, h), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(img)
        self.delete("all")
        self.create_image(0, 0, image=self._photo, anchor="nw")
        self.create_text(w*.25, h/2, text="Sats",
                         fill=self.theme.TEXT_SOFT if self.value == "Sats" else self.theme.MUTED_2,
                         font=self.theme.font_small_bold)
        self.create_text(w*.75, h/2, text="BTC",
                         fill=self.theme.TEXT_SOFT if self.value == "BTC" else self.theme.MUTED_2,
                         font=self.theme.font_small_bold)


class AmountEntry(AARoundedCanvas):
    def __init__(self, master, theme: Theme, textvariable, unit="BTC", unit_command=None):
        self.base_height = 58
        self.base_radius = 10
        super().__init__(master, theme, parent_bg=theme.CARD, height=theme.px(self.base_height))
        self.entry = tk.Entry(self, textvariable=textvariable, bd=0, relief="flat",
                              bg=theme.INPUT_BG, fg=theme.TEXT, insertbackground=theme.TEXT,
                              font=theme.font_field)
        self.segment = SegmentedControl(self, theme, value=unit, command=unit_command)
        self._entry_window = self.create_window(0, 0, anchor="nw", window=self.entry)
        self._segment_window = self.create_window(0, 0, anchor="ne", window=self.segment)
        self.bind("<Configure>", self._layout)
        self.bind("<<UIScaleChanged>>", self._rescale, add="+")

    def _rescale(self, event=None):
        self.configure(height=self.theme.px(self.base_height))
        self._layout()

    def _layout(self, event=None):
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        self.draw_roundrect(0, 0, w, h, self.theme.px(self.base_radius), self.theme.INPUT_BG, "amountbg")
        self.tag_lower("amountbg")
        pad = self.theme.px(11)
        seg_w = self.theme.px(112)
        seg_h = self.theme.px(36)
        self.segment.configure(width=seg_w, height=seg_h)
        self.coords(self._entry_window, pad, self.theme.px(7))
        self.itemconfigure(self._entry_window, width=max(30, w-seg_w-pad*3), height=max(20, h-self.theme.px(14)))
        self.coords(self._segment_window, w-pad, (h-seg_h)/2)


class FeeTooltip:
    def __init__(self, owner, theme: Theme):
        self.owner = owner
        self.theme = theme
        self.window = None

    def show(self, text, root_x, root_y):
        if self.window is None:
            top = tk.Toplevel(self.owner)
            top.withdraw()
            top.overrideredirect(True)
            top.configure(bg="#FFFFFF", bd=1, relief="solid")
            try:
                top.attributes("-topmost", True)
            except tk.TclError:
                pass
            self.label = tk.Label(top, text="", bg="#FFFFFF", fg=self.theme.TEXT,
                                  font=self.theme.font_tooltip, padx=10, pady=6)
            self.label.pack()
            arrow = tk.Canvas(top, width=18, height=9, bg="#FFFFFF", highlightthickness=0, bd=0)
            arrow.pack()
            arrow.create_polygon(2, 0, 16, 0, 9, 8, fill="#FFFFFF", outline=self.theme.BORDER)
            self.window = top

        self.label.configure(text=text)
        self.window.update_idletasks()
        width = self.window.winfo_reqwidth()
        height = self.window.winfo_reqheight()
        screen_w = self.window.winfo_screenwidth()
        x = max(4, min(screen_w-width-4, int(root_x-width/2)))
        y = int(root_y-height-8)
        self.window.geometry(f"+{x}+{y}")
        self.window.deiconify()
        self.window.lift()

    def hide(self):
        if self.window is not None:
            self.window.withdraw()


class FeeSlider(tk.Canvas):
    PRESET_SAT_VB = (0, 1, 2, 3)
    CUSTOM_MAX_SAT_VB = 20

    def __init__(self, master, theme: Theme, fiat_text_callback, custom=False, active=False):
        self.theme = theme
        self.fiat_text_callback = fiat_text_callback
        self.custom = bool(custom)
        self.active = bool(active)
        self.value = 10 if self.custom else 2
        self.base_height = 44
        self._track_photo = None
        self._knob_photo = None
        self._track_key = None
        self._knob_key = None
        self._tooltip_hide_job = None
        super().__init__(master, bg=theme.CARD, highlightthickness=0, bd=0,
                         cursor="arrow", height=theme.px(self.base_height))
        self.tooltip = FeeTooltip(self, theme)
        self.bind("<Configure>", self._draw)
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Leave>", self._hide_tooltip)
        self.bind("<<UIScaleChanged>>", self._rescale, add="+")

    def set_custom(self, custom):
        self.custom = bool(custom)
        self.value = 10 if self.custom else 2
        self._draw()

    def set_active(self, active):
        self.active = bool(active)
        self.configure(cursor="hand2" if self.active else "arrow")
        if not self.active:
            self.tooltip.hide()
        self._draw()

    def current_sat_vb(self):
        return int(self.value) if self.custom else int(self.PRESET_SAT_VB[int(self.value)])

    def _rescale(self, event=None):
        self.configure(height=self.theme.px(self.base_height))
        self._track_key = None
        self._knob_key = None
        self._draw()

    def _press(self, event):
        if self.active:
            self._set_from_x(event.x)
            self._show_tooltip()

    def _drag(self, event):
        if self.active:
            self._set_from_x(event.x)
            self._show_tooltip()

    def _geometry(self):
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        knob_r = max(10, self.theme.px(13))
        margin = knob_r + self.theme.px(3)  # fixes endpoint clipping
        usable = max(1, w - margin * 2)
        ratio = (
            int(self.value) / self.CUSTOM_MAX_SAT_VB
            if self.custom
            else int(self.value) / 3.0
        )
        return w, h, knob_r, margin, usable, margin + usable*ratio, h/2

    def _set_from_x(self, x):
        _, _, _, margin, usable, _, _ = self._geometry()
        ratio = max(0.0, min(1.0, (x-margin)/usable))
        self.value = (
            round(ratio * self.CUSTOM_MAX_SAT_VB)
            if self.custom
            else round(ratio * 3)
        )
        self._draw()

    def _release(self, _event=None):
        # Keep the selected fee visible long enough to read after a click.
        self._cancel_tooltip_hide()
        self._tooltip_hide_job = self.after(800, self._hide_tooltip_after_delay)

    def _cancel_tooltip_hide(self):
        if self._tooltip_hide_job is not None:
            self.after_cancel(self._tooltip_hide_job)
            self._tooltip_hide_job = None

    def _hide_tooltip(self, _event=None):
        self._cancel_tooltip_hide()
        self.tooltip.hide()

    def _hide_tooltip_after_delay(self):
        self._tooltip_hide_job = None
        self.tooltip.hide()

    @staticmethod
    def _hex_to_rgb(value):
        v = value.lstrip("#")
        return tuple(int(v[i:i+2], 16) for i in (0, 2, 4))

    def _make_track(self, width, height):
        key = (int(width), int(height), self.active)
        if key == self._track_key and self._track_photo is not None:
            return
        s = SUPERSAMPLE
        rw, rh = max(2, int(width))*s, max(2, int(height))*s
        if self.active:
            left = self._hex_to_rgb(self.theme.ORANGE)
            right = self._hex_to_rgb(self.theme.GREEN)
            seed = Image.new("RGB", (256, 1))
            px = seed.load()
            for i in range(256):
                t = i/255.0
                px[i, 0] = tuple(int(round(left[c] + (right[c]-left[c])*t)) for c in range(3))
            img = seed.resize((rw, rh), Image.Resampling.BILINEAR).convert("RGBA")
        else:
            img = Image.new("RGBA", (rw, rh), self.theme.TRACK_OFF)
        mask = Image.new("L", (rw, rh), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, rw-1, rh-1], radius=rh//2, fill=255)
        img.putalpha(mask)
        img = img.resize((int(width), int(height)), Image.Resampling.LANCZOS)
        self._track_photo = ImageTk.PhotoImage(img)
        self._track_key = key

    def _make_knob(self, diameter):
        key = (int(diameter), self.active)
        if key == self._knob_key and self._knob_photo is not None:
            return
        s = SUPERSAMPLE
        d = max(4, int(diameter))
        img = Image.new("RGBA", (d*s, d*s), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        dr.ellipse([s, s, (d-1)*s, (d-1)*s],
                   fill="#FFFFFF" if self.active else "#E5E6E7",
                   outline="#D0D4D8" if self.active else "#D9DDE0",
                   width=s)
        img = img.resize((d, d), Image.Resampling.LANCZOS)
        self._knob_photo = ImageTk.PhotoImage(img)
        self._knob_key = key

    def _draw(self, event=None):
        self.delete("all")
        w, h, knob_r, margin, _, x, y = self._geometry()
        track_h = max(5, self.theme.px(6))
        track_w = max(2, int(w - 2*margin))
        self._make_track(track_w, track_h)
        self._make_knob(knob_r*2)
        self.create_image(margin, y, image=self._track_photo, anchor="w")
        self.create_image(x, y, image=self._knob_photo, anchor="center")

    def _show_tooltip(self):
        self._cancel_tooltip_hide()
        _, _, _, _, _, x, _ = self._geometry()
        text = f"{self.current_sat_vb()} sat/vB  ≈  {self.fiat_text_callback()}"
        self.tooltip.show(text, self.winfo_rootx()+x, self.winfo_rooty()+self.winfo_height()/2)


class ResponsiveQR(tk.Canvas):
    """A responsive QR code that always represents its bound text value."""

    def __init__(self, master, theme: Theme, textvariable: tk.StringVar):
        self.theme = theme
        self.textvariable = textvariable
        self._matrix = None
        self._matrix_value = None
        super().__init__(master, bg=theme.CARD, highlightthickness=0, bd=0)
        self.bind("<Configure>", self._draw)
        self.textvariable.trace_add("write", self._address_changed)

    def _address_changed(self, *_):
        self._matrix = None
        self._draw()

    def _qr_matrix(self):
        value = self.textvariable.get().strip()
        if not value:
            return []
        if self._matrix is not None and value == self._matrix_value:
            return self._matrix
        try:
            import qrcode
        except ImportError:
            return None
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=1,
            border=3,
        )
        qr.add_data(value)
        qr.make(fit=True)
        self._matrix = qr.get_matrix()
        self._matrix_value = value
        return self._matrix

    def _draw(self, event=None):
        self.delete("all")
        w, h = max(2, self.winfo_width()), max(2, self.winfo_height())
        size = min(int(min(w, h, self.theme.px(285))), w, h)
        if size <= 20:
            return
        matrix = self._qr_matrix()
        if matrix == []:
            return
        if matrix is None:
            self.create_text(
                w / 2,
                h / 2,
                text="Install project requirements to render the address QR code.",
                fill=self.theme.MUTED,
                font=self.theme.font_small,
                width=max(40, w - self.theme.px(40)),
                justify="center",
            )
            return
        modules = len(matrix)
        cell = size / modules
        xoff, yoff = (w-size)/2, (h-size)/2
        self.create_rectangle(xoff, yoff, xoff+size, yoff+size, fill="#FFFFFF", outline="")
        for yy in range(modules):
            for xx in range(modules):
                if matrix[yy][xx]:
                    x1 = xoff + xx*cell; y1 = yoff + yy*cell
                    x2 = xoff + (xx+1)*cell + .5; y2 = yoff + (yy+1)*cell + .5
                    self.create_rectangle(x1, y1, x2, y2, fill="#000000", outline="")
