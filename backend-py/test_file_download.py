"""A file Caroline gives is downloadable from the chat: file_download resolves the link's path and reads it in chunks,
and the always-on instruction tells her to give the link (2026-11-24).
Run: python -m pytest test_file_download.py -q"""

import base64

from app import file_download, policies


def test_paths_from_links(tmp_path):
    f = tmp_path / "report.docx"
    f.write_bytes(b"x")
    ws = str(tmp_path)
    assert file_download.resolve_path(str(f), ws) == f
    assert file_download.resolve_path("file:///" + str(f).replace("\\", "/"), ws) is not None
    assert file_download.resolve_path("report.docx", ws) == tmp_path / "report.docx"      # relative: in her workspace
    assert file_download.resolve_path(str(tmp_path / "missing.bin"), ws) is None
    assert file_download.resolve_path(str(tmp_path), ws) is None                          # a folder is not a file
    assert file_download.resolve_path("", ws) is None


def test_chunks_put_the_file_back_together(tmp_path, monkeypatch):
    monkeypatch.setattr(file_download, "CHUNK_BYTES", 7)
    data = bytes(range(256)) * 3
    f = tmp_path / "game.fex"
    f.write_bytes(data)
    got, offset = b"", 0
    while True:
        c = file_download.read_chunk(f, offset)
        assert c["name"] == "game.fex" and c["size"] == len(data)
        part = base64.b64decode(c["dataBase64"])
        got += part
        offset += len(part)
        if c["done"]:
            break
    assert got == data


def test_the_instruction_is_always_on_and_names_the_editor_links_only_there(monkeypatch):
    assert policies.file_download_link_instruction in policies.ALWAYS_ON_INSTRUCTIONS
    monkeypatch.delenv("CAROLINE_FERRET_EDITOR", raising=False)
    plain = policies.file_download_link_instruction()
    assert "file:///" in plain and "ferret-build:" not in plain
    monkeypatch.setenv("CAROLINE_FERRET_EDITOR", "1")
    editor = policies.file_download_link_instruction()
    assert "[Game.fex](ferret-build:fex)" in editor and "project:" in editor
