# Sports Arb Bot — makefile for common setup/run tasks.
#
#   make setup     # create venv + install package (core deps)
#   make browser   # install Playwright + download Chromium (browser books)
#   make config    # copy the example config to config.yaml if it doesn't exist
#   make run       # start the dashboard + bot loop
#   make test      # run the test suite (engine smoke tests)
#   make clean     # wipe venv + python artifacts
#
# All commands are safe to re-run.

PY         ?= python3
VENV       := .venv
PIP        := $(VENV)/bin/pip
PYTHON     := $(VENV)/bin/python
PKG        := -e .

.DEFAULT_GOAL := help
SHELL        := /bin/bash

.PHONY: help
help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

.PHONY: venv
venv: ## Create a virtual environment in .venv
	test -d $(VENV) || $(PY) -m venv $(VENV)
	$(PIP) install --upgrade pip

.PHONY: setup
setup: venv config ## Install the package (editable) with core dependencies
	$(PIP) install $(PKG)
	@echo "Done. Next: fill in config.yaml, then `make run`."

.PHONY: browser
browser: venv ## Install Playwright + download Chromium (for browser books)
	$(PIP) install -e .[browser]
	$(PYTHON) -m playwright install chromium

.PHONY: config
config: ## Create config.yaml from the example if it is missing
	@test -f config.yaml && echo "config.yaml already exists — leaving it alone." \
		|| { echo "Creating config.yaml from config.example.yaml"; \
			cp config.example.yaml config.yaml; echo "Edit config.yaml (API key, bankroll, books)."; }

.PHONY: run
run: ## Start the dashboard + bot loop (Ctrl-C to stop)
	$(PYTHON) -m arb

.PHONY: test
test: venv ## Run the test suite (installs the 'dev' extra for pytest)
	$(PIP) install -e .[dev]
	$(PYTHON) -m pytest

.PHONY: clean
clean: ## Remove venv + python artifacts (keeps config + db)
	rm -rf $(VENV) build dist *.egg-info arb/*.egg-info
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
