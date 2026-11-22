"""Track importance (app/track_importance.py). Run: python -m pytest test_track_importance.py -q"""

from app import track_importance as ti

WEEK = ti.HALF_LIFE_S


def test_initial_rating_is_kept_and_decays_toward_the_floor(tmp_path):
    ws = str(tmp_path)
    assert ti.get(ws, "t1") is None
    assert ti.set_initial(ws, "t1", 0.8, now=0) == 0.8
    assert ti.set_initial(ws, "t1", 0.1, now=5) == ti.get(ws, "t1", now=5)   # a second rating does not overwrite
    week_later = ti.get(ws, "t1", now=WEEK)
    assert abs(week_later - (ti.FLOOR + (0.8 - ti.FLOOR) / 2)) < 1e-6
    assert ti.get(ws, "t1", now=100 * WEEK) < ti.FLOOR + 0.001


def test_mentions_raise_and_an_explicit_instruction_lifts_to_the_floor(tmp_path):
    ws = str(tmp_path)
    ti.set_initial(ws, "t", 0.3, now=0)
    a = ti.bump(ws, "t", explicit=False, now=0)
    b = ti.bump(ws, "t", explicit=False, now=0)
    assert 0.3 < a < b < 1.0
    assert ti.bump(ws, "t", explicit=True, now=0) >= ti.EXPLICIT_FLOOR


def test_a_mention_after_a_long_silence_starts_from_the_decayed_value(tmp_path):
    ws = str(tmp_path)
    ti.set_initial(ws, "t", 0.9, now=0)
    assert ti.bump(ws, "t", explicit=False, now=10 * WEEK) < 0.3


def test_detail_levels():
    assert ti.detail_level(0.9) == "full" and ti.detail_level(0.5) == "normal" and ti.detail_level(0.1) == "brief"
    assert ti.detail_level(None) == "normal"
