# Local Typing Input Simulator
#
#   make setup   one-time: create the virtualenv and install everything
#   make run     run the overlay from source
#   make test    run the automated test suite
#   make app     build a double-clickable "Typing Simulator.app"
#   make install build the app and copy it into /Applications
#
#   make reset-permissions
#                clear stale macOS permission entries, when System Settings
#                shows the app as allowed but the app says it is not
#
# Everything lives inside .venv; nothing is installed system-wide.

VENV       := .venv
PY         := $(VENV)/bin/python
PIP        := $(VENV)/bin/pip
PYTEST     := $(VENV)/bin/pytest
PYINSTALL  := $(VENV)/bin/pyinstaller
APP_NAME   := Typing Simulator
APP        := dist/$(APP_NAME).app
SPEC       := packaging/TypingSimulator.spec
# Must match `bundle_identifier` in $(SPEC); it is how tccutil names the app.
BUNDLE_ID  := local.typing-simulator

# Python 3.11+ is required; override with `make PYTHON=/path/to/python3.12 setup`.
PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || \
                  command -v python3.11 || command -v python3)

.DEFAULT_GOAL := help
.PHONY: help setup run test app install reset-permissions clean distclean check

help: ## Show this help
	@echo "Local Typing Input Simulator"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-10s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "First time:  make setup && make run"

$(PY):
	@echo "==> Creating the virtualenv with $(PYTHON)"
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
		|| { echo "Python 3.11 or newer is required (found $$($(PYTHON) -V))."; exit 1; }
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip

setup: $(PY) ## Create the virtualenv and install the app and its dev tools
	@echo "==> Installing dependencies"
	$(PIP) install --quiet -e ".[dev]"
	@echo
	@echo "Done. Run it with:  make run"
	@echo "macOS will ask for Accessibility permission the first time you type."

run: setup ## Run the overlay from source
	$(PY) -m typing_simulator

test: setup ## Run the automated test suite
	$(PYTEST)

check: test ## Alias for `make test`

app: setup ## Build a double-clickable .app bundle
	@echo "==> Installing the build tool"
	$(PIP) install --quiet "pyinstaller>=6.3"
	@echo "==> Building $(APP_NAME).app (this takes a minute)"
	$(PYINSTALL) --noconfirm --clean --distpath dist --workpath build/pyinstaller $(SPEC)
	@echo "==> Verifying the code signature"
	@codesign --verify --strict "$(APP)" \
		|| { echo "The bundle is not validly signed; macOS will never grant it \
Accessibility. Try 'make clean app'."; exit 1; }
	@$(MAKE) --no-print-directory reset-permissions
	@echo
	@echo "Built: $(APP)"
	@echo "Open it with:  open \"$(APP)\""
	@echo "It will ask for Accessibility permission itself on the first run."

# Why this runs after every build
# -------------------------------
# The bundle is ad-hoc signed, so macOS identifies it by a hash of its
# contents.  Every rebuild produces a different hash, which means an existing
# TCC entry stops matching the app - and the failure is silent and extremely
# confusing: System Settings still lists "Typing Simulator" with the switch on,
# while the app is told it has no permission.  Clearing the stale entry here
# means the next launch prompts cleanly and the grant that results is one that
# actually applies.
reset-permissions: ## Clear stale macOS permission entries for the app
	@echo "==> Clearing stale permission entries for $(BUNDLE_ID)"
	@tccutil reset Accessibility $(BUNDLE_ID) >/dev/null 2>&1 || true
	@tccutil reset PostEvent $(BUNDLE_ID) >/dev/null 2>&1 || true
	@tccutil reset ListenEvent $(BUNDLE_ID) >/dev/null 2>&1 || true
	@echo "    Grant permission again on the next launch."

install: app ## Build the app and copy it into /Applications
	@echo "==> Copying to /Applications"
	rm -rf "/Applications/$(APP_NAME).app"
	cp -R "$(APP)" "/Applications/$(APP_NAME).app"
	@echo "Installed: /Applications/$(APP_NAME).app"
	@echo
	@echo "This is a different copy from $(APP), and macOS grants permission"
	@echo "per copy. Open the installed one and grant permission to it:"
	@echo "  open \"/Applications/$(APP_NAME).app\""

clean: ## Remove build output and caches
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

distclean: clean ## Also remove the virtualenv
	rm -rf $(VENV)
