"""Tests for the default routine shown on days with no calendar events.

Like test_battery.py, this execs only the self-contained routine block from
app.py so the test runs without the full server environment.

Run with pytest, or directly: `python3 server/tests/test_routine.py`
"""
import os
from datetime import date

_APP = os.path.join(os.path.dirname(__file__), "..", "app.py")


def _load_routine():
    src = open(_APP, encoding="utf-8").read()
    start = src.index("# ─── Default Daily Routine")
    end = src.index("def build_prompt(")
    ns = {}
    exec(compile(src[start:end], "routine", "exec"), ns)
    return ns


_NS = _load_routine()
empty_day_items = _NS["empty_day_items"]

WEDNESDAY = date(2026, 10, 7)
SATURDAY = date(2026, 10, 10)
SUNDAY = date(2026, 10, 11)
NOTE = "Be curious, be kind! 🌈"


def test_weekday_uses_weekday_routine():
    assert empty_day_items(WEDNESDAY, "Homework", "Chores", NOTE) == ["• Homework", f"• {NOTE}"]


def test_weekend_uses_weekend_routine():
    for day in (SATURDAY, SUNDAY):
        assert empty_day_items(day, "Homework", "Chores", NOTE) == ["• Chores", f"• {NOTE}"]


def test_blank_routine_shows_note_alone():
    for blank in ("", "   ", None):
        assert empty_day_items(WEDNESDAY, blank, "Chores", NOTE) == [f"• {NOTE}"]
        assert empty_day_items(SATURDAY, "Homework", blank, NOTE) == [f"• {NOTE}"]


def test_functions_copy_matches_server():
    fn = os.path.join(os.path.dirname(__file__), "..", "..", "functions", "main.py")
    def block(path):
        src = open(path, encoding="utf-8").read()
        return src[src.index("# ─── Default Daily Routine"):src.index("def build_prompt(")]
    assert block(_APP) == block(fn)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
