from types import SimpleNamespace
import tkinter as tk

import pytest

from state import (
    HIDDEN_BALANCE_TEXT,
    WalletUIState,
    display_unit_from_setting,
    display_unit_to_setting,
    format_wallet_balance,
)
from theme import (
    DARK_PALETTE,
    LIGHT_PALETTE,
    Theme,
    palette_for_mode,
    resolve_theme_mode,
)
from wallet_core import BitcoinAmount, DisplayUnit


def test_confirmation_monitoring_uses_only_locally_broadcast_pending_txids() -> None:
    state = object.__new__(WalletUIState)
    state._snapshot = SimpleNamespace(
        pending_txids=("ab" * 32, "ab" * 32),
        transactions=(
            SimpleNamespace(txid="cd" * 32, confirmed=False),
        ),
    )

    assert state.monitored_transaction_ids == ("ab" * 32,)


def test_settings_display_unit_mapping_is_explicit_and_round_trips() -> None:
    assert display_unit_from_setting("BTC") is DisplayUnit.BTC
    assert display_unit_from_setting("sats") is DisplayUnit.SATS
    assert display_unit_to_setting(DisplayUnit.BTC) == "BTC"
    assert display_unit_to_setting(DisplayUnit.SATS) == "sats"


@pytest.mark.parametrize("value", ["Sats", "btc", "", None])
def test_settings_display_unit_mapping_rejects_unknown_values(value) -> None:
    with pytest.raises(ValueError, match="Unsupported Bitcoin display unit"):
        display_unit_from_setting(value)


def test_hidden_balance_uses_fixed_length_mask_for_every_unit() -> None:
    balance = BitcoinAmount(12_345_678)

    assert format_wallet_balance(
        balance, DisplayUnit.BTC, hidden=True
    ) == (HIDDEN_BALANCE_TEXT, "BTC")
    assert format_wallet_balance(
        balance, DisplayUnit.SATS, hidden=True
    ) == (HIDDEN_BALANCE_TEXT, "Sats")
    assert HIDDEN_BALANCE_TEXT == "••••••••"


def test_visible_balance_uses_existing_amount_formatting() -> None:
    assert format_wallet_balance(
        BitcoinAmount(12_345_678), DisplayUnit.BTC, hidden=False
    ) == ("0.12345678", "BTC")
    assert format_wallet_balance(
        BitcoinAmount(12_345_678), DisplayUnit.SATS, hidden=False
    ) == ("12,345,678", "Sats")


def test_withdrawal_input_unit_is_separate_from_display_preference() -> None:
    interpreter = tk.Tcl()
    state = object.__new__(WalletUIState)
    state.display_unit = tk.StringVar(interpreter, value="BTC")
    state.withdrawal_unit = tk.StringVar(interpreter, value="Sats")

    assert state.unit is DisplayUnit.BTC
    assert state.withdrawal_amount_unit is DisplayUnit.SATS


def test_hidden_balance_also_masks_fiat_presentation() -> None:
    state = object.__new__(WalletUIState)
    state.hide_balance = SimpleNamespace(get=lambda: True)
    state.fiat_currency = SimpleNamespace(get=lambda: "USD")

    assert state.fiat_zero_text() == HIDDEN_BALANCE_TEXT


def test_system_theme_currently_resolves_to_light_palette() -> None:
    assert resolve_theme_mode("system") == "light"
    assert palette_for_mode("system") is LIGHT_PALETTE
    assert palette_for_mode("light") is LIGHT_PALETTE
    assert palette_for_mode("dark") is DARK_PALETTE


@pytest.mark.parametrize("mode", ["Dark", "automatic", "", None])
def test_unknown_theme_modes_are_rejected(mode) -> None:
    with pytest.raises(ValueError, match="Unsupported theme mode"):
        resolve_theme_mode(mode)


class _FakeThemeWidget:
    def __init__(self, *, background: str, foreground: str, children=()) -> None:
        self.options = {"background": background, "foreground": foreground}
        self.children = list(children)
        self.events = []

    def winfo_children(self):
        return self.children

    def cget(self, option):
        if option not in self.options:
            raise tk.TclError(f"unknown option {option}")
        return self.options[option]

    def configure(self, **changes):
        self.options.update(changes)

    def event_generate(self, event_name, *, when):
        self.events.append((event_name, when))


def test_apply_theme_recolours_and_announces_the_whole_widget_tree() -> None:
    child = _FakeThemeWidget(
        background=LIGHT_PALETTE["CARD"],
        foreground=LIGHT_PALETTE["TEXT"],
    )
    root = _FakeThemeWidget(
        background=LIGHT_PALETTE["BG"],
        foreground="SystemWindowText",
        children=(child,),
    )
    theme = object.__new__(Theme)
    theme.root = root
    theme.mode = "light"
    theme.resolved_mode = "light"
    theme._palette = dict(LIGHT_PALETTE)
    theme._set_palette_attributes(theme._palette)

    assert theme.apply_mode("dark") is True
    assert root.options["background"] == DARK_PALETTE["BG"]
    assert root.options["foreground"] == "SystemWindowText"
    assert child.options == {
        "background": DARK_PALETTE["CARD"],
        "foreground": DARK_PALETTE["TEXT"],
    }
    assert root.events == [("<<ThemeChanged>>", "tail")]
    assert child.events == [("<<ThemeChanged>>", "tail")]
    assert theme.apply_mode("dark") is False
