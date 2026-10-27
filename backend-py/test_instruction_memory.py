"""Instruction memory: priority + LFU eviction, repeats, caps, rendering."""

import time

from app import instruction_memory as im


def test_add_and_render(tmp_path):
    item, evicted, repeated = im.add_instruction(str(tmp_path), "Отчитывайся строго по поручению", 5)
    assert not repeated and not evicted and item.priority == 5
    text = im.instruction_memory_system_prompt_clause(str(tmp_path))
    assert "Отчитывайся строго по поручению" in text and f"({item.id})" in text


def test_empty_renders_nothing(tmp_path):
    assert im.instruction_memory_system_prompt_clause(str(tmp_path)) == ""


def test_repeat_is_not_duplicated_and_raises_rank(tmp_path):
    a, _, _ = im.add_instruction(str(tmp_path), "Никогда не предлагай лишнего.", 2)
    b, _, repeated = im.add_instruction(str(tmp_path), "никогда не предлагай лишнего", 4)
    assert repeated and b.id == a.id and b.use_count == 2 and b.priority == 4
    assert len(im.load_instructions(str(tmp_path))) == 1


def test_eviction_is_by_priority_then_lfu_then_recency(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "MAX_ITEMS", 3)
    ws = str(tmp_path)
    hard, _, _ = im.add_instruction(ws, "hard rule", 5)
    rarely, _, _ = im.add_instruction(ws, "normal rarely used", 3)
    often, _, _ = im.add_instruction(ws, "normal often used", 3)
    for _ in range(4):
        im.mark_applied(ws, often.id)
    new, evicted, _ = im.add_instruction(ws, "another normal", 3)
    # same priority as the others: the least used of the priority-3 entries goes -- "new" has 1 use, as
    # does "rarely", and "rarely" is older, so it goes
    assert [e.id for e in evicted] == [rarely.id]
    ids = {i.id for i in im.load_instructions(ws)}
    assert ids == {hard.id, often.id, new.id}


def test_high_priority_survives_a_flood_of_low_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "MAX_ITEMS", 3)
    ws = str(tmp_path)
    hard, _, _ = im.add_instruction(ws, "hard rule", 5)
    for n in range(10):
        im.add_instruction(ws, f"mild preference {n}", 1)
        time.sleep(0.001)
    items = im.load_instructions(ws)
    assert len(items) == 3 and hard.id in {i.id for i in items}


def test_new_low_entry_into_full_higher_store_is_reported_as_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "MAX_ITEMS", 2)
    ws = str(tmp_path)
    im.add_instruction(ws, "one", 5)
    im.add_instruction(ws, "two", 5)
    item, evicted, _ = im.add_instruction(ws, "three", 1)
    assert [e.id for e in evicted] == [item.id]
    assert len(im.load_instructions(ws)) == 2


def test_char_budget_evicts_lowest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "MAX_TOTAL_CHARS", 400)
    ws = str(tmp_path)
    keep, _, _ = im.add_instruction(ws, "k" * 100, 5)
    for n in range(6):
        im.add_instruction(ws, f"{n}" + "x" * 100, 2)
    assert len("\n".join(im._render_lines(im.load_instructions(ws)))) <= 400
    assert keep.id in {i.id for i in im.load_instructions(ws)}


def test_forget_and_applied(tmp_path):
    ws = str(tmp_path)
    item, _, _ = im.add_instruction(ws, "rule")
    assert im.mark_applied(ws, item.id).use_count == 2
    assert im.mark_applied(ws, "nope") is None
    assert im.forget_instruction(ws, item.id) and not im.forget_instruction(ws, item.id)


def test_text_is_capped_and_empty_rejected(tmp_path):
    item, _, _ = im.add_instruction(str(tmp_path), "z" * 5000)
    assert len(item.text) == im.MAX_ITEM_CHARS
    try:
        im.add_instruction(str(tmp_path), "   ")
        assert False
    except ValueError:
        pass
