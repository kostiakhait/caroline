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
BACKEND_DIR := $(SCRIPT_DIR)/backend
BACKEND_PY_DIR := $(SCRIPT_DIR)/backend-py

# Any local clone of the `reforce` repo that has the `caroline` branch reachable
# (as a local branch or via a remote, e.g. origin/caroline) -- pulled via `git show
# caroline:<path>`, not a plain file copy, so this works regardless of what that
# clone's OWN working tree currently has checked out. Override on the command line
# (`make build CAMERLENGO_REPO=...`) for a non-default sibling layout.
CAMERLENGO_REPO ?= $(SCRIPT_DIR)/../reforce
CAMERLENGO_VENDOR_STUBS := $(BACKEND_PY_DIR)/camerlengo-vendor-stubs
CAROLINE_DIR := $(SCRIPT_DIR)/Windows/Caroline
NATIVEHOST_DIR := $(SCRIPT_DIR)/Windows/Caroline.NativeHost
INSTALLER_DIR := $(SCRIPT_DIR)/Windows/CarolineInstaller
XCFA_DIR := $(SCRIPT_DIR)/vendor/XcfaRenderer
PROJECT := $(CAROLINE_DIR)/Caroline.csproj
NATIVEHOST_PROJECT := $(NATIVEHOST_DIR)/Caroline.NativeHost.csproj
INSTALLER_PROJECT := $(INSTALLER_DIR)/CarolineInstaller.csproj
OUT := $(SCRIPT_DIR)/dist
INSTALLER_OUT := $(SCRIPT_DIR)/dist_installer

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
BACKEND_SRC := $(shell find "$(BACKEND_DIR)/src" -type f 2>/dev/null)
BACKEND_PY_SRC := $(shell find "$(BACKEND_PY_DIR)/app" -type f -name '*.py' 2>/dev/null) $(BACKEND_PY_DIR)/run_server.py $(BACKEND_PY_DIR)/supervisor.py
CAROLINE_SRC := $(shell find "$(CAROLINE_DIR)" -type f -not -path '*/bin/*' -not -path '*/obj/*' \( -name '*.cs' -o -name '*.xaml' -o -name '*.csproj' -o -path '*/wwwroot/*' \) 2>/dev/null)
NATIVEHOST_SRC := $(shell find "$(NATIVEHOST_DIR)" -type f -not -path '*/bin/*' -not -path '*/obj/*' \( -name '*.cs' -o -name '*.xaml' -o -name '*.csproj' \) 2>/dev/null)
INSTALLER_SRC := $(shell find "$(INSTALLER_DIR)" -type f -not -path '*/bin/*' -not -path '*/obj/*' \( -name '*.cs' -o -name '*.csproj' -o -path '*/Assets/*' \) 2>/dev/null)
XCFA_SRC := $(shell find "$(XCFA_DIR)" -type f -not -path '*/bin/*' -not -path '*/obj/*' -not -path '*/XcfaRenderer.Tests/*' -not -path '*/Demo/*' \( -name '*.cs' -o -name '*.csproj' \) 2>/dev/null)

LINUX_SHELL_DIR := $(SCRIPT_DIR)/linux-shell
LINUX_SHELL_SRC := $(shell find "$(LINUX_SHELL_DIR)" -type f 2>/dev/null)
WWWROOT_DIR := $(CAROLINE_DIR)/wwwroot
OUT_LINUX := $(SCRIPT_DIR)/dist-linux

.PHONY: help build installer clean dist-linux

help:
	@echo "Targets: build, installer, clean, dist-linux"
	@echo "(deploy, deploy-models: available only when deploy.mk is present -- see its header comment)"

# --- 1. Backend: npm install only when package.json/lockfile change --------
$(BACKEND_DIR)/node_modules/.stamp: $(BACKEND_DIR)/package.json $(BACKEND_DIR)/package-lock.json
	cd "$(BACKEND_DIR)" && npm install
	touch "$@"

# --- 2. Backend: recompile only when its own source or deps changed --------
$(BACKEND_DIR)/dist/.stamp: $(BACKEND_DIR)/node_modules/.stamp $(BACKEND_SRC) $(BACKEND_DIR)/tsconfig.json
	cd "$(BACKEND_DIR)" && npm run build
	touch "$@"

# --- 3. MCP server bundling -- rebundle only when the compiled backend changed --
$(BACKEND_DIR)/mcp-servers/.stamp: $(BACKEND_DIR)/dist/.stamp
	cd "$(BACKEND_DIR)" && node scripts/bundle-mcp-servers.mjs
	touch "$@"

# --- 4. WPF shell publish + backend sidecar copy (was build.bat) -----------
#
# Produces a self-contained, portable install folder under dist/ -- entirely
# from this repo's own sources (backend/mcp-servers-src/ for the bundled MCP
# servers, vendor/XcfaRenderer for Visual Mode), no reference to any other
# repo at runtime.
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
# CAMERLENGO_VENDOR_STUBS/Config.py listed explicitly (2026-10-03 bug fix, confirmed
# live): the vendoring recipe below copies it into dist/backend-py/camerlengo/Config.py,
# but nothing above (BACKEND_PY_SRC only covers backend-py/app + run_server.py/
# supervisor.py) ever told Make this target depends on it -- two deploys in a row
# silently shipped a STALE dist/Caroline.exe with the old stub because Make saw no
# changed prerequisite and skipped the entire rebuild recipe, including `rm -rf $(OUT)`.
# NOTE: the actual vendored AI.py/etc. content (pulled live via `git show caroline:...`
# from CAMERLENGO_REPO) still has NO file-based prerequisite here and can't easily get
# one -- a change on the reforce side alone still requires `make clean` to force a
# real rebuild; only the local stub file is now tracked automatically.
$(OUT)/Caroline.exe: $(CAROLINE_SRC) $(NATIVEHOST_SRC) $(XCFA_SRC) $(BACKEND_DIR)/mcp-servers/.stamp $(BACKEND_PY_SRC) $(CAMERLENGO_VENDOR_STUBS)/Config.py
	@echo "=== Caroline Build ==="
	rm -rf "$(OUT)"
	dotnet publish "$(PROJECT)" -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -o "$(OUT)"
	@echo "--- Caroline.NativeHost (embedded-browser host, see its own doc comment) ---"
	dotnet publish "$(NATIVEHOST_PROJECT)" -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -o "$(OUT)"
	mkdir -p "$(OUT)/backend"
	cp -r "$(BACKEND_DIR)/dist" "$(OUT)/backend/dist" & \
	cp -r "$(BACKEND_DIR)/node_modules" "$(OUT)/backend/node_modules" & \
	cp -r "$(BACKEND_DIR)/mcp-servers" "$(OUT)/backend/mcp-servers" & \
	cp -r "$(BACKEND_DIR)/skills-src" "$(OUT)/backend/skills-src" & \
	cp -r "$(BACKEND_DIR)/python-scripts" "$(OUT)/backend/python-scripts" & \
	wait
	cp "$(BACKEND_DIR)/package.json" "$(OUT)/backend/package.json"
	@echo "--- backend-py (the live backend -- see Native/SupervisorClient.cs) ---"
	mkdir -p "$(OUT)/backend-py"
	cp -r "$(BACKEND_PY_DIR)/app" "$(OUT)/backend-py/app"
	cp "$(BACKEND_PY_DIR)/run_server.py" "$(OUT)/backend-py/run_server.py"
	cp "$(BACKEND_PY_DIR)/supervisor.py" "$(OUT)/backend-py/supervisor.py"
	find "$(OUT)/backend-py/app" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	mkdir -p "$(OUT)/backend-py/python-scripts"
	cp "$(BACKEND_DIR)/python-scripts/local_tts_server.py" "$(OUT)/backend-py/python-scripts/local_tts_server.py"
	cp -r "$(BACKEND_DIR)/skills-src" "$(OUT)/backend-py/skills-src"
	@echo "--- whatsapp-sidecar (Baileys, Node -- see app/whatsapp_channel.py) ---"
	mkdir -p "$(OUT)/whatsapp-sidecar"
	cp "$(SCRIPT_DIR)/companion-apps/whatsapp-sidecar/package.json" "$(OUT)/whatsapp-sidecar/package.json"
	cp "$(SCRIPT_DIR)/companion-apps/whatsapp-sidecar/index.js" "$(OUT)/whatsapp-sidecar/index.js"
	@echo "--- camerlengo (small-model primary path, see small_model_engine.py's own doc comment) ---"
	@if git -C "$(CAMERLENGO_REPO)" cat-file -e caroline:AI.py 2>/dev/null; then \
		mkdir -p "$(OUT)/backend-py/camerlengo"; \
		for f in AI.py Cache.py EmailBasics.py Logger.py JSONStorage.py; do \
			git -C "$(CAMERLENGO_REPO)" show caroline:$$f > "$(OUT)/backend-py/camerlengo/$$f"; \
		done; \
		cp "$(CAMERLENGO_VENDOR_STUBS)/Config.py" "$(OUT)/backend-py/camerlengo/Config.py"; \
		echo "vendored from $(CAMERLENGO_REPO)@caroline"; \
	else \
		echo "WARNING: $(CAMERLENGO_REPO) has no reachable 'caroline' branch -- skipping camerlengo vendoring."; \
		echo "         small_model_engine.py degrades to always-escalate on this build (never a hard dependency)."; \
	fi
	@echo "Build complete: $(OUT)/Caroline.exe"

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
$(OUT_LINUX)/linux-shell/caroline_shell.py: $(LINUX_SHELL_SRC) $(BACKEND_PY_SRC) $(CAMERLENGO_VENDOR_STUBS)/Config.py
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
	find "$(OUT_LINUX)/backend-py/app" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find "$(OUT_LINUX)/linux-shell" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "--- camerlengo (small-model primary path, same conditional vendoring as the Windows build) ---"
	@if git -C "$(CAMERLENGO_REPO)" cat-file -e caroline:AI.py 2>/dev/null; then \
		mkdir -p "$(OUT_LINUX)/backend-py/camerlengo"; \
		for f in AI.py Cache.py EmailBasics.py Logger.py JSONStorage.py; do \
			git -C "$(CAMERLENGO_REPO)" show caroline:$$f > "$(OUT_LINUX)/backend-py/camerlengo/$$f"; \
		done; \
		cp "$(CAMERLENGO_VENDOR_STUBS)/Config.py" "$(OUT_LINUX)/backend-py/camerlengo/Config.py"; \
		echo "vendored from $(CAMERLENGO_REPO)@caroline"; \
	else \
		echo "WARNING: $(CAMERLENGO_REPO) has no reachable 'caroline' branch -- skipping camerlengo vendoring."; \
	fi
	@echo "Linux shell package complete: $(OUT_LINUX)"
	@echo "NOTE: runtime/python and runtime/codex are NOT produced by this recipe (it's plain file"
	@echo "      copying, cross-platform by design). Run on a Linux host:"
	@echo "      linux-shell/provision_runtime.sh $(OUT_LINUX)"

dist-linux: $(OUT_LINUX)/linux-shell/caroline_shell.py

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
$(INSTALLER_OUT)/CarolineInstaller.exe: $(INSTALLER_SRC) $(INSTALLER_OUT)/Caroline.zip $(INSTALLER_OUT)/Caroline.zip.sha256 $(INSTALLER_OUT)/Caroline.zip.version
	dotnet publish "$(INSTALLER_PROJECT)" -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -o "$(INSTALLER_OUT)"
	powershell -NoProfile -Command "(Get-FileHash '$(INSTALLER_OUT)/CarolineInstaller.exe' -Algorithm SHA256).Hash.ToLower()" > "$(INSTALLER_OUT)/CarolineInstaller.exe.sha256"
	@echo ""
	@echo "Installer build complete: $(INSTALLER_OUT)"

$(INSTALLER_OUT)/CarolineInstaller.exe.sha256: $(INSTALLER_OUT)/CarolineInstaller.exe

installer: $(INSTALLER_OUT)/CarolineInstaller.exe

clean:
	rm -rf "$(OUT)" "$(INSTALLER_OUT)" "$(OUT_LINUX)"

# --- 7. Deploy targets (private) ---------------------------------------------
#
# Not part of the public build: deploy.mk is gitignored and exists only on
# the maintainer's own machine. See deploy.mk's own header comment for why.
-include deploy.mk
