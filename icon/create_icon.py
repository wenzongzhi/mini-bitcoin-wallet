"""Build the multi-resolution application icon from the supplied Bitcoin logo.

Windows title bars render icons at several small, DPI-dependent sizes. Keeping
those sizes in one ICO file avoids scaling a 256 px image at runtime, which can
crop the circle and make the white glyph look tinted or jagged.
"""

from pathlib import Path

from PIL import Image


ICON_DIRECTORY = Path(__file__).resolve().parent
SOURCE_LOGO_PATH = ICON_DIRECTORY / "bitcoin_logo.png"
TITLEBAR_PREVIEW_PATH = ICON_DIRECTORY / "bitcoin_titlebar_icon.png"
APP_ICON_PATH = ICON_DIRECTORY / "bitcoin_tool_titlebar_icon.ico"

MASTER_SIZE = 1024
ICON_OCCUPANCY = 0.86
STRAIGHTENING_ANGLE_DEGREES = 13
WINDOWS_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def build_titlebar_master(source_path: Path) -> Image.Image:
    """Return a straight, centered icon with transparent anti-crop padding."""

    source = Image.open(source_path).convert("RGBA")
    source = source.resize((MASTER_SIZE, MASTER_SIZE), Image.Resampling.LANCZOS)

    # The supplied mark is intentionally tilted. Straightening the whole mark
    # keeps the circular background unchanged while making the B read cleanly.
    straightened = source.rotate(
        STRAIGHTENING_ANGLE_DEGREES,
        resample=Image.Resampling.BICUBIC,
        expand=False,
        fillcolor=(0, 0, 0, 0),
    )

    occupied_size = round(MASTER_SIZE * ICON_OCCUPANCY)
    occupied_size -= occupied_size % 2
    inset = (MASTER_SIZE - occupied_size) // 2
    mark = straightened.resize(
        (occupied_size, occupied_size), Image.Resampling.LANCZOS
    )

    master = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    master.alpha_composite(mark, (inset, inset))
    return master


def build_icon_bundle() -> None:
    """Write a preview PNG and a DPI-aware Windows ICO bundle."""

    master = build_titlebar_master(SOURCE_LOGO_PATH)
    preview = master.resize((256, 256), Image.Resampling.LANCZOS)
    preview.save(TITLEBAR_PREVIEW_PATH, format="PNG", optimize=True)
    master.save(
        APP_ICON_PATH,
        format="ICO",
        sizes=[(size, size) for size in WINDOWS_ICON_SIZES],
        bitmap_format="png",
    )


if __name__ == "__main__":
    build_icon_bundle()
