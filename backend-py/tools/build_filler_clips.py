"""Renders the filler remarks (app/filler_phrases.py, the shipped audio in app/assets/fillers) into talking-head
clips ONCE per animation model, and packs them for the download server (2026-10-24, explicit instruction: save the
clips once for all four models and download them from the server, do not render them on every machine).

For each model (CarolineA, CarolineB, PeterA, PeterB) the renderer bridge is started with XCFA_CLIP_CACHE pointing at
a fresh folder and asked to prewarm every filler mp3 (ru and en, female and male voices); its cache then holds the
clips under keys made of the audio, the model's NAME and size, and the picture height -- never a path -- so a pack
unpacked into any machine's clip cache (the folder XCFA_CLIP_CACHE names, by default <temp>/xcfa-clip-cache on Linux,
<temp>/CarolineVisualModeClips on Windows) is found by the same lookups. The rest pose (rest-<key>.png) is in the
pack too. Output: <out>/<ModelName>-clips.tar, one per model.

    python tools/build_filler_clips.py --bridge <XcfaBridge.dll> --models <dir with *.xcfa> --out <dir>
                                       [--only CarolineB] [--height 480]
Needs `dotnet` and ffmpeg on the machine, and the .xcfa files. Takes ~1-2 s of CPU per clip."""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

MODELS = ("CarolineA", "CarolineB", "PeterA", "PeterB")


def build_one(bridge: str, model_path: Path, fillers: list[str], cache_dir: Path, height: int, ffmpeg: str) -> tuple[int, float]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, XCFA_CLIP_CACHE=str(cache_dir))
    proc = subprocess.Popen(["dotnet", bridge], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)

    def send(payload: dict) -> None:
        assert proc.stdin
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    started = time.monotonic()
    send({"cmd": "configure", "modelPath": str(model_path), "ffmpeg": ffmpeg, "height": height})
    assert proc.stdout
    ready = json.loads(proc.stdout.readline())
    print(f"  {model_path.name}: model warm in {ready.get('prepMs')} ms", flush=True)
    send({"cmd": "prewarm", "audio": fillers})
    for line in proc.stdout:
        event = json.loads(line)
        if event.get("event") == "prewarmed":
            print(f"  {model_path.name}: {event.get('count')} rendered, {event.get('left')} left", flush=True)
            if event.get("left", 1) == 0:
                break
        elif event.get("event") == "error":
            print("  bridge error:", event.get("message"), flush=True)
    send({"cmd": "quit"})
    proc.wait(timeout=60)
    clips = len(list(cache_dir.glob("*.webm")))
    return clips, time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", required=True, help="path of XcfaBridge.dll")
    parser.add_argument("--models", required=True, help="folder with CarolineA.xcfa ... PeterB.xcfa")
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", help="a single model name")
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent / "app" / "assets" / "fillers"
    fillers = sorted(glob.glob(str(root / "*" / "*" / "*.mp3")))
    if not fillers:
        print("no filler audio under", root)
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name in ([args.only] if args.only else MODELS):
        model = Path(args.models) / f"{name}.xcfa"
        if not model.exists():
            print(f"{name}: {model} is missing, skipped")
            continue
        print(f"{name}: {len(fillers)} filler audio files", flush=True)
        cache = out / f"{name}-cache"
        clips, seconds = build_one(args.bridge, model, fillers, cache, args.height, args.ffmpeg)
        tar_path = out / f"{name}-clips.tar"
        with tarfile.open(tar_path, "w") as tar:
            for item in sorted(cache.iterdir()):
                if item.suffix in (".webm", ".png"):
                    tar.add(item, arcname=item.name)
        print(f"{name}: {clips} clips in {seconds:.0f} s -> {tar_path} ({tar_path.stat().st_size / 1048576:.1f} MB)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
