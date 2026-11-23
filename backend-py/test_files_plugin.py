"""The file tools (app/plugins/files_plugin.py; no test before 2026-11-22): a big file read only as far as shown,
a binary file named as such instead of shown as garbage, UTF-16 text, a huge folder listed in part.
Run: python -m pytest test_files_plugin.py -q"""

import asyncio

from app.plugins import files_plugin as fp


def run(coro):
    return asyncio.run(coro)


def test_a_big_text_file_is_read_only_as_far_as_shown(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "MAX_READ_BYTES", 100)
    path = tmp_path / "big.log"
    path.write_text("x" * 5000, encoding="utf-8")
    read_sizes = []
    real_open = open

    def tracking_open(file, mode="r", *a, **k):
        handle = real_open(file, mode, *a, **k)
        if "b" in mode:
            real_read = handle.read
            handle.read = lambda n=-1: read_sizes.append(n) or real_read(n)
        return handle

    monkeypatch.setattr("builtins.open", tracking_open)
    text = run(fp.read_file({"path": str(path)}, None))["text"]
    assert text.startswith("x" * 100) and "file is 5000 bytes, only the first 100 shown" in text
    assert read_sizes == [100]


def test_a_binary_file_is_named_not_shown(tmp_path):
    path = tmp_path / "tool.exe"
    path.write_bytes(b"MZ\x90\x00\x03\x00" + bytes(range(256)) * 10)
    text = run(fp.read_file({"path": str(path)}, None))["text"]
    assert "not a text file" in text and len(text) < 400


def test_utf16_and_bom_text_files_are_read(tmp_path):
    (tmp_path / "u16.txt").write_bytes("Привет, мир".encode("utf-16"))
    (tmp_path / "bom.txt").write_bytes("﻿Привет".encode("utf-8"))
    (tmp_path / "plain.txt").write_bytes(b"hello\n")
    assert run(fp.read_file({"path": str(tmp_path / "u16.txt")}, None))["text"] == "Привет, мир"
    assert run(fp.read_file({"path": str(tmp_path / "bom.txt")}, None))["text"] == "Привет"
    assert run(fp.read_file({"path": str(tmp_path / "plain.txt")}, None))["text"] == "hello\n"


def test_a_huge_folder_is_listed_in_part_folders_first(tmp_path):
    (tmp_path / "zz_dir").mkdir()
    for i in range(fp.LIST_MAX_COUNT + 5):
        (tmp_path / f"f{i:04d}.txt").write_text("a", encoding="utf-8")
    lines = run(fp.list_directory({"path": str(tmp_path)}, None))["text"].splitlines()
    assert lines[0] == "[dir] zz_dir"
    assert len(lines) == fp.LIST_MAX_COUNT + 1 and "6 more entries not shown" in lines[-1]


def test_a_small_folder_is_listed_whole(tmp_path):
    (tmp_path / "a.txt").write_text("abc", encoding="utf-8")
    assert run(fp.list_directory({"path": str(tmp_path)}, None))["text"] == "a.txt (3 bytes)"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run(fp.list_directory({"path": str(empty)}, None))["text"].endswith("is empty.")


def test_write_then_append(tmp_path):
    path = tmp_path / "sub" / "note.txt"
    run(fp.write_file({"path": str(path), "content": "one"}, None))
    run(fp.write_file({"path": str(path), "content": "two", "append": True}, None))
    assert path.read_text(encoding="utf-8") == "onetwo"
