#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO_ROOT"

run(){
  printf '==> %s\n' "$*"
  "$@"
}

run_python_checks() {
  python_bin="${PYTHON_BIN:-}"
  if [[ -z "$python_bin" ]]; then
    if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
      PATH="$REPO_ROOT/.venv/bin:${PATH:-}"
      export PATH
      python_bin="$REPO_ROOT/.venv/bin/python"
    elif command -v python3.14 >/dev/null 2>&1; then
      python_bin="python3.14"
    elif command -v python3.13 >/dev/null 2>&1; then
      python_bin="python3.13"
    elif command -v python3.12 >/dev/null 2>&1; then
      python_bin="python3.12"
    elif command -v python3.11 >/dev/null 2>&1; then
      python_bin="python3.11"
    elif command -v python3.10 >/dev/null 2>&1; then
      python_bin="python3.10"
    elif command -v python3 >/dev/null 2>&1; then
      python_bin="python3"
    else
      cat >&2 <<'EOF'
Python 3.10 or newer is required to run the Python package tests.
EOF
      exit 127
    fi
  fi

  run "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else "Python 3.10 or newer is required")'

  if "$python_bin" -m ruff --version >/dev/null 2>&1; then
    run "$python_bin" -m ruff --version
  fi

  run "$python_bin" tests/check_python_deps.py "$REPO_ROOT/pyproject.toml"
  run env PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 "$python_bin" -m pip check

  run "$python_bin" -m ruff check .

  run "$python_bin" -m compileall -q src tests webui/scripts

  run "$python_bin" -m unittest tests.test_dependency_automerge_workflows

  run "$python_bin" -m pytest --cov=wudup --cov-branch --cov-report=xml
}

run_shell_checks() {
  if ! command -v shellcheck >/dev/null 2>&1; then
    cat >&2 <<'EOF'
shellcheck is required to run the full test suite.
Install it with your package manager, for example:
  brew install shellcheck
  sudo apt-get install shellcheck
EOF
    exit 127
  fi

  run bash -n \
    entrypoint.sh \
    install.sh \
    bin/updates \
    bin/docker-update-from-wud \
    wud/http.sh \
    wud/release-parser.sh \
    wud/release-notes-to-discord.sh \
    wud/github-release-embed.sh \
    wud/tag-manager.sh \
    tests/run-all.sh \
    tests/test-docker-update-from-wud.sh \
    tests/container-build.sh \
    tests/smoke-container-image.sh \
    tests/e2e-docker-compose.sh \
    tests/test-entrypoint.sh \
    tests/test-github-release-embed.sh \
    tests/test-release-parser.sh \
    tests/test-release-notes-to-discord.sh \
    tests/test-tag-manager.sh \
    tests/test-upstreams-map.sh \
    tests/test-wud-append-updates.sh \
    tests/test-install.sh \
    tests/test-publish-release-image.sh \
    tests/test-updates-wrapper.sh \
    tests/fakes/docker

  run sh -n \
    wud/on-update.sh \
    wud/append-updates.sh

  run shellcheck \
    entrypoint.sh \
    install.sh \
    bin/updates \
    bin/docker-update-from-wud \
    wud/on-update.sh \
    wud/append-updates.sh \
    wud/http.sh \
    wud/release-parser.sh \
    wud/release-notes-to-discord.sh \
    wud/github-release-embed.sh \
    wud/tag-manager.sh \
    tests/run-all.sh \
    tests/test-docker-update-from-wud.sh \
    tests/container-build.sh \
    tests/smoke-container-image.sh \
    tests/e2e-docker-compose.sh \
    tests/test-entrypoint.sh \
    tests/test-github-release-embed.sh \
    tests/test-release-parser.sh \
    tests/test-release-notes-to-discord.sh \
    tests/test-tag-manager.sh \
    tests/test-upstreams-map.sh \
    tests/test-wud-append-updates.sh \
    tests/test-install.sh \
    tests/test-publish-release-image.sh \
    tests/test-updates-wrapper.sh \
    tests/fakes/docker

  for test_script in tests/test-*.sh; do
    run "$test_script"
  done
}

run_webui_checks() {
  local required="${1:-false}"

  if command -v npm >/dev/null 2>&1 && [[ -f webui/package-lock.json ]]; then
    run node --check webui/scripts/dev-server.mjs
    run npm --prefix webui ci --ignore-scripts
    run npm --prefix webui run typecheck
    run npm --prefix webui run test
    run npm --prefix webui run build
  elif [[ "$required" == true ]]; then
    cat >&2 <<'EOF'
npm and webui/package-lock.json are required to run WebUI checks.
EOF
    exit 127
  else
    printf '==> skipping webui npm checks; npm or webui/package-lock.json not found\n'
  fi
}

replay_parallel_log() {
  local label="$1"
  local status="$2"
  local log_file="$3"

  printf '\n==> %s checks output\n' "$label"
  if [[ -s "$log_file" ]]; then
    sed 's/^/    /' "$log_file"
  else
    printf '    no output\n'
  fi
  if [[ "$status" -eq 0 ]]; then
    printf '==> %s checks passed\n' "$label"
  else
    printf '==> %s checks failed with status %s\n' "$label" "$status"
  fi
}

run_all_checks() {
  local tmp_dir
  local python_log shell_log webui_log
  local python_pid shell_pid webui_pid
  local python_status=0 shell_status=0 webui_status=0
  local failed_sections=""

  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/wud-run-all.XXXXXX")"
  python_log="$tmp_dir/python.log"
  shell_log="$tmp_dir/shell.log"
  webui_log="$tmp_dir/webui.log"

  printf '==> running python, shell, and webui checks in parallel\n'

  run_python_checks > "$python_log" 2>&1 &
  python_pid=$!
  run_shell_checks > "$shell_log" 2>&1 &
  shell_pid=$!
  run_webui_checks false > "$webui_log" 2>&1 &
  webui_pid=$!

  if wait "$python_pid"; then
    python_status=0
  else
    python_status=$?
  fi
  if wait "$shell_pid"; then
    shell_status=0
  else
    shell_status=$?
  fi
  if wait "$webui_pid"; then
    webui_status=0
  else
    webui_status=$?
  fi

  replay_parallel_log "python" "$python_status" "$python_log"
  replay_parallel_log "shell" "$shell_status" "$shell_log"
  replay_parallel_log "webui" "$webui_status" "$webui_log"

  rm -rf "$tmp_dir"

  if [[ "$python_status" -ne 0 ]]; then
    failed_sections="${failed_sections} python($python_status)"
  fi
  if [[ "$shell_status" -ne 0 ]]; then
    failed_sections="${failed_sections} shell($shell_status)"
  fi
  if [[ "$webui_status" -ne 0 ]]; then
    failed_sections="${failed_sections} webui($webui_status)"
  fi
  if [[ -n "$failed_sections" ]]; then
    printf '\nFailed test section(s):%s\n' "$failed_sections" >&2
    return 1
  fi

  printf '\n==> all test sections passed\n'
}

MODE="--all"
if [[ $# -gt 0 ]]; then
  MODE="$1"
fi

case "$MODE" in
  --python)
    run_python_checks
    ;;
  --shell)
    run_shell_checks
    ;;
  --webui)
    run_webui_checks true
    ;;
  --all)
    run_all_checks
    ;;
  *)
    echo "Usage: $0 [--python | --shell | --webui | --all]" >&2
    exit 1
    ;;
esac
