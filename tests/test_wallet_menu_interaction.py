"""Regression coverage for opening dropdowns without activating an item."""

from types import SimpleNamespace
import tkinter as tk
from unittest.mock import Mock

import pytest

import pages
from pages import WalletHeader, WalletSelector
from theme import Theme


@pytest.fixture
def wallet_ui():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display is unavailable: {exc}")
    root.withdraw()
    # Mouse event_generate needs mapped widgets. Keep the mapped test window
    # transparent so the regression exercises Tk bindings without flashing UI.
    root.attributes("-alpha", 0)
    root.geometry("520x420")
    state = SimpleNamespace(
        wallet_name=tk.StringVar(root, value="Test Wallet"),
        active_wallet_name="Test Wallet",
        wallets=(SimpleNamespace(name="Test Wallet"),),
        display_unit=tk.StringVar(root, value="BTC"),
        hide_balance=tk.BooleanVar(root, value=False),
        revision=tk.IntVar(root, value=0),
        is_initialized=True,
        formatted_balance=lambda: ("0", "BTC"),
        select_wallet=Mock(),
    )
    try:
        theme = Theme(root)
        root.deiconify()
        root.update()
        yield root, theme, state
    finally:
        root.destroy()


def test_settings_opening_click_does_not_activate_create_wallet(
    wallet_ui, monkeypatch,
) -> None:
    root, theme, state = wallet_ui
    create_wallet = Mock()
    monkeypatch.setattr(pages, "create_new_wallet", create_wallet)
    header = WalletHeader(root, theme, state)
    header.pack()
    root.update_idletasks()
    popup = Mock()
    monkeypatch.setattr(header.menu, "tk_popup", popup)

    header.menu_button.event_generate("<ButtonPress-1>", x=4, y=4)
    popup.assert_not_called()
    create_wallet.assert_not_called()

    header.menu_button.event_generate("<ButtonRelease-1>", x=4, y=4)

    popup.assert_called_once_with(
        header.menu_button.winfo_rootx(),
        header.menu_button.winfo_rooty() + header.menu_button.winfo_height(),
    )
    assert header.menu.index("active") is None
    create_wallet.assert_not_called()

    # Creating a wallet still requires an explicit menu-item activation.
    header.menu.invoke(0)
    create_wallet.assert_called_once_with(root, state)


def test_settings_can_open_without_mouse_coordinates(wallet_ui, monkeypatch) -> None:
    root, theme, state = wallet_ui
    header = WalletHeader(root, theme, state)
    popup = Mock()
    monkeypatch.setattr(header.menu, "tk_popup", popup)

    assert header._show_menu() == "break"
    popup.assert_called_once()
    assert header.menu_button.bind("<Return>")
    assert header.menu_button.bind("<space>")


def test_wallet_selector_opens_only_after_release(wallet_ui, monkeypatch) -> None:
    root, theme, state = wallet_ui
    selector = WalletSelector(root, theme, state)
    selector.pack()
    root.update_idletasks()
    menu = selector._new_menu()
    popup = Mock()
    monkeypatch.setattr(menu, "tk_popup", popup)
    monkeypatch.setattr(selector, "_new_menu", lambda: menu)

    selector.name_label.event_generate("<ButtonPress-1>", x=4, y=4)
    popup.assert_not_called()
    selector.name_label.event_generate("<ButtonRelease-1>", x=4, y=4)

    popup.assert_called_once_with(
        selector.winfo_rootx(),
        selector.winfo_rooty() + selector.winfo_height(),
    )
    state.select_wallet.assert_not_called()
