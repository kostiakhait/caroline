"""The four built-in Visual Mode models come in the background at every start, whether or not Visual Mode is on
(app/visual_mode.py BackgroundDownloads), and arriving files never switch the feature on by themselves.
Run: python -m pytest test_visual_mode_background.py -q"""

import json

import pytest

from app import visual_mode as vm


@pytest.fixture
def ws(tmp_path, monkeypatch):
    models = tmp_path / "models"          # does not exist yet, as on a fresh install
    monkeypatch.setenv("CAROLINE_MODELS_DIR", str(models))
    monkeypatch.setattr(vm, "_builtin_names_for_profile", lambda _ws: ["CarolineA", "CarolineB"])
    vm.background_downloads.queue, vm.background_downloads.current = [], None
    vm.background_downloads.skip = set()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return str(workspace), models


def test_models_dir_is_the_env_folder_even_before_it_exists(ws):
    _, models = ws
    assert vm.models_dir() == str(models)


def test_all_four_come_persona_first(ws):
    workspace, _ = ws
    assert vm.missing_background_models(workspace) == ["CarolineA", "CarolineB", "PeterA", "PeterB"]


def test_what_is_on_disk_is_skipped(ws):
    workspace, models = ws
    models.mkdir()
    (models / "CarolineA.xcfa").write_bytes(b"x")
    (models / "PeterB.xcfa").write_bytes(b"x")
    assert vm.missing_background_models(workspace) == ["CarolineB", "PeterA"]


def test_one_at_a_time_each_finished_file_starts_the_next(ws):
    workspace, _ = ws
    bg = vm.BackgroundDownloads()
    assert bg.begin(workspace, set()) == "CarolineA"
    assert bg.done("CarolineA", True) == "CarolineB"
    assert bg.done("CarolineB", True) == "PeterA"
    assert bg.done("PeterA", True) == "PeterB"
    assert bg.done("PeterB", True) is None


def test_a_failure_stops_the_chain_until_the_next_start(ws):
    workspace, _ = ws
    bg = vm.BackgroundDownloads()
    bg.begin(workspace, set())
    assert bg.done("CarolineA", False) is None
    assert bg.queue == []
    assert bg.begin(workspace, set()) == "CarolineA"      # the next start tries again


def test_files_the_toggle_started_are_not_started_twice(ws):
    workspace, _ = ws
    bg = vm.BackgroundDownloads()
    assert bg.begin(workspace, {"CarolineA", "CarolineB"}) == "PeterA"
    bg2 = vm.BackgroundDownloads()
    bg2.begin(workspace, set())
    bg2.taken_over("CarolineB")
    assert bg2.queue == ["PeterA", "PeterB"]


def test_a_purchased_model_does_not_move_the_chain(ws):
    workspace, _ = ws
    bg = vm.BackgroundDownloads()
    bg.begin(workspace, set())
    assert bg.done("Alice", True) is None
    assert bg.current == "CarolineA"


def test_nothing_missing_nothing_started(ws):
    workspace, models = ws
    models.mkdir()
    for n in ("CarolineA", "CarolineB", "PeterA", "PeterB"):
        (models / f"{n}.xcfa").write_bytes(b"x")
    assert vm.BackgroundDownloads().begin(workspace, set()) is None


def test_the_message_is_the_one_the_host_downloads_from():
    m = vm.builtin_download_message("PeterA")
    assert m["type"] == "visual_model_download_start" and m["modelName"] == "PeterA"
    assert m["url"].endswith("/models/PeterA.xcfa") and m["shaUrl"].endswith("/PeterA.xcfa.sha256")


def test_enabling_is_a_separate_wish_that_a_download_never_sets(ws):
    workspace, _ = ws
    assert not vm.is_enable_when_ready(workspace) and not vm.is_visual_mode_enabled(workspace)
    vm.set_enable_when_ready(workspace, True)
    assert vm.is_enable_when_ready(workspace) and not vm.is_visual_mode_enabled(workspace)
    vm.set_visual_mode_enabled(workspace, True)           # the toggle's own completion
    assert vm.is_visual_mode_enabled(workspace) and not vm.is_enable_when_ready(workspace)
    vm.set_enable_when_ready(workspace, True)
    vm.set_visual_mode_enabled(workspace, False)          # switching it off cancels the wish
    assert not vm.is_enable_when_ready(workspace)
    assert json.loads((vm._settings_path(workspace)).read_text())["enabled"] is False
