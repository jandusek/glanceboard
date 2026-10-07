"""Tests for the battery line burned into the display image.

Like test_casting.py, this execs only the self-contained battery block from
app.py so the test needs Pillow but not the full server environment.

Run with pytest, or directly: `python3 server/tests/test_battery.py`
"""
import os

from PIL import Image

_APP = os.path.join(os.path.dirname(__file__), "..", "app.py")


def _load_battery():
    src = open(_APP, encoding="utf-8").read()
    start = src.index("# ─── Battery Indicator")
    end = src.index("# ─── Helper: Run pipeline for a single user")
    ns = {}
    exec(compile(src[start:end], "battery", "exec"), ns)
    return ns


_NS = _load_battery()
parse_battery_percentage = _NS["parse_battery_percentage"]
draw_battery_line = _NS["draw_battery_line"]

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
W, H = 800, 480


def blank():
    return Image.new("RGB", (W, H), WHITE)


def inked_rows(img):
    """Rows where the right edge pixel is black."""
    return [y for y in range(H) if img.getpixel((W - 1, y)) == BLACK]


def test_full_battery_reaches_the_top():
    rows = inked_rows(draw_battery_line(blank(), 100))
    assert rows[0] <= 1
    assert rows[-1] == H - 1


def test_half_battery_stays_in_the_bottom_half():
    rows = inked_rows(draw_battery_line(blank(), 50))
    assert rows[-1] == H - 1
    assert rows[0] >= H // 2


def test_empty_battery_draws_nothing():
    assert inked_rows(draw_battery_line(blank(), 0)) == []


def test_line_is_solid():
    assert inked_rows(draw_battery_line(blank(), 100)) == list(range(H))
    assert len(inked_rows(draw_battery_line(blank(), 25))) == H // 4


def test_line_is_one_pixel_wide():
    img = draw_battery_line(blank(), 100)
    assert img.getpixel((W - 1, H - 1)) == BLACK
    assert img.getpixel((W - 2, H - 1)) == WHITE


def test_header_parsing():
    assert parse_battery_percentage("87") == 87
    assert parse_battery_percentage(" 0 ") == 0
    assert parse_battery_percentage("100") == 100
    assert parse_battery_percentage(None) is None
    assert parse_battery_percentage("") is None
    assert parse_battery_percentage("-1") is None
    assert parse_battery_percentage("101") is None
    assert parse_battery_percentage("abc") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
