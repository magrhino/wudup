.PHONY: webui-demo-state webui-dev webui-demo lock test lint python-env

# First runnable .venv interpreter: this checkout's, else the main checkout's
# (git worktrees share it). Override with PYTHON_BIN=/path/to/python or a name on PATH.
ifeq ($(strip $(PYTHON_BIN)),)
PYTHON_BIN := $(shell for dir in "$(CURDIR)" "$$(git worktree list --porcelain 2>/dev/null | sed -n '1s/^worktree //p')"; do \
	if [ -n "$$dir" ] && [ -x "$$dir/.venv/bin/python" ]; then printf '%s\n' "$$dir/.venv/bin/python"; break; fi; \
done)
endif
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
	@if [ -z "$(PYTHON_BIN)" ]; then \
		echo "No Python virtual environment found at .venv (here or in the main checkout)." >&2; \
		echo "Create it as described in docs/DEVELOPMENT.md (Local Setup), or set PYTHON_BIN." >&2; \
		exit 1; \
	elif ! command -v "$(PYTHON_BIN)" >/dev/null 2>&1; then \
		echo "PYTHON_BIN=$(PYTHON_BIN) is not a runnable Python interpreter or a command on PATH." >&2; \
		exit 1; \
	fi

# Pass pytest arguments with ARGS, e.g. make test ARGS="tests/test_python_cli.py -k dry_run".
test: python-env
	$(PYTHON_ENV) "$(PYTHON_BIN)" -m pytest $(ARGS)

lint: python-env
	"$(PYTHON_BIN)" -m ruff check .
