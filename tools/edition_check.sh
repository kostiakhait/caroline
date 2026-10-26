#!/usr/bin/env bash
# Builds the IntelliCat edition from this tree into a scratch directory and checks it (run on Linux/WSL):
#   tools/edition_check.sh [python]      python = an interpreter with Caroline's requirements (default: python3)
# 1. the tree is copied in the dist-linux layout and tools/edition_build.py is applied
# 2. every .py compiles
# 3. no NEW undefined name appears (pyflakes, compared with the unprocessed copy)
# 4. `from app.main import app` works and none of the cut modules gets imported
set -uo pipefail
PY="${1:-python3}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${EDITION_OUT:-/tmp/caroline-edition}"
rm -rf "$OUT"; mkdir -p "$OUT/base" "$OUT/ic"
for t in base ic; do
  mkdir -p "$OUT/$t/backend-py"
  cp -r "$SRC/backend-py/app" "$OUT/$t/backend-py/app"
  cp "$SRC/backend-py/run_server.py" "$SRC/backend-py/supervisor.py" "$OUT/$t/backend-py/"
  cp -r "$SRC/Windows/Caroline/wwwroot" "$OUT/$t/wwwroot"
  find "$OUT/$t" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
  # her gitignored service-keys module: placeholders are enough to import and check
  [ -f "$OUT/$t/backend-py/app/sw_service_keys_local.py" ] || cp "$SRC/backend-py/app/sw_service_keys_example.py" "$OUT/$t/backend-py/app/sw_service_keys_local.py"
done
"$PY" "$SRC/tools/edition_build.py" "$OUT/ic" || { echo "FAIL: edition_build"; exit 1; }

echo "--- compile"
"$PY" -m compileall -q "$OUT/ic/backend-py" >"$OUT/compile.log" 2>&1 && echo ok || { cat "$OUT/compile.log" | head -20; echo "FAIL: compile"; exit 1; }

echo "--- new undefined names (pyflakes, against the unprocessed copy)"
PF="${PYFLAKES_DIR:-$HOME/.intellicat/pyflakes}"
[ -d "$PF" ] || "$PY" -m pip install -q --disable-pip-version-check --target "$PF" pyflakes >/dev/null 2>&1
flakes() { "$PY" "$SRC/tools/edition_flakes.py" "$1/backend-py" "$PF"; }
flakes "$OUT/base" >"$OUT/flakes.base"; flakes "$OUT/ic" >"$OUT/flakes.ic"
comm -13 "$OUT/flakes.base" "$OUT/flakes.ic" >"$OUT/flakes.new"
if [ -s "$OUT/flakes.new" ]; then cat "$OUT/flakes.new" | head -60; echo "FAIL: $(wc -l <"$OUT/flakes.new") new undefined names"; RC=1; else echo "none"; fi

echo "--- import"
( cd "$OUT/ic/backend-py" && PYTHONDONTWRITEBYTECODE=1 CAROLINE_WORKSPACE_DIR="$OUT/ws" "$PY" - <<'EOF'
import sys
from app.main import app, PORT
cut = ("app.engines.claude_engine", "app.engines.codex_engine", "app.engines.codex_rpc", "app.claude_ladder",
       "app.cli_control", "app.openai_account", "app.openai_mode")
bad = [m for m in cut if m in sys.modules]
print("import ok, port", PORT)
if bad:
    print("FAIL: cut modules were imported:", bad); sys.exit(1)
EOF
) 2>&1 | tail -15
echo "--- what still mentions the cut sources (lines; comments included)"
for t in claude codex openai; do printf "%-7s backend %-5s frontend %s\n" "$t" \
  "$(grep -rIi "$t" "$OUT/ic/backend-py/app" --include=*.py | wc -l)" "$(grep -rIia "$t" "$OUT/ic/wwwroot" --include=*.js --include=*.html | grep -v vendor | wc -l)"; done
exit "${RC:-0}"
