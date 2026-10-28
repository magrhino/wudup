.PHONY: webui-demo-state webui-dev webui-demo lock test lint python-env

# This checkout's .venv, else the main checkout's (git worktrees share it).
# Override with PYTHON_BIN=/path/to/python.
PYTHON_BIN ?= $(firstword $(wildcard $(CURDIR)/.venv/bin/python) $(wildcard $(dir $(shell git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)).venv/bin/python))
# Absolute so test subprocesses import this checkout's src, not the venv's editable install.
PYTHON_ENV = PYTHONPATH="$(CURDIR)/src$${PYTHONPATH:+:$$PYTHONPATH}"

lock:
	./scripts/lock.sh

webui-demo-state:
	python3 webui/scripts/seed_demo_state.py

webui-dev:
	npm --prefix webui run dev

webui-demo: webui-dev

python-env:
	@if [ -z "$(PYTHON_BIN)" ] || [ ! -x "$(PYTHON_BIN)" ]; then \
		echo "No Python virtual environment found at .venv (here or in the main checkout)." >&2; \
		echo "Create it as described in docs/DEVELOPMENT.md (Local Setup), or set PYTHON_BIN." >&2; \
		exit 1; \
	fi

# Pass pytest arguments with ARGS, e.g. make test ARGS="tests/test_python_cli.py -k dry_run".
test: python-env
	$(PYTHON_ENV) "$(PYTHON_BIN)" -m pytest $(ARGS)

lint: python-env
	"$(PYTHON_BIN)" -m ruff check .
