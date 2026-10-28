#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
# Resolve symlinks (macOS /var -> /private/var) so paths match git's output.
TMP_DIR="$(cd "$TMP_DIR" && pwd -P)"

unset PYTHON_BIN

fail() {
  printf 'not ok - %s\n' "$*" >&2
  exit 1
}

# Prints the interpreter the Makefile selects when run from $1.
selected_python() {
  local dir="$1"
  make -s -C "$dir" -f "$REPO_ROOT/Makefile" -f "$TMP_DIR/show.mk" show-python-bin
}

assert_selected() {
  local dir="$1" expected="$2" actual
  actual="$(selected_python "$dir")"
  [[ "$actual" == "$expected" ]] || fail "from $dir expected PYTHON_BIN=$expected, got $actual"
}

make_venv() {
  local checkout="$1"
  mkdir -p "$checkout/.venv/bin"
  ln -s "$TMP_DIR/bin/fakepython" "$checkout/.venv/bin/python"
}

new_repo_with_worktree() {
  local main="$1" worktree="$2"
  git init -q "$main"
  git -C "$main" -c user.name=test -c user.email=test@example.invalid commit -q --allow-empty -m init
  git -C "$main" worktree add -q --detach "$worktree"
}

mkdir -p "$TMP_DIR/bin"
printf '#!/bin/sh\nexit 0\n' > "$TMP_DIR/bin/fakepython"
chmod +x "$TMP_DIR/bin/fakepython"
# shellcheck disable=SC2016 # $(PYTHON_BIN) is Make syntax, expanded by make.
printf 'show-python-bin:\n\t@printf "%%s\\n" "$(PYTHON_BIN)"\n' > "$TMP_DIR/show.mk"

# A worktree without its own venv uses the main checkout's, even with spaces in the path.
main="$TMP_DIR/main checkout"
worktree="$TMP_DIR/linked worktree"
new_repo_with_worktree "$main" "$worktree"
make_venv "$main"
assert_selected "$main" "$main/.venv/bin/python"
assert_selected "$worktree" "$main/.venv/bin/python"

# A broken worktree venv falls back to the main checkout's.
mkdir -p "$worktree/.venv/bin"
ln -s "$TMP_DIR/missing-python" "$worktree/.venv/bin/python"
assert_selected "$worktree" "$main/.venv/bin/python"

# A runnable worktree venv wins over the main checkout's.
rm "$worktree/.venv/bin/python"
ln -s "$TMP_DIR/bin/fakepython" "$worktree/.venv/bin/python"
assert_selected "$worktree" "$worktree/.venv/bin/python"

# PYTHON_BIN may name a command on PATH.
PATH="$TMP_DIR/bin:$PATH" make -s -C "$worktree" -f "$REPO_ROOT/Makefile" python-env PYTHON_BIN=fakepython \
  || fail "PYTHON_BIN=fakepython on PATH was rejected"

# Missing interpreters fail with an operator-facing message.
mkdir -p "$TMP_DIR/no venv"
if output="$(make -s -C "$TMP_DIR/no venv" -f "$REPO_ROOT/Makefile" python-env 2>&1)"; then
  fail "python-env passed without a venv"
fi
[[ "$output" == *"No Python virtual environment found"* ]] || fail "unexpected missing-venv output: $output"

if output="$(make -s -C "$worktree" -f "$REPO_ROOT/Makefile" python-env PYTHON_BIN="$TMP_DIR/missing-python" 2>&1)"; then
  fail "python-env accepted a missing PYTHON_BIN"
fi
[[ "$output" == *"is not a runnable Python interpreter"* ]] || fail "unexpected bad PYTHON_BIN output: $output"

printf 'ok - Makefile Python interpreter discovery\n'
