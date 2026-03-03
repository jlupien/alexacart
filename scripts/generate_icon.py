#!/usr/bin/env python3
"""
Generate the AlexaCart app icon (.icns) for macOS.

Usage:  uv run --with pillow python scripts/generate_icon.py

The output (scripts/AlexaCart.icns) is committed to the repo so that
build_macos_app.sh works without Pillow installed.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("Pillow is required: uv run --with pillow python scripts/generate_icon.py")
    sys.exit(1)

SIZE = 1024
BG_COLOR = (16, 185, 129)  # Emerald green
FG_COLOR = (255, 255, 255)  # White


def draw_icon():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # --- Background: rounded rectangle ---
    draw.rounded_rectangle([40, 40, 984, 984], radius=180, fill=BG_COLOR)

    # --- Shopping cart (white) ---
    lw = 42  # line width

    # Handle: horizontal grip → diagonal down to basket
    handle_pts = [(185, 300), (295, 300), (370, 620)]
    draw.line(handle_pts, fill=FG_COLOR, width=lw, joint="curve")
    # Round the line ends
    r = lw // 2
    for pt in [handle_pts[0], handle_pts[-1]]:
        draw.ellipse([pt[0] - r, pt[1] - r, pt[0] + r, pt[1] + r], fill=FG_COLOR)

    # Basket: filled trapezoid (wider at top)
    basket = [(345, 400), (835, 400), (780, 660), (400, 660)]
    draw.polygon(basket, fill=FG_COLOR)

    # Wire-basket lines (green stripes across the basket)
    for frac in [0.35, 0.65]:
        y = int(400 + (660 - 400) * frac)
        # Interpolate the x edges of the trapezoid at this y
        x_left = int(345 + (400 - 345) * frac) + 8
        x_right = int(835 + (780 - 835) * frac) - 8
        draw.line([(x_left, y), (x_right, y)], fill=BG_COLOR, width=12)

    # Legs connecting basket bottom → wheels
    left_wx, right_wx = 440, 730
    wheel_y = 740
    wheel_r = 48
    for wx in [left_wx, right_wx]:
        draw.line(
            [(wx, 660), (wx, wheel_y - wheel_r + 5)],
            fill=FG_COLOR,
            width=22,
        )

    # Wheels (white ring with green center)
    for wx in [left_wx, right_wx]:
        draw.ellipse(
            [wx - wheel_r, wheel_y - wheel_r, wx + wheel_r, wheel_y + wheel_r],
            fill=FG_COLOR,
        )
        inner_r = 20
        draw.ellipse(
            [wx - inner_r, wheel_y - inner_r, wx + inner_r, wheel_y + inner_r],
            fill=BG_COLOR,
        )

    return img


def build_icns(output_path: Path):
    icon = draw_icon()

    with tempfile.TemporaryDirectory() as tmpdir:
        iconset_dir = Path(tmpdir) / "AlexaCart.iconset"
        iconset_dir.mkdir()

        for sz in [16, 32, 128, 256, 512]:
            icon.resize((sz, sz), Image.LANCZOS).save(
                iconset_dir / f"icon_{sz}x{sz}.png"
            )
            icon.resize((sz * 2, sz * 2), Image.LANCZOS).save(
                iconset_dir / f"icon_{sz}x{sz}@2x.png"
            )

        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(output_path)],
            check=True,
        )


if __name__ == "__main__":
    project_dir = Path(__file__).resolve().parent.parent
    icns_path = project_dir / "scripts" / "AlexaCart.icns"
    build_icns(icns_path)
    print(f"Generated {icns_path}")
