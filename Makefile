# Local Typing Input Simulator
#
#   make setup   one-time: create the virtualenv and install everything
#   make dev     build and open the development app - the one to use while
#                working on this, because macOS can actually grant it
#                permission and the grant survives your edits
#   make run     run the overlay straight from source (see the warning below)
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

DEV_APP_NAME := Typing Simulator (dev)
DEV_APP      := dist/$(DEV_APP_NAME).app
# Must match BUNDLE_ID in packaging/dev_bundle.py.
DEV_BUNDLE_ID := local.typing-simulator.dev

# Python 3.11+ is required; override with `make PYTHON=/path/to/python3.12 setup`.
PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || \
                  command -v python3.11 || command -v python3)

.DEFAULT_GOAL := help
.PHONY: help setup dev dev-app run test app install reset-permissions \
        reset-dev-permissions clean distclean check

help: ## Show this help
	@echo "Local Typing Input Simulator"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-10s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "First time:  make setup && make dev"

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
	@echo "Done. Run it with:  make dev"
	@echo "macOS will ask for Accessibility permission the first time it starts."

# Why there is a development bundle at all
# ---------------------------------------
# macOS grants Accessibility to the *running executable*. Run from source that
# executable is the virtualenv's interpreter - a symlink to a Homebrew Python
# shared with everything else on the machine, which macOS additionally
# attributes to whichever terminal launched it. There is no way to grant that
# combination permission and have it mean this application.
#
# This bundle's executable is a copy of the interpreter placed *inside* the
# bundle, so the grant is made to the bundle. The source stays outside it and
# is reached through PYTHONPATH, so editing code does not change the bundle's
# contents - and an ad-hoc signature is a hash of exactly those contents. Grant
# it once and it stays granted, however many times you edit and relaunch.
dev-app: setup ## Build the development app bundle (about a second)
	@echo "==> Building $(DEV_APP_NAME).app"
	@$(PY) packaging/dev_bundle.py --dist dist --source src

dev: dev-app ## Build and open the development app
	@echo "==> Opening $(DEV_APP_NAME).app"
	@open "$(DEV_APP)"
	@echo
	@echo "Logs:  ~/Library/Logs/Typing Simulator/typing-simulator.log"
	@echo "If permission is refused and asking does nothing, run:"
	@echo "  make reset-dev-permissions"

run: setup ## Run the overlay straight from source (cannot hold a permission grant)
	@echo "Note: run from source, macOS attributes Accessibility to the"
	@echo "      interpreter and to the terminal that launched it, not to this"
	@echo "      application. Use 'make dev' if you need it to actually type."
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

# The development bundle does not normally need this - its identity survives
# source edits - but it does after the checkout moves, or if the prompt was
# dismissed once and macOS therefore stopped showing it. The app's own
# "Grant permission" button does exactly this when nothing is granted yet.
reset-dev-permissions: ## Clear macOS permission entries for the development app
	@echo "==> Clearing permission entries for $(DEV_BUNDLE_ID)"
	@tccutil reset Accessibility $(DEV_BUNDLE_ID) >/dev/null 2>&1 || true
	@tccutil reset PostEvent $(DEV_BUNDLE_ID) >/dev/null 2>&1 || true
	@tccutil reset ListenEvent $(DEV_BUNDLE_ID) >/dev/null 2>&1 || true
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
