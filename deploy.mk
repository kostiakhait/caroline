# --- Caroline deploy targets (maintainer-only, not part of the public build)
#
# Split out of the main Makefile so a fresh clone of this repo can `make
# build`/`make installer` with nothing beyond what's actually committed here
# -- `deploy`/`deploy-models`/`upload` are internal-only (upload to the
# maintainer's own server via a private uploader tool that isn't part of
# this repo at all). Committed here since 2026-10-18 (disaster-recovery:
# this machine is the only copy otherwise) -- it carries no secret itself,
# only non-sensitive host/port/path config; the actual bearer token lives
# in deploy_token.txt, outside this repo entirely, gitignored, never
# committed anywhere (also backed up in Notes, see UPLOADER_DIR's own
# comment below). -include'd conditionally from the main Makefile: if
# $(UPLOADER_DIR) or the token file is missing (any clone but the
# maintainer's own), the recipes below fail fast with a clear message
# instead of silently doing nothing.
#
# UPLOADER_DIR intentionally points OUTSIDE this repo, at the private
# silmarillion monorepo Caroline was extracted from -- Uploader/ (the Go
# resumable-HTTP-PUT client) is internal tooling shared by other projects
# there too, and was never a candidate for vendoring/publishing alongside
# Caroline itself. This only matters on the maintainer's own machine, where
# both repos are checked out side by side; adjust below if that path ever
# moves.
UPLOADER_DIR := D:/REPO/silmarillion/Uploader
UPLOADER_EXE := $(UPLOADER_DIR)/uploader.exe

DEPLOY_HOST ?= downloader.multi-portal.org
DEPLOY_PATH ?= /var/www/html/apps/caroline
DEPLOY_MODELS_PATH ?= /var/www/html/apps/caroline/models
DEPLOY_PORT ?= 41981

MODELS_DIR := $(SCRIPT_DIR)/art/models
MODEL_FILES := CarolineA.xcfa CarolineB.xcfa PeterA.xcfa PeterB.xcfa

.PHONY: deploy upload deploy-models

# --- Deploy (was deploy.bat) -------------------------------------------------
#
# Uploads the four release files to $(DEPLOY_HOST) via the Uploader/ tool
# (resumable HTTP PUT, not scp -- scp kept dying mid-transfer on this
# connection with "client_loop: send disconnect: Connection reset" around
# 86MB into the ~300MB Caroline.zip). The uploader server runs persistently
# on the remote host as the "caroline-uploader" systemd service, port 41981,
# behind a firewalled reverse proxy (plain HTTP end-to-end -- see
# Uploader/main.go's -http flag).
#
# Depends on the real installer output files. Used to NOT actually be cheap
# when nothing changed (2026-10-17, confirmed live): $(OUT)/Caroline.exe
# depended on the phony camerlengo-vendor target, which Make always treated
# as stale, cascading into a full Caroline.zip re-zip (443MB) and a full
# `dotnet publish` of the installer on every single `make deploy`, build-vs-
# no-build. Fixed (2026-10-20, per explicit instruction: "Скопировать один
# раз и перестать"): camerlengo-vendor is no longer phony -- see the main
# Makefile's own $(OUT)/backend-py/camerlengo/.stamp rule -- so `make
# deploy` with nothing actually changed is now cheap again. Still, a deploy
# retried after a pure upload failure (the server/network side, not a code
# change) has no reason to re-walk the whole dependency graph at all. Use
# `make upload` instead to retry the upload alone against whatever is
# already sitting in $(INSTALLER_OUT).
#
# Requires $(UPLOADER_DIR)/deploy_token.txt (the bearer token printed when
# the caroline-uploader systemd service was first started on the remote
# host -- gitignored there too, not checked in).
deploy: $(INSTALLER_OUT)/CarolineInstaller.exe $(INSTALLER_OUT)/CarolineInstaller.exe.sha256 upload

# --- Upload-only (no build dependency at all) --------------------------------
#
# Added 2026-10-17 per explicit instruction, after repeated `make deploy`
# retries (chasing an unrelated upload-side failure) each silently re-paid
# the full zip+publish cascade described in `deploy`'s own comment above.
# Uploads exactly what is currently in $(INSTALLER_OUT) -- does not check
# it is fresh, does not rebuild anything. Fails fast with a clear message if
# the release files aren't there yet (first run needs a real `make deploy`
# or `make installer` first).
upload: $(UPLOADER_EXE)
	@for f in Caroline.zip Caroline.zip.sha256 Caroline.zip.version CarolineInstaller.exe CarolineInstaller.exe.sha256; do \
		test -f "$(INSTALLER_OUT)/$$f" || { \
			echo "ERROR: $(INSTALLER_OUT)/$$f not found -- run 'make deploy' or 'make installer' at least once first."; exit 1; }; \
	done
	@test -f "$(UPLOADER_DIR)/deploy_token.txt" || { \
		echo "ERROR: $(UPLOADER_DIR)/deploy_token.txt not found -- copy the token"; \
		echo "printed by the caroline-uploader systemd service on the remote host"; \
		echo "into that file, unquoted, no trailing newline needed."; exit 1; }
	@echo "Uploading to $(DEPLOY_HOST):$(DEPLOY_PORT) -> $(DEPLOY_PATH)..."
	@DEPLOY_TOKEN=$$(cat "$(UPLOADER_DIR)/deploy_token.txt"); \
	for f in Caroline.zip Caroline.zip.sha256 Caroline.zip.version CarolineInstaller.exe CarolineInstaller.exe.sha256; do \
		echo "  -> $$f"; \
		MSYS_NO_PATHCONV=1 "$(UPLOADER_EXE)" upload -http -host "$(DEPLOY_HOST)" -port $(DEPLOY_PORT) -token "$$DEPLOY_TOKEN" \
			-file "$(INSTALLER_OUT)/$$f" -remote-path "$(DEPLOY_PATH)/$$f" || { echo "ERROR: upload failed for $$f."; exit 1; }; \
	done
	@echo ""
	@echo "Deploy complete:"
	@echo "  https://downloader.multi-portal.org/apps/caroline/Caroline.zip"
	@echo "  https://downloader.multi-portal.org/apps/caroline/CarolineInstaller.exe"

# --- Models deploy (was deploy_models.bat) -----------------------------------
#
# Uploads the four talking-head models (art/models/*.xcfa, tens of GB each)
# to $(DEPLOY_HOST), where CarolineInstaller's ModelsInstaller.cs downloads
# them from on a fresh install. Separate from `deploy` on purpose -- these
# rarely change and are far too big to re-upload on every routine app deploy.
# SHA-256 is computed fresh each run (can take a while for a 12+ GB file) --
# no attempt to skip/cache that, this target is run rarely by hand.
deploy-models: $(UPLOADER_EXE)
	@test -f "$(UPLOADER_DIR)/deploy_token.txt" || { \
		echo "ERROR: $(UPLOADER_DIR)/deploy_token.txt not found -- see the deploy target's"; \
		echo "own comment for how to obtain it."; exit 1; }
	@echo "=== Caroline Visual Mode Models Deploy ==="
	@echo "Target : $(DEPLOY_HOST):$(DEPLOY_MODELS_PATH)"
	@echo "[1/2] Computing SHA-256 for each model (this can take a while)..."
	@for m in $(MODEL_FILES); do \
		if [ ! -f "$(MODELS_DIR)/$$m" ]; then echo "  $$m not found locally, skipping."; \
		else echo "  hashing $$m..."; \
			powershell -NoProfile -Command "(Get-FileHash '$(MODELS_DIR)/$$m' -Algorithm SHA256).Hash.ToLower()" > "$(MODELS_DIR)/$$m.sha256"; \
		fi; \
	done
	@echo "[2/2] Uploading to $(DEPLOY_HOST):$(DEPLOY_PORT) -> $(DEPLOY_MODELS_PATH)..."
	@DEPLOY_TOKEN=$$(cat "$(UPLOADER_DIR)/deploy_token.txt"); \
	for m in $(MODEL_FILES); do \
		if [ -f "$(MODELS_DIR)/$$m" ]; then \
			echo "  -> $$m"; \
			MSYS_NO_PATHCONV=1 "$(UPLOADER_EXE)" upload -http -host "$(DEPLOY_HOST)" -port $(DEPLOY_PORT) -token "$$DEPLOY_TOKEN" \
				-file "$(MODELS_DIR)/$$m" -remote-path "$(DEPLOY_MODELS_PATH)/$$m" || { echo "ERROR: upload failed for $$m."; exit 1; }; \
			echo "  -> $$m.sha256"; \
			MSYS_NO_PATHCONV=1 "$(UPLOADER_EXE)" upload -http -host "$(DEPLOY_HOST)" -port $(DEPLOY_PORT) -token "$$DEPLOY_TOKEN" \
				-file "$(MODELS_DIR)/$$m.sha256" -remote-path "$(DEPLOY_MODELS_PATH)/$$m.sha256" || { echo "ERROR: upload failed for $$m.sha256."; exit 1; }; \
		fi; \
	done
	@echo ""
	@echo "Models deploy complete: https://downloader.multi-portal.org/apps/caroline/models/"

# Skip the rebuild if uploader.exe is already newer than its sources --
# Make's own dependency tracking replaces the old scripts' manual mtime
# comparison for exactly this.
$(UPLOADER_EXE): $(UPLOADER_DIR)/main.go
	cd "$(UPLOADER_DIR)" && go build -o uploader.exe .

# --- Local install (maintainer's own machine) --------------------------------
#
# Switches this machine's own installed Caroline to the just-built dist/ --
# see install-local.ps1's own header comment for why that script replaced
# the old prepare2.ps1 + manual Caroline.exe copy + switch2.ps1 three-step
# dance (2026-10-12, per explicit instruction: a real, recurring gap where
# any C# change silently never reached the switched-to install unless
# copied by hand every time). Depends on the real build output, same as
# `deploy`, so this is always safe to run right after one.
.PHONY: install-local
install-local: $(OUT)/Caroline.exe
	powershell -NoProfile -ExecutionPolicy Bypass -File "$(SCRIPT_DIR)/install-local.ps1"
