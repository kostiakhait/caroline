# --- Caroline build/deploy Makefile -----------------------------------------
#
# Replaces build.bat, build_installer.bat. Real file-based prerequisites
# throughout, not "rebuild everything every time" phony targets -- `make
# build`/`make installer` only redo the steps whose actual inputs changed
# since the last run, the same way a C/C++ Makefile would. Recipes run under
# Git Bash (see SHELL below), not cmd.exe.
#
# Prerequisites on the BUILD machine: .NET 8 SDK, Node.js, npm, Python (for
# zip packing -- see the zip recipe's own note), Git Bash, and GNU Make
# itself (authored against GnuWin32 Make 3.81 -- no GNU Make 4.x-only syntax
# used).
#
# Common invocations:
#   make build          app itself only, no packaging
#   make installer       + Caroline.zip/.sha256/.version + CarolineInstaller.exe
#   make clean            removes dist/ and dist_installer/ entirely (forces
#                          a full rebuild next time -- normally unnecessary,
#                          Make's own dependency tracking handles this)
#
# `make deploy`/`make deploy-models` (uploading a release to the maintainer's
# own server) are NOT part of this public Makefile -- see deploy.mk (gitignored,
# maintainer-machine-only; -include'd below when present, a no-op otherwise).

SHELL := C:/Program Files/Git/bin/bash.exe
.SHELLFLAGS := -ec

SCRIPT_DIR := $(CURDIR)
# Where the sources are read from and where the outputs go (2026-11-22). A plain `make build`
# reads the working tree and writes dist/ etc. next to it, as always. A release (deploy.mk)
# builds from HEAD alone: tools/stage_source.py writes the committed sources into
# .build/src, and the release outputs live in .build/ -- never mixed with a dev build's stamps,
# never carrying anyone's uncommitted edits.
SRC ?= $(SCRIPT_DIR)
BUILD_ROOT ?= $(SCRIPT_DIR)
BACKEND_PY_DIR := $(SRC)/backend-py
# The bundled Python (gitignored, never staged) is only ever read from the real checkout.
PYTHON_RUNTIME_DIR := $(SCRIPT_DIR)/backend-py/python-runtime

# Any local clone of the `reforce` repo that has the `caroline` branch reachable
# (as a local branch or via a remote, e.g. origin/caroline) -- pulled via `git show
# caroline:<path>`, not a plain file copy, so this works regardless of what that
# clone's OWN working tree currently has checked out. Override on the command line
# (`make build CAMERLENGO_REPO=...`) for a non-default sibling layout.
CAMERLENGO_REPO ?= $(SCRIPT_DIR)/../reforce
CAMERLENGO_VENDOR_STUBS := $(BACKEND_PY_DIR)/camerlengo-vendor-stubs
CAROLINE_DIR := $(SRC)/Windows/Caroline
INSTALLER_DIR := $(SRC)/Windows/CarolineInstaller
XCFA_DIR := $(SRC)/vendor/XcfaRenderer
PROJECT := $(CAROLINE_DIR)/Caroline.csproj
INSTALLER_PROJECT := $(INSTALLER_DIR)/CarolineInstaller.csproj
OUT := $(BUILD_ROOT)/dist
INSTALLER_OUT := $(BUILD_ROOT)/dist_installer

# --- Real source-file dependencies, so a target only rebuilds when something
# it actually reads has changed -- not on every invocation. bin/obj are each
# project's own build output, excluded so they don't create a dependency on
# artifacts a previous build produced (which would make every target look
# perpetually "newer than its inputs").
# Each variable is exactly ONE `find` process (not one per -o'd pattern, and
# not one per directory) -- GnuWin32 Make 3.81's $(shell) confirmed live to
# fail intermittently ("process_begin: CreateProcess(NULL, "", ...) failed")
# once too many separate $(shell) subprocess spawns land in the same
# Makefile parse pass. Four total here stays well clear of that.
SKILLS_SRC := $(shell find "$(BACKEND_PY_DIR)/skills-src" -type f 2>/dev/null)
BACKEND_PY_SRC := $(shell find "$(BACKEND_PY_DIR)/app" -type f -name '*.py' 2>/dev/null) $(shell find "$(BACKEND_PY_DIR)/app/assets" -type f -name '*.html' 2>/dev/null) $(BACKEND_PY_DIR)/run_server.py $(BACKEND_PY_DIR)/supervisor.py $(BACKEND_PY_DIR)/VERSION
CAROLINE_SRC := $(shell find "$(CAROLINE_DIR)" -type f -not -path '*/bin/*' -not -path '*/obj/*' \( -name '*.cs' -o -name '*.xaml' -o -name '*.csproj' -o -path '*/wwwroot/*' \) 2>/dev/null)
INSTALLER_SRC := $(shell find "$(INSTALLER_DIR)" -type f -not -path '*/bin/*' -not -path '*/obj/*' \( -name '*.cs' -o -name '*.csproj' -o -path '*/Assets/*' \) 2>/dev/null)
XCFA_SRC := $(shell find "$(XCFA_DIR)" -type f -not -path '*/bin/*' -not -path '*/obj/*' -not -path '*/XcfaRenderer.Tests/*' -not -path '*/Demo/*' \( -name '*.cs' -o -name '*.csproj' \) 2>/dev/null)

LINUX_SHELL_DIR := $(SRC)/linux-shell
LINUX_SHELL_SRC := $(shell find "$(LINUX_SHELL_DIR)" -type f 2>/dev/null)
WWWROOT_DIR := $(CAROLINE_DIR)/wwwroot
OUT_LINUX := $(BUILD_ROOT)/dist-linux
OUT_INTELLICAT := $(BUILD_ROOT)/dist-intellicat
PYTHON ?= python
# the maintainer-only deploy rules (paths of that machine); a build on another host passes DEPLOY_MK=/dev/null
DEPLOY_MK ?= deploy.mk

.PHONY: help build installer clean dist-linux dist-intellicat appimage-linux

help:
	@echo "Targets: build, installer, clean, dist-linux, appimage-linux"
	@echo "(deploy, deploy-models: available only when deploy.mk is present -- see its header comment)"

# --- 1-3. (gone, 2026-10-24) The old Node/TypeScript backend (backend/: its
# npm install, tsc build, node_modules and MCP server bundle) is no longer built
# or shipped -- backend-py is the only backend, and every tool, desktop automation
# included (app/plugins/_win32*.py, _x11_*.py, _linux_chain.py), lives in it. The
# skills it used to carry are backend-py/skills-src now.

# --- 4. WPF shell publish + backend sidecar copy (was build.bat) -----------
#
# Produces a self-contained, portable install folder under dist/ -- entirely
# from this repo's own sources, no reference to any other repo at runtime.
#
# Split into independent stamp targets (2026-10-12, per explicit instruction),
# replacing one monolithic $(OUT)/Caroline.exe recipe that rebuilt EVERYTHING
# (both dotnet publishes, every copy step) whenever ANY of its prerequisites
# changed -- confirmed live: a one-line backend-py edit cost the same full
# rebuild as a real C# change, every time. Each stamp below depends only on
# the inputs it actually reads, so e.g. a Python-only change now skips both
# dotnet publishes entirely. $(OUT) itself is never wiped wholesale anymore
# (that line used to double as "clean up anything removed since last build"
# for the WHOLE tree in one shot) -- each stamp's own recipe now does that
# narrower cleanup for just its own subtree before recopying, same effect,
# scoped. `make clean` (full wipe) still exists for a genuine from-scratch
# rebuild. touch -m advances each stamp's mtime past the FILES it just wrote
# (a bare `touch "$@"` on a stamp inside the folder its own recipe just
# recreated could otherwise race a slow filesystem's own mtime granularity).
#
# NOTE: -p:IncludeNativeLibrariesForSelfExtract=true is deliberately NOT
# passed here -- confirmed live it silently drops wwwroot/assets/** (the
# persona reference photos) from the publish output entirely, with no
# warning or error. Isolated by testing each publish flag individually:
# -r win-x64 --self-contained true -p:PublishSingleFile=true alone still
# correctly includes wwwroot/assets; adding IncludeNativeLibrariesForSelfExtract
# is what breaks it. Without it, native DLLs (e.g. WebView2Loader.dll) just
# publish as loose files next to Caroline.exe instead of being bundled into
# the single-file exe for self-extraction -- harmless, still a working
# self-contained single-file publish.
$(OUT)/.dotnet.stamp: $(CAROLINE_SRC) $(XCFA_SRC)
	@echo "=== Caroline Build ==="
	mkdir -p "$(OUT)"
	# Bug fix (2026-10-12), confirmed live: dropping the old recipe's blanket
	# `rm -rf $(OUT)` (replaced by each stamp's own narrower cleanup) left
	# THIS stamp with none at all -- confirmed live, a `dotnet publish` reusing
	# a not-freshly-cleaned output directory across separate invocations over
	# time silently dropped loose native dependency DLLs (PresentationNative_
	# cor3.dll, D3DCompiler_47_cor3.dll, WebView2Loader.dll) that a fully clean
	# `make clean && make build` always produced correctly -- the app then
	# crashed on startup with System.DllNotFoundException. Clears everything
	# dotnet publish itself owns at $(OUT)'s top level, WITHOUT touching the
	# subtrees/stamps the other targets below own (backend/, backend-py/,
	# every *.stamp file) -- same "narrower cleanup, same
	# effect, scoped" principle already applied to those, just missed here.
	find "$(OUT)" -mindepth 1 -maxdepth 1 ! -name backend-py ! -name browsers ! -name "*.stamp" -exec rm -rf {} +
	dotnet publish "$(PROJECT)" -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -o "$(OUT)"
	touch -m "$@"

# Vendored ONCE (2026-10-20, per explicit instruction: "Отказать. Скопировать
# один раз и перестать.") from CAMERLENGO_REPO@caroline straight into
# $(CAMERLENGO_VENDOR_STUBS) as real, git-tracked files -- NOT pulled live on
# every build anymore. The previous design (`git show caroline:...` on every
# `make deploy`/`make build`) deliberately never went stale, but the cost of
# that was real: camerlengo-vendor was .PHONY, so Make always treated
# Caroline.exe's build as stale too, forcing a full dotnet publish (+ full
# Caroline.zip re-zip on deploy) even when NOTHING had actually changed,
# C# or camerlengo-side. Now a real file-dependency rule: Make only
# redoes this (a handful of cheap local copies, no git call at all) when one
# of the actual stub files changed, and Caroline.exe's build only reruns
# when THIS step's own output changed. Picking up a genuine reforce-side
# change again requires manually re-running the same `git show caroline:<f>
# > $(CAMERLENGO_VENDOR_STUBS)/<f>` commands by hand (see git history for
# the exact file list) -- a deliberate, infrequent, explicit step now,
# not an automatic one.
$(OUT)/backend-py/camerlengo/.stamp: $(CAMERLENGO_VENDOR_STUBS)/AI.py $(CAMERLENGO_VENDOR_STUBS)/Cache.py $(CAMERLENGO_VENDOR_STUBS)/Logger.py $(CAMERLENGO_VENDOR_STUBS)/JSONStorage.py $(CAMERLENGO_VENDOR_STUBS)/Microagent.py $(CAMERLENGO_VENDOR_STUBS)/Config.py
	@echo "--- camerlengo (small-model primary path, see small_model_engine.py's own doc comment) ---"
	@mkdir -p "$(OUT)/backend-py/camerlengo"
	cp $^ "$(OUT)/backend-py/camerlengo/"
	@touch "$@"

# --- backend-py (the live backend -- see Native/SupervisorClient.cs) -------
$(OUT)/.backend-py.stamp: $(BACKEND_PY_SRC) $(SKILLS_SRC)
	@echo "--- backend-py (the live backend -- see Native/SupervisorClient.cs) ---"
	rm -rf "$(OUT)/backend" "$(OUT)/backend-py/python-scripts"
	rm -rf "$(OUT)/backend-py/app"
	mkdir -p "$(OUT)/backend-py"
	cp -r "$(BACKEND_PY_DIR)/app" "$(OUT)/backend-py/app"
	cp "$(BACKEND_PY_DIR)/run_server.py" "$(OUT)/backend-py/run_server.py"
	cp "$(BACKEND_PY_DIR)/supervisor.py" "$(OUT)/backend-py/supervisor.py"
	cp "$(BACKEND_PY_DIR)/VERSION" "$(OUT)/backend-py/VERSION"
	find "$(OUT)/backend-py/app" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf "$(OUT)/backend-py/skills-src"
	cp -r "$(BACKEND_PY_DIR)/skills-src" "$(OUT)/backend-py/skills-src"
	touch -m "$@"

# Caroline's own web-search browser (app/web_browser.py, decided 2026-10-23: "вшить
# Chromium в установщик"): the FULL Chromium of the revision the bundled Playwright
# expects (run in its "new" headless mode -- the separate headless shell is turned away by
# search engines, seen live 2026-10-24), installed into dist/browsers -- shipped inside Caroline.zip and launched by its
# explicit path, so nothing depends on the user's own Playwright caches. Redone only when
# the Playwright version (its browsers.json) changes.
PLAYWRIGHT_BROWSERS_JSON := $(PYTHON_RUNTIME_DIR)/Lib/site-packages/playwright/driver/package/browsers.json
$(OUT)/.browsers.stamp: $(PLAYWRIGHT_BROWSERS_JSON)
	@echo "--- browsers (headless Chromium for web search -- see app/web_browser.py) ---"
	rm -rf "$(OUT)/browsers"
	mkdir -p "$(OUT)/browsers"
	PLAYWRIGHT_BROWSERS_PATH="$(OUT)/browsers" "$(PYTHON_RUNTIME_DIR)/python.exe" -m playwright install --no-shell chromium
	ls "$(OUT)"/browsers/chromium-*/chrome-win64/chrome.exe
	touch -m "$@"

$(OUT)/Caroline.exe: $(OUT)/.dotnet.stamp $(OUT)/.browsers.stamp $(OUT)/backend-py/camerlengo/.stamp $(OUT)/.backend-py.stamp
	@echo "Build complete: $(OUT)/Caroline.exe"
	@touch -m "$@"

build: $(OUT)/Caroline.exe

# --- Linux shell package (no dotnet publish at all -- linux-shell/ is plain
# Python; see docs/LINUX_PORT_PLAN.md's Phase 5 for why the shell itself
# can't be bundled into a self-contained runtime the way the backend's
# runtime/python is: python3-gi/gir1.2-webkit2-4.1 are system packages tied
# to the installed GTK3/WebKitGTK versions). This recipe only assembles the
# directory tree an AppImage step (not yet built) would wrap; it's
# deliberately runnable from this Windows build machine too, since every
# step here is a plain file copy, nothing Linux-specific about the build
# itself. wwwroot is copied as a SIBLING of linux-shell/ (not left pointing
# back into Windows/Caroline/), matching caroline_shell.py's own
# _default_wwwroot() packaged-install lookup.
$(OUT_LINUX)/linux-shell/caroline_shell.py: $(LINUX_SHELL_SRC) $(filter %/wwwroot/%,$(CAROLINE_SRC)) $(wildcard $(INSTALLER_DIR)/Assets/Banners/*.png) $(BACKEND_PY_SRC) $(SKILLS_SRC) $(CAMERLENGO_VENDOR_STUBS)/AI.py $(CAMERLENGO_VENDOR_STUBS)/Cache.py $(CAMERLENGO_VENDOR_STUBS)/Logger.py $(CAMERLENGO_VENDOR_STUBS)/JSONStorage.py $(CAMERLENGO_VENDOR_STUBS)/Microagent.py $(CAMERLENGO_VENDOR_STUBS)/Config.py
	@echo "=== Caroline Linux shell package ==="
	rm -rf "$(OUT_LINUX)"
	mkdir -p "$(OUT_LINUX)/linux-shell"
	cp -r "$(LINUX_SHELL_DIR)/." "$(OUT_LINUX)/linux-shell/"
	@echo "--- wwwroot (chat UI assets, shared with the Windows build) ---"
	cp -r "$(WWWROOT_DIR)" "$(OUT_LINUX)/wwwroot"
	@echo "--- splash banners (same onboarding set the Windows build copies) ---"
	mkdir -p "$(OUT_LINUX)/SplashBanners"
	cp "$(INSTALLER_DIR)/Assets/Banners/"*.png "$(OUT_LINUX)/SplashBanners/"
	@echo "--- backend-py ---"
	mkdir -p "$(OUT_LINUX)/backend-py"
	cp -r "$(BACKEND_PY_DIR)/app" "$(OUT_LINUX)/backend-py/app"
	cp "$(BACKEND_PY_DIR)/run_server.py" "$(OUT_LINUX)/backend-py/run_server.py"
	cp "$(BACKEND_PY_DIR)/supervisor.py" "$(OUT_LINUX)/backend-py/supervisor.py"
	cp "$(BACKEND_PY_DIR)/VERSION" "$(OUT_LINUX)/backend-py/VERSION"
	cp "$(BACKEND_PY_DIR)/requirements-linux.txt" "$(OUT_LINUX)/backend-py/requirements-linux.txt"
	cp -r "$(BACKEND_PY_DIR)/skills-src" "$(OUT_LINUX)/backend-py/skills-src"
	find "$(OUT_LINUX)/backend-py/app" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find "$(OUT_LINUX)/linux-shell" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "--- camerlengo (small-model primary path, vendored once -- see the Windows build's own stamp rule for why) ---"
	mkdir -p "$(OUT_LINUX)/backend-py/camerlengo"
	cp "$(CAMERLENGO_VENDOR_STUBS)/AI.py" "$(CAMERLENGO_VENDOR_STUBS)/Cache.py" "$(CAMERLENGO_VENDOR_STUBS)/Logger.py" "$(CAMERLENGO_VENDOR_STUBS)/JSONStorage.py" "$(CAMERLENGO_VENDOR_STUBS)/Microagent.py" "$(CAMERLENGO_VENDOR_STUBS)/Config.py" "$(OUT_LINUX)/backend-py/camerlengo/"
	@echo "Linux shell package complete: $(OUT_LINUX)"
	@echo "NOTE: runtime/python, runtime/codex and runtime/signal-cli are NOT produced by this recipe (it's plain"
	@echo "      file copying, cross-platform by design). Run on a Linux host:"
	@echo "      linux-shell/provision_runtime.sh $(OUT_LINUX)"

dist-linux: $(OUT_LINUX)/linux-shell/caroline_shell.py

# The IntelliCat edition (SquirrelWisdom only): the dist-linux tree with everything that serves the Claude and
# OpenAI answer sources removed -- files listed in tools/edition_cut_files.txt, and the regions marked
# "edition:cut" in the sources (tools/edition_build.py explains the markers). The default build is not affected.
# A real file target (2026-11-22): redone only when the Linux tree or the edition's cut rules changed.
$(OUT_INTELLICAT)/EDITION: $(OUT_LINUX)/linux-shell/caroline_shell.py $(SRC)/tools/edition_build.py $(SRC)/tools/edition_cut_files.txt
	@echo "=== Caroline, IntelliCat edition (SquirrelWisdom only) ==="
	rm -rf "$(OUT_INTELLICAT)"
	cp -r "$(OUT_LINUX)" "$(OUT_INTELLICAT)"
	"$(PYTHON)" "$(SRC)/tools/edition_build.py" "$(OUT_INTELLICAT)"
	echo intellicat > "$(OUT_INTELLICAT)/EDITION"
	@echo "IntelliCat edition complete: $(OUT_INTELLICAT)"

dist-intellicat: $(OUT_INTELLICAT)/EDITION

# AppImage wrapping (Phase 5 of docs/LINUX_PORT_PLAN.md) -- same "can't run
# the real tool from Windows" shape as provision_runtime.sh above: appimagetool
# is a Linux ELF binary, so this target only documents the two commands a
# Linux host actually runs, same as dist-linux's own NOTE does for
# provision_runtime.sh. Produces an AppImage that still depends on the HOST
# having python3-gi/gir1.2-webkit2-4.1/GTK3/WebKitGTK installed (see
# linux-shell/build_appimage.sh's own header comment for why a fully
# self-contained one -- bundling those shared libraries too -- is a
# separate, bigger, not-yet-attempted effort).
appimage-linux: dist-linux
	@echo "AppImage wrapping can't run on this build machine (appimagetool is a Linux ELF binary)."
	@echo "On a Linux host, after this dist-linux output has runtime/python + runtime/codex:"
	@echo "      linux-shell/provision_runtime.sh $(OUT_LINUX)"
	@echo "      linux-shell/build_appimage.sh $(OUT_LINUX) $(OUT_LINUX)/../Caroline-x86_64.AppImage"

# --- 5. Zip + hash + version stamp (was build_installer.bat, steps 1-2) ----
#
# PowerShell's Compress-Archive AND a plain System.IO.Compression call run
# through -Command both crawled/hung packing dist/ on this machine (Optimal
# compression through a PowerShell host, likely fighting antivirus real-time
# scanning of the process). Python's zipfile module, called directly with no
# PowerShell host in between, doesn't have that problem.
#
# VERSION defaults to the current timestamp, computed fresh every time this
# recipe actually RUNS (not at parse time) -- override with `make installer
# VERSION=...` for a specific stamp instead.
$(INSTALLER_OUT)/Caroline.zip: $(OUT)/Caroline.exe
	mkdir -p "$(INSTALLER_OUT)"
	cd "$(OUT)" && python -c "import zipfile,os; z=zipfile.ZipFile(r'$(INSTALLER_OUT)/Caroline.zip','w',zipfile.ZIP_DEFLATED); [z.write(os.path.join(r,f), os.path.relpath(os.path.join(r,f), '.')) for r,_,fs in os.walk('.') for f in fs]; z.close()"
	powershell -NoProfile -Command "(Get-FileHash '$(INSTALLER_OUT)/Caroline.zip' -Algorithm SHA256).Hash.ToLower()" > "$(INSTALLER_OUT)/Caroline.zip.sha256"
	echo "$${VERSION:-$$(powershell -NoProfile -Command "Get-Date -Format yyyyMMddHHmm")}" > "$(INSTALLER_OUT)/Caroline.zip.version"

$(INSTALLER_OUT)/Caroline.zip.sha256 $(INSTALLER_OUT)/Caroline.zip.version: $(INSTALLER_OUT)/Caroline.zip

# --- 6. Installer publish (was build_installer.bat, steps 3-4) -------------
# Depends on its own sources only (2026-11-22): the installer downloads Caroline.zip at run
# time (DownloadsInfo.cs) and embeds nothing of it, so a new zip is no reason to republish it.
$(INSTALLER_OUT)/CarolineInstaller.exe: $(INSTALLER_SRC)
	dotnet publish "$(INSTALLER_PROJECT)" -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -o "$(INSTALLER_OUT)"
	powershell -NoProfile -Command "(Get-FileHash '$(INSTALLER_OUT)/CarolineInstaller.exe' -Algorithm SHA256).Hash.ToLower()" > "$(INSTALLER_OUT)/CarolineInstaller.exe.sha256"
	@echo ""
	@echo "Installer build complete: $(INSTALLER_OUT)"

$(INSTALLER_OUT)/CarolineInstaller.exe.sha256: $(INSTALLER_OUT)/CarolineInstaller.exe

installer: $(INSTALLER_OUT)/CarolineInstaller.exe $(INSTALLER_OUT)/Caroline.zip

clean:
	rm -rf "$(OUT)" "$(INSTALLER_OUT)" "$(OUT_LINUX)" "$(OUT_INTELLICAT)"

# --- 7. Deploy targets (private) ---------------------------------------------
#
# Not part of the public build: deploy.mk is gitignored and exists only on
# the maintainer's own machine. See deploy.mk's own header comment for why.
-include $(DEPLOY_MK)
