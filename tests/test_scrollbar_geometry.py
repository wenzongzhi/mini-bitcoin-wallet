from widgets import FeeSlider, _scroll_thumb_bounds


def test_scroll_thumb_tracks_visible_fraction() -> None:
    assert _scroll_thumb_bounds(0.25, 0.75, 10, 110, 20) == (35, 85)


def test_scroll_thumb_keeps_minimum_drag_length_at_track_end() -> None:
    assert _scroll_thumb_bounds(0.98, 1.0, 10, 110, 28) == (82, 110)


def test_scroll_thumb_clamps_invalid_fractions() -> None:
    assert _scroll_thumb_bounds(-1, 2, 10, 110, 20) == (10, 110)


def test_fee_slider_uses_requested_preset_and_custom_ranges() -> None:
    assert FeeSlider.PRESET_SAT_VB == (0, 1, 2, 3)
    assert FeeSlider.CUSTOM_MAX_SAT_VB == 20
