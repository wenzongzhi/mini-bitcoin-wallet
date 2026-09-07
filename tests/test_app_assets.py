from PIL import Image

from app_assets import APP_ICON_FALLBACK_PATH, APP_ICON_PATH, ICON_DIRECTORY


def test_icon_assets_live_in_icon_directory() -> None:
    assert ICON_DIRECTORY.name == "icon"
    assert APP_ICON_PATH.parent == ICON_DIRECTORY


def test_windows_icon_contains_common_dpi_sizes() -> None:
    expected_sizes = {
        (16, 16),
        (20, 20),
        (24, 24),
        (32, 32),
        (40, 40),
        (48, 48),
        (64, 64),
        (128, 128),
        (256, 256),
    }

    with Image.open(APP_ICON_PATH) as icon:
        assert icon.format == "ICO"
        assert icon.ico.sizes() == expected_sizes


def test_cross_platform_icon_fallback_is_rgba() -> None:
    with Image.open(APP_ICON_FALLBACK_PATH) as icon:
        assert icon.size == (256, 256)
        assert icon.mode == "RGBA"
