import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

from theme import Theme
from state import WalletUIState
from wallet_core import DisplayUnit, TransactionDirection, TransactionSummary
from wallet_dialogs import create_new_wallet, import_wallet
from wallet_manager import show_wallet_manager
from widgets import (
    AmountEntry,
    BitcoinLogo,
    DualActionBar,
    FeeSlider,
    PillowToggle,
    ResponsiveQR,
    RoundedButton,
    RoundedEntry,
    RoundedPanel,
    ScrollableFrame,
)
from transaction_dialog import format_transaction_time, show_transaction_details
from withdrawal_dialogs import review_withdrawal


class WalletSelector(tk.Frame):
    """Compact active-wallet menu shared by every wallet header."""

    def __init__(self, master, theme: Theme, state: WalletUIState):
        super().__init__(master, bg=theme.CARD, bd=0, cursor="hand2", takefocus=True)
        self.theme = theme
        self.state = state

        self.name_label = tk.Label(
            self,
            textvariable=state.wallet_name,
            bg=theme.CARD,
            fg=theme.MUTED,
            font=theme.font_wallet_title,
            cursor="hand2",
        )
        self.name_label.pack(side="left")
        self.arrow_label = tk.Label(
            self,
            text="▾",
            bg=theme.CARD,
            fg=theme.MUTED_2,
            font=theme.font_small,
            cursor="hand2",
            padx=theme.px(5),
        )
        self.arrow_label.pack(side="left", pady=(theme.px(3), 0))

        for widget in (self, self.name_label, self.arrow_label):
            widget.bind("<Button-1>", self._show_menu)
        self.bind("<Return>", self._show_menu)
        self.bind("<space>", self._show_menu)

    def _show_menu(self, event=None):
        menu = tk.Menu(self, tearoff=False, font=self.theme.font_small)
        for wallet in self.state.wallets:
            active = wallet.name == self.state.active_wallet_name
            label = f"{wallet.name}  ✓" if active else wallet.name
            menu.add_command(
                label=label,
                command=lambda name=wallet.name: self._select_wallet(name),
            )
        if self.state.wallets:
            menu.add_separator()
        menu.add_command(label="Manage Wallets...", command=self._manage_wallets)

        try:
            menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())
        finally:
            menu.grab_release()

    def _select_wallet(self, wallet_name: str) -> None:
        try:
            self.state.select_wallet(wallet_name)
        except ValueError as exc:
            messagebox.showerror("Open Wallet", str(exc), parent=self.winfo_toplevel())

    def _manage_wallets(self) -> None:
        show_wallet_manager(self.winfo_toplevel(), self.theme, self.state)


class WalletHeader(tk.Frame):
    def __init__(self, master, theme: Theme, state: WalletUIState):
        super().__init__(master, bg=theme.CARD, bd=0)
        self.theme = theme
        self.state = state
        self._balance_resize_id = None
        self.grid_columnconfigure(0, weight=1)

        left = tk.Frame(self, bg=theme.CARD, bd=0)
        left.grid(row=0, column=0, sticky="nsew")

        WalletSelector(left, theme, state).pack(anchor="w")
        self.balance_font = tkfont.Font(
            root=self,
            family=theme.family,
            size=40,
            weight="bold",
        )
        self.balance_label = tk.Label(
            left,
            text="",
            bg=theme.CARD,
            fg=theme.TEXT,
            font=self.balance_font,
        )
        self.balance_label.pack(anchor="w", pady=(theme.px(18), 0))
        self.unit_label = tk.Label(left, text="", bg=theme.CARD, fg=theme.MUTED,
                                   font=theme.font_unit)
        self.unit_label.pack(anchor="w")

        self.right = tk.Frame(self, bg=theme.CARD, bd=0)
        self.right.grid(row=0, column=1, sticky="ne")

        menu_button = tk.Label(self.right, text="...", bg=theme.CARD, fg=theme.MUTED,
                               font=theme.font_menu, cursor="hand2", padx=4)
        menu_button.pack(anchor="e", pady=(0, theme.px(1)))
        self.menu = self._build_menu()
        menu_button.bind("<Button-1>", self._show_menu)
        menu_button.bind("<Enter>", lambda e: menu_button.configure(fg=theme.TEXT_SOFT))
        menu_button.bind("<Leave>", lambda e: menu_button.configure(fg=theme.MUTED))
        BitcoinLogo(self.right, theme).pack(anchor="e")

        state.display_unit.trace_add("write", lambda *_: self._update_balance())
        state.revision.trace_add("write", lambda *_: self._update_balance())
        self.bind("<Configure>", self._schedule_balance_fit, add="+")
        self.bind("<<UIScaleChanged>>", self._schedule_balance_fit, add="+")
        self._update_balance()

    def _update_balance(self):
        value, unit = self.state.formatted_balance()
        self.balance_label.configure(text=value)
        self.unit_label.configure(text=unit)
        self._schedule_balance_fit()

    def _schedule_balance_fit(self, _event=None):
        if self._balance_resize_id is not None:
            try:
                self.after_cancel(self._balance_resize_id)
            except tk.TclError:
                pass
        self._balance_resize_id = self.after_idle(self._fit_balance_font)

    def _fit_balance_font(self):
        self._balance_resize_id = None
        available_width = max(
            70,
            self.winfo_width() - self.right.winfo_reqwidth() - self.theme.px(12),
        )
        base_size = max(14, int(round(40 * self.theme.scale)))
        self.balance_font.configure(size=base_size)
        measured_width = self.balance_font.measure(self.balance_label.cget("text"))
        if measured_width > available_width:
            fitted_size = max(12, int(base_size * available_width / measured_width))
            self.balance_font.configure(size=fitted_size)

    def _build_menu(self):
        menu = tk.Menu(self, tearoff=False, font=self.theme.font_small)
        menu.add_command(label="Create new wallet...", command=self._create_wallet)
        menu.add_command(label="Import secret words...", command=self._import_wallet)
        menu.add_separator()
        settings = tk.Menu(menu, tearoff=False, font=self.theme.font_small)
        settings.add_command(label="Wallets...", command=self._manage_wallets)
        settings.add_separator()
        fiat = tk.Menu(settings, tearoff=False, font=self.theme.font_small)
        for code in ("USD", "JPY", "CNY", "EUR"):
            fiat.add_radiobutton(label=code, value=code, variable=self.state.fiat_currency)
        settings.add_cascade(label="Fiat currency", menu=fiat)
        settings.add_checkbutton(label="Dark mode", variable=self.state.dark_mode)
        settings.add_separator()
        settings.add_command(label="Advanced Settings...", command=self._advanced_settings)
        menu.add_cascade(label="Settings", menu=settings)
        return menu

    def _create_wallet(self):
        create_new_wallet(self.winfo_toplevel(), self.state)

    def _import_wallet(self):
        import_wallet(self.winfo_toplevel(), self.state)

    def _manage_wallets(self):
        show_wallet_manager(self.winfo_toplevel(), self.theme, self.state)

    def _advanced_settings(self):
        messagebox.showinfo(
            "Advanced Settings",
            "Advanced settings will be supplied as independent features.",
            parent=self.winfo_toplevel(),
        )

    def _show_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()


class BackButton(tk.Label):
    def __init__(self, master, theme: Theme, command):
        super().__init__(master, text="‹ Back", bg=theme.CARD, fg=theme.MUTED,
                         font=theme.font_back, cursor="hand2")
        self.bind("<Button-1>", lambda e: command())
        self.bind("<Enter>", lambda e: self.configure(fg=theme.TEXT_SOFT))
        self.bind("<Leave>", lambda e: self.configure(fg=theme.MUTED))


class TransactionItem(tk.Frame):
    def __init__(self, master, theme: Theme, transaction: TransactionSummary, command):
        super().__init__(
            master,
            bg=theme.CARD,
            bd=0,
            cursor="hand2",
            takefocus=True,
            highlightthickness=1,
            highlightbackground=theme.CARD,
        )
        self.command = command
        incoming = transaction.direction is TransactionDirection.INCOMING
        kind = {
            TransactionDirection.INCOMING: "Deposit",
            TransactionDirection.OUTGOING: "Withdrawal",
            TransactionDirection.SELF: "Internal transfer",
        }[transaction.direction]
        amount = transaction.amount.format(
            DisplayUnit.BTC,
            signed=transaction.direction is not TransactionDirection.SELF,
        )
        icon = {
            TransactionDirection.INCOMING: "↘",
            TransactionDirection.OUTGOING: "↗",
            TransactionDirection.SELF: "↔",
        }[transaction.direction]
        accent = theme.GREEN if incoming else theme.PURPLE
        self.grid_columnconfigure(1, weight=1)
        tk.Label(self, text=icon, bg=theme.CARD,
                 fg=accent, font=theme.font_body
                 ).grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, theme.px(8)))
        tk.Label(self, text=kind, bg=theme.CARD, fg=theme.TEXT_SOFT, font=theme.font_body,
                 anchor="w").grid(row=0, column=1, sticky="ew")
        tk.Label(self, text=format_transaction_time(transaction), bg=theme.CARD, fg=theme.MUTED_2,
                 font=theme.font_small, anchor="w").grid(row=1, column=1, sticky="ew", pady=(theme.px(2), 0))
        tk.Label(self, text=amount, bg=theme.CARD, fg=theme.TEXT_SOFT,
                 font=theme.font_body_bold, anchor="e").grid(row=0, column=2, sticky="e")
        tk.Label(self, text="BTC", bg=theme.CARD, fg=theme.MUTED_2,
                 font=theme.font_small, anchor="e").grid(row=1, column=2, sticky="e", pady=(theme.px(2), 0))
        self._bind_activation(self)
        self.bind("<Return>", self._activate)
        self.bind("<space>", self._activate)
        self.bind(
            "<FocusIn>",
            lambda _event: self.configure(highlightbackground=theme.PURPLE),
        )
        self.bind(
            "<FocusOut>",
            lambda _event: self.configure(highlightbackground=theme.CARD),
        )

    def _bind_activation(self, widget):
        widget.bind("<Button-1>", self._activate, add="+")
        for child in widget.winfo_children():
            self._bind_activation(child)

    def _activate(self, _event=None):
        self.focus_set()
        self.command()
        return "break"


class PageBase(tk.Frame):
    def __init__(self, master, theme: Theme, state: WalletUIState):
        super().__init__(master, bg=theme.BG, bd=0)
        self.theme = theme
        self.state = state
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.panel = RoundedPanel(self, theme)
        self.panel.grid(row=0, column=0, sticky="nsew", padx=theme.px(14), pady=theme.px(14))
        self.content = self.panel.content
        self.bind("<<UIScaleChanged>>", self._rescale_panel_margin, add="+")

    def _rescale_panel_margin(self, event=None):
        self.panel.grid_configure(padx=self.theme.px(14), pady=self.theme.px(14))


class HomePage(PageBase):
    def __init__(self, master, theme, state, on_receive, on_send):
        super().__init__(master, theme, state)
        c = self.content
        c.grid_columnconfigure(0, weight=1)
        c.grid_rowconfigure(1, weight=1)
        WalletHeader(c, theme, state).grid(row=0, column=0, sticky="ew",
                                           padx=theme.px(18), pady=(theme.px(14), theme.px(7)))
        self.transaction_list = ScrollableFrame(c, theme)
        self.transaction_list.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=theme.px(18),
            pady=(theme.px(4), theme.px(8)),
        )
        state.revision.trace_add("write", lambda *_: self._render_transactions())
        self._render_transactions()
        DualActionBar(c, theme, left_command=on_send, right_command=on_receive).grid(
            row=2, column=0, sticky="ew", padx=theme.px(5), pady=(theme.px(6), theme.px(5)))

    def _render_transactions(self):
        content = self.transaction_list.content
        for child in content.winfo_children():
            child.destroy()
        self._transaction_items = []
        if not self.state.is_initialized:
            self._render_empty_wallet_actions(content)
            return
        if not self.state.transactions:
            tk.Label(
                content,
                text="No transactions yet",
                bg=self.theme.CARD,
                fg=self.theme.MUTED_2,
                font=self.theme.font_body,
            ).grid(row=0, column=0, sticky="n", pady=self.theme.px(24))
            return
        for row, transaction in enumerate(self.state.transactions):
            item = TransactionItem(
                content,
                self.theme,
                transaction,
                command=lambda selected=transaction: self._show_transaction(selected),
            )
            item.grid(
                row=row, column=0, sticky="ew", pady=self.theme.px(4)
            )
            item.bind("<Up>", lambda _event, index=row: self._focus_item(index - 1))
            item.bind("<Down>", lambda _event, index=row: self._focus_item(index + 1))
            item.bind("<Prior>", lambda _event: self.transaction_list._scroll_pages(-1))
            item.bind("<Next>", lambda _event: self.transaction_list._scroll_pages(1))
            self.transaction_list.bind_mousewheel_tree(item)
            self._transaction_items.append(item)

    def _render_empty_wallet_actions(self, content):
        empty = tk.Frame(content, bg=self.theme.CARD, bd=0)
        empty.grid(row=0, column=0, sticky="nsew", pady=self.theme.px(22))
        empty.grid_columnconfigure(0, weight=1)
        empty.grid_columnconfigure(1, weight=1)
        tk.Label(
            empty,
            text="No wallet is open",
            bg=self.theme.CARD,
            fg=self.theme.TEXT_SOFT,
            font=self.theme.font_body_bold,
        ).grid(row=0, column=0, columnspan=2, pady=(0, self.theme.px(5)))
        tk.Label(
            empty,
            text="Create a new wallet or import your secret words.",
            bg=self.theme.CARD,
            fg=self.theme.MUTED,
            font=self.theme.font_small,
        ).grid(row=1, column=0, columnspan=2, pady=(0, self.theme.px(14)))
        RoundedButton(
            empty,
            self.theme,
            text="CREATE WALLET",
            command=lambda: create_new_wallet(self.winfo_toplevel(), self.state),
            height=44,
            radius=14,
        ).grid(row=2, column=0, sticky="ew", padx=(0, self.theme.px(5)))
        RoundedButton(
            empty,
            self.theme,
            text="IMPORT WALLET",
            command=lambda: import_wallet(self.winfo_toplevel(), self.state),
            fill=self.theme.PURPLE_SOFT,
            height=44,
            radius=14,
        ).grid(row=2, column=1, sticky="ew", padx=(self.theme.px(5), 0))

    def _focus_item(self, index):
        if not self._transaction_items:
            return "break"
        target = max(0, min(index, len(self._transaction_items) - 1))
        self._transaction_items[target].focus_set()
        self.transaction_list.canvas.yview_moveto(
            target / max(len(self._transaction_items) - 1, 1)
        )
        return "break"

    def _show_transaction(self, transaction):
        show_transaction_details(self.winfo_toplevel(), self.theme, transaction)


class ReceivePage(PageBase):
    def __init__(self, master, theme, state, on_back):
        super().__init__(master, theme, state)
        c = self.content
        c.grid_columnconfigure(0, weight=1)
        c.grid_rowconfigure(3, weight=1)
        WalletHeader(c, theme, state).grid(row=0, column=0, sticky="ew",
                                           padx=theme.px(18), pady=(theme.px(14), theme.px(3)))
        BackButton(c, theme, on_back).grid(row=1, column=0, sticky="w",
                                           padx=theme.px(18), pady=(theme.px(7), theme.px(7)))
        tk.Label(c, text="Send only Bitcoin to this address", bg=theme.CARD, fg=theme.MUTED,
                 font=theme.font_body, anchor="w").grid(row=2, column=0, sticky="ew",
                                                        padx=theme.px(18), pady=(theme.px(3), theme.px(5)))
        ResponsiveQR(c, theme, textvariable=state.receive_address).grid(
            row=3, column=0, sticky="nsew", padx=theme.px(18), pady=theme.px(4)
        )
        self.address_entry = RoundedEntry(
            c,
            theme,
            textvariable=state.receive_address,
            fixed_font=True,
            readonly=True,
            action_text="⧉",
            action_command=self._copy_receive_address,
        )
        self.address_entry.grid(
            row=4,
            column=0,
            sticky="ew",
            padx=theme.px(18),
            pady=(theme.px(7), theme.px(12)),
        )

    def _copy_receive_address(self):
        address = self.state.receive_address.get().strip()
        if not address:
            return
        root = self.winfo_toplevel()
        root.clipboard_clear()
        root.clipboard_append(address)
        # Keep clipboard ownership after the click handler returns.
        root.update_idletasks()


class FeeAxis(tk.Frame):
    def __init__(self, master, theme: Theme):
        super().__init__(master, bg=theme.CARD, bd=0)
        self.theme = theme
        self.custom = False
        self.render()

    def set_custom(self, custom):
        self.custom = bool(custom)
        self.render()

    def render(self):
        for child in self.winfo_children():
            child.destroy()
        for i in range(4):
            self.grid_columnconfigure(i, weight=0)
        if self.custom:
            self.grid_columnconfigure(0, weight=1)
            self.grid_columnconfigure(1, weight=1)
            tk.Label(self, text="Slow · 0 sat/vB", bg=self.theme.CARD, fg="#B3B8BD",
                     font=self.theme.font_small, anchor="w").grid(row=0, column=0, sticky="ew")
            tk.Label(self, text="Fast · 20 sat/vB", bg=self.theme.CARD, fg="#B3B8BD",
                     font=self.theme.font_small, anchor="e").grid(row=0, column=1, sticky="ew")
        else:
            labels = ("~ 24 hrs", "~ 4 hrs", "~ 60 min", "~ 10 min")
            for i, text in enumerate(labels):
                self.grid_columnconfigure(i, weight=1)
                anchor = "w" if i == 0 else ("e" if i == 3 else "center")
                tk.Label(self, text=text, bg=self.theme.CARD, fg="#B3B8BD",
                         font=self.theme.font_small, anchor=anchor).grid(row=0, column=i, sticky="ew")


class SendPage(PageBase):
    def __init__(self, master, theme, state, on_back):
        super().__init__(master, theme, state)
        c = self.content
        c.grid_columnconfigure(0, weight=1)
        c.grid_rowconfigure(2, weight=1)

        WalletHeader(c, theme, state).grid(row=0, column=0, sticky="ew",
                                           padx=theme.px(18), pady=(theme.px(14), theme.px(2)))
        BackButton(c, theme, on_back).grid(row=1, column=0, sticky="w",
                                           padx=theme.px(18), pady=(theme.px(3), theme.px(4)))

        form = tk.Frame(c, bg=theme.CARD, bd=0)
        form.grid(row=2, column=0, sticky="nsew", padx=theme.px(18), pady=(theme.px(1), theme.px(3)))
        form.grid_columnconfigure(0, weight=1)
        form.grid_rowconfigure(7, weight=1)

        amount_header = tk.Frame(form, bg=theme.CARD, bd=0)
        amount_header.grid(row=0, column=0, sticky="ew", pady=(theme.px(1), theme.px(4)))
        amount_header.grid_columnconfigure(0, weight=1)
        tk.Label(amount_header, text="Amount", bg=theme.CARD, fg=theme.TEXT,
                 font=theme.font_field).grid(row=0, column=0, sticky="w")
        max_box = tk.Frame(amount_header, bg=theme.CARD, bd=0)
        max_box.grid(row=0, column=1, sticky="e")
        self.max_toggle = PillowToggle(
            max_box,
            theme,
            checked=state.send_all.get(),
            command=self._toggle_send_all,
        )
        self.max_toggle.pack(side="left", padx=(0, theme.px(7)))
        tk.Label(max_box, text="Max", bg=theme.CARD, fg=theme.MUTED,
                 font=theme.font_small).pack(side="left")

        self.amount_entry = AmountEntry(form, theme, textvariable=state.amount,
                                        unit=state.display_unit.get(), unit_command=self._change_unit)
        self.amount_entry.grid(row=1, column=0, sticky="ew", pady=(0, theme.px(8)))

        tk.Label(form, text="Address", bg=theme.CARD, fg=theme.TEXT,
                 font=theme.font_field, anchor="w").grid(row=2, column=0, sticky="ew",
                                                         pady=(theme.px(3), theme.px(4)))
        RoundedEntry(form, theme, textvariable=state.address).grid(row=3, column=0, sticky="ew",
                                                                   pady=(0, theme.px(7)))

        fee_header = tk.Frame(form, bg=theme.CARD, bd=0)
        fee_header.grid(row=4, column=0, sticky="ew", pady=(theme.px(4), theme.px(1)))
        fee_header.grid_columnconfigure(0, weight=1)
        tk.Label(fee_header, text="Transaction Fee", bg=theme.CARD, fg=theme.MUTED,
                 font=theme.font_small).grid(row=0, column=0, sticky="w")
        custom_box = tk.Frame(fee_header, bg=theme.CARD, bd=0)
        custom_box.grid(row=0, column=1, sticky="e")
        self.custom_toggle = PillowToggle(custom_box, theme, checked=state.custom_fee.get(),
                                          command=self._toggle_custom)
        self.custom_toggle.pack(side="left", padx=(0, theme.px(7)))
        tk.Label(custom_box, text="Custom", bg=theme.CARD, fg=theme.MUTED,
                 font=theme.font_small).pack(side="left")

        self.fee_slider = FeeSlider(
            form,
            theme,
            fiat_text_callback=state.fiat_zero_text,
            custom=state.custom_fee.get(),
            active=state.is_initialized,
        )
        self.fee_slider.grid(row=5, column=0, sticky="ew", pady=(theme.px(3), 0))
        self.fee_axis = FeeAxis(form, theme)
        self.fee_axis.grid(row=6, column=0, sticky="ew")
        self.fee_axis.set_custom(state.custom_fee.get())
        tk.Frame(form, bg=theme.CARD, bd=0).grid(row=7, column=0, sticky="nsew")

        RoundedButton(c, theme, text="REVIEW WITHDRAWAL", fill=theme.PURPLE_SOFT,
                      radius=18, height=62, command=self._review_send).grid(
            row=3, column=0, sticky="ew", padx=theme.px(5), pady=(theme.px(3), theme.px(5)))

        state.amount.trace_add("write", lambda *_: self._update_fee_active())
        state.address.trace_add("write", lambda *_: self._update_fee_active())
        state.display_unit.trace_add("write", lambda *_: self._sync_segment())
        state.revision.trace_add("write", lambda *_: self._wallet_changed())
        self._update_fee_active()

    def _change_unit(self, unit):
        self.state.display_unit.set(unit)

    def _sync_segment(self):
        self.amount_entry.segment.set_value(self.state.display_unit.get())

    def _toggle_custom(self, checked):
        self.state.custom_fee.set(bool(checked))
        self.fee_slider.set_custom(checked)
        self.fee_axis.set_custom(checked)

    def _toggle_send_all(self, checked):
        self.state.send_all.set(bool(checked))
        self.amount_entry.entry.configure(state="disabled" if checked else "normal")
        self._update_fee_active()

    def _update_fee_active(self):
        self.fee_slider.set_active(self.state.is_initialized)

    def _wallet_changed(self):
        self.max_toggle.set_checked(self.state.send_all.get())
        self.amount_entry.entry.configure(
            state="disabled" if self.state.send_all.get() else "normal"
        )
        self._update_fee_active()

    def _review_send(self):
        review_withdrawal(
            self.winfo_toplevel(),
            self.theme,
            self.state,
            self.fee_slider.current_sat_vb(),
        )
