"""Tests for character casting — who gets drawn into a given day's scene.

app.py imports FastAPI, Pillow and friends at module scope, so importing it
just to reach two pure functions would make this test require the full server
environment. Instead we exec only the self-contained casting block, which
depends on nothing but `re`.

Run with pytest, or directly: `python3 server/tests/test_casting.py`
"""
import os
import re

_APP = os.path.join(os.path.dirname(__file__), "..", "app.py")


def _load_casting():
    src = open(_APP, encoding="utf-8").read()
    start = src.index("# ─── Character Casting")
    end = src.index("def build_prompt(")
    ns = {"re": re, "print": print}
    exec(compile(src[start:end], "casting", "exec"), ns)
    return ns


_NS = _load_casting()
resolve_scene_characters = _NS["resolve_scene_characters"]
describe_character = _NS["_describe_character"]


TAVI = {"id": "1", "name": "Tavi", "type": "kid", "age": 7, "gender": "male",
        "inclusion": "always", "description": "Blonde hair.", "imageUrl": "t.png"}
STEVE = {"id": "2", "name": "Steve", "type": "kid", "age": 8, "gender": "male",
         "inclusion": "when_mentioned", "description": "Red cap.", "imageUrl": "s.png"}
SARAH = {"id": "3", "name": "Sarah", "type": "kid", "age": 38, "gender": "female",
         "inclusion": "when_mentioned", "aliases": ["mom", "mum"], "description": ""}
AL = {"id": "4", "name": "Al", "type": "kid", "inclusion": "when_mentioned"}
REX = {"id": "5", "name": "Rex", "type": "extra", "inclusion": "when_mentioned",
       "description": "Scruffy terrier."}
NAN = {"id": "6", "name": "Nan", "type": "extra"}  # predates the inclusion field

LIBRARY = [TAVI, STEVE, SARAH, AL, REX]


def cast_names(events, characters=LIBRARY, **kwargs):
    cast, _ = resolve_scene_characters(events, characters, **kwargs)
    return [c["name"] for c in cast]


def test_quiet_day_draws_only_regulars():
    assert cast_names([{"summary": "Dentist"}]) == ["Tavi"]


def test_named_character_joins_the_scene():
    assert cast_names([{"summary": "Playdate with Steve"}]) == ["Tavi", "Steve"]


def test_guest_carries_the_event_that_cast_them():
    _, reasons = resolve_scene_characters(
        [{"summary": "Playdate with Steve"}], LIBRARY)
    assert reasons["2"] == ["Playdate with Steve"]
    assert "1" not in reasons  # regulars need no reason


def test_alias_resolves_to_the_character():
    assert cast_names([{"summary": "Trip with mom"}]) == ["Tavi", "Sarah"]


def test_matching_is_case_insensitive():
    assert cast_names([{"summary": "TRIP WITH MUM"}]) == ["Tavi", "Sarah"]


def test_names_do_not_match_inside_longer_words():
    # "Al" must not be found inside "Always"
    assert cast_names([{"summary": "Always pack a lunch"}]) == ["Tavi"]


def test_location_and_description_are_searched():
    assert cast_names([{"summary": "Walk", "location": "Steve's house"}]) == ["Tavi", "Steve"]
    assert cast_names([{"summary": "Walk", "description": "bring Rex"}]) == ["Tavi", "Rex"]


def test_multiple_events_cast_multiple_guests():
    assert cast_names([
        {"summary": "Playdate with Steve"},
        {"summary": "Trip with mom"},
    ]) == ["Tavi", "Steve", "Sarah"]


def test_records_without_inclusion_still_appear_every_day():
    assert cast_names([{"summary": "Nothing relevant"}], [TAVI, NAN]) == ["Tavi", "Nan"]


def test_regulars_are_never_dropped_for_the_cap():
    regulars = [dict(TAVI, id=str(i), name=f"Kid{i}") for i in range(6)]
    got = cast_names([{"summary": "Playdate with Steve"}], regulars + [STEVE])
    assert got == [f"Kid{i}" for i in range(6)]


def test_guests_are_trimmed_to_the_cap():
    cast, reasons = resolve_scene_characters(
        [{"summary": "Steve and Rex and mom"}],
        [TAVI, STEVE, SARAH, REX],
        max_characters=3,
    )
    assert [c["name"] for c in cast] == ["Tavi", "Steve", "Sarah"]
    assert "5" not in reasons  # the trimmed guest's reason goes with them


def test_prompt_lines_explain_why_a_guest_is_there():
    cast, reasons = resolve_scene_characters(
        [{"summary": "Playdate with Steve"}], LIBRARY)
    lines = [describe_character(c, i + 1, reasons) for i, c in enumerate(cast)]
    assert lines[0] == "1) A boy named Tavi, age 7. Blonde hair."
    assert lines[1].endswith("— here today for: Playdate with Steve")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{'all tests passed' if not failures else f'{failures} failed'}")
    raise SystemExit(1 if failures else 0)
