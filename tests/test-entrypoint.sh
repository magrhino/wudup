#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SCRIPT="$REPO_ROOT/entrypoint.sh"
TEST_TMP=""
APP_DIR=""
LAST_STATUS=0

# Keep developer shell settings from leaking into entrypoint output.
unset WUD_LOG_DIR WUD_SYNC_SCRIPTS WUD_SCRIPTS_DIR WUD_PENDING_SOURCE WUDUP_LEGACY_SCRIPTS

fail(){
  printf 'not ok - %s\n' "$*" >&2
  if [[ -n "${TEST_TMP:-}" && -f "$TEST_TMP/output.log" ]]; then
    sed 's/^/# /' "$TEST_TMP/output.log" >&2 || true
  fi
  exit 1
}

setup_case(){
  TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/wud-entrypoint-test.XXXXXX")"
  APP_DIR="$TEST_TMP/app"
  mkdir -p "$APP_DIR/bin" "$TEST_TMP/docker" "$TEST_TMP/out"

  cat > "$APP_DIR/bin/docker-update-from-wud" <<'FAKE_UPDATER'
#!/usr/bin/env bash
printf 'docker-update-from-wud'
for arg in "$@"; do
  printf ' [%s]' "$arg"
done
printf '\n'
FAKE_UPDATER
  chmod +x "$APP_DIR/bin/docker-update-from-wud"

  cat > "$TEST_TMP/python" <<'FAKE_PYTHON'
#!/usr/bin/env bash
printf 'python'
for arg in "$@"; do
  printf ' [%s]' "$arg"
done
printf '\n'
FAKE_PYTHON
  chmod +x "$TEST_TMP/python"
}

teardown_case(){
  [[ -n "${TEST_TMP:-}" && -d "$TEST_TMP" ]] && rm -rf "$TEST_TMP"
  TEST_TMP=""
}

run_entrypoint(){
  local -a env_args=(
    "WUD_APP_DIR=$APP_DIR"
    "DOCKER_BASE=$TEST_TMP/docker"
    "WUD_OUT_FILE=$TEST_TMP/out/images.todo"
    "PYTHON_BIN=${PYTHON_BIN:-}"
  )
  local name
  for name in WUD_SYNC_SCRIPTS WUD_SCRIPTS_DIR WUD_PENDING_SOURCE WUDUP_LEGACY_SCRIPTS WUD_LOG_DIR; do
    if [[ -n "${!name+x}" ]]; then
      env_args+=("$name=${!name}")
    fi
  done

  LAST_STATUS=0
  env -u WUD_SYNC_SCRIPTS -u WUD_SCRIPTS_DIR -u WUD_PENDING_SOURCE -u WUDUP_LEGACY_SCRIPTS \
    "${env_args[@]}" "$SCRIPT" "$@" > "$TEST_TMP/output.log" 2>&1 ||
    LAST_STATUS=$?
}

assert_status(){
  local expected="$1"
  [[ "$LAST_STATUS" == "$expected" ]] || fail "expected status $expected, got $LAST_STATUS"
}

assert_output(){
  local expected="$1" actual
  actual="$(cat "$TEST_TMP/output.log")"
  [[ "$actual" == "$expected" ]] || fail "expected output [$expected], got [$actual]"
}

assert_output_contains(){
  local expected="$1"
  grep -qF -- "$expected" "$TEST_TMP/output.log" || fail "expected output to contain [$expected]"
}

test_default_runs_web(){
  setup_case
  PYTHON_BIN="$TEST_TMP/python" run_entrypoint
  assert_status 0
  assert_output "python [-m] [wudup.cli] [web] [--base] [$TEST_TMP/docker] [--file] [$TEST_TMP/out/images.todo] [--log-dir] [/logs]"
  teardown_case
}

test_leading_flag_exits_with_guidance(){
  local flag
  for flag in --yes --dry-run; do
    setup_case
    PYTHON_BIN="$TEST_TMP/python" run_entrypoint "$flag"
    assert_status 2
    assert_output_contains "Options without a command (such as $flag) used to run the removed updates wrapper"
    assert_output_contains "legacy-file-mode"
    if grep -q '^python\|^docker-update-from-wud' "$TEST_TMP/output.log"; then
      fail "$flag dispatched to another command"
    fi
    teardown_case
  done
}

test_leading_flag_guidance_prints_arguments_literally(){
  setup_case
  PYTHON_BIN="$TEST_TMP/python" run_entrypoint '--%s%n%d'
  assert_status 2
  assert_output_contains "Options without a command (such as --%s%n%d) used to run the removed updates wrapper"
  teardown_case
}

test_doctor_dispatch_injects_paths(){
  setup_case
  PYTHON_BIN="$TEST_TMP/python" run_entrypoint doctor --no-color
  assert_status 0
  assert_output "python [-m] [wudup.cli] [doctor] [--base] [$TEST_TMP/docker] [--file] [$TEST_TMP/out/images.todo] [--log-dir] [/logs] [--no-color]"
  teardown_case
}

test_updater_dispatch_injects_missing_paths(){
  setup_case
  run_entrypoint docker-update-from-wud --yes
  assert_status 0
  assert_output "docker-update-from-wud [--base] [$TEST_TMP/docker] [--file] [$TEST_TMP/out/images.todo] [--log-dir] [/logs] [--yes]"
  teardown_case
}

test_updater_dispatch_preserves_explicit_paths(){
  setup_case
  run_entrypoint docker-update-from-wud --dry-run --base /custom/docker --file /custom/images.todo
  assert_status 0
  assert_output 'docker-update-from-wud [--log-dir] [/logs] [--dry-run] [--base] [/custom/docker] [--file] [/custom/images.todo]'
  teardown_case
}

test_updater_dispatch_preserves_explicit_log_dir(){
  setup_case
  WUD_LOG_DIR=/env/logs run_entrypoint docker-update-from-wud --dry-run --log-dir /custom/logs
  assert_status 0
  assert_output "docker-update-from-wud [--base] [$TEST_TMP/docker] [--file] [$TEST_TMP/out/images.todo] [--dry-run] [--log-dir] [/custom/logs]"
  teardown_case
}

test_web_dispatch_injects_paths(){
  setup_case
  PYTHON_BIN="$TEST_TMP/python" run_entrypoint web --host 0.0.0.0
  assert_status 0
  assert_output "python [-m] [wudup.cli] [web] [--base] [$TEST_TMP/docker] [--file] [$TEST_TMP/out/images.todo] [--log-dir] [/logs] [--host] [0.0.0.0]"
  teardown_case
}

test_removed_commands_exit_with_guidance(){
  local command
  for command in updates sync-wud-scripts truenas-status-export; do
    setup_case
    PYTHON_BIN="$TEST_TMP/python" run_entrypoint "$command" --yes
    assert_status 2
    assert_output_contains "$command was removed"
    assert_output_contains "legacy-file-mode"
    if grep -q '^python\|^docker-update-from-wud' "$TEST_TMP/output.log"; then
      fail "$command dispatched to another command"
    fi
    teardown_case
  done
}

test_removed_sync_settings_warn_and_do_not_sync(){
  setup_case
  mkdir -p "$TEST_TMP/managed-wud"
  PYTHON_BIN="$TEST_TMP/python" WUD_SYNC_SCRIPTS=1 WUD_SCRIPTS_DIR="$TEST_TMP/managed-wud" run_entrypoint web
  assert_status 0
  assert_output_contains "Ignoring WUD_SYNC_SCRIPTS: WUD script sync was removed"
  assert_output_contains "Ignoring WUD_SCRIPTS_DIR: WUD script sync was removed"
  assert_output_contains "python [-m] [wudup.cli] [web]"
  if [[ -n "$(find "$TEST_TMP/managed-wud" -mindepth 1 -print -quit)" ]]; then
    fail "entrypoint wrote into the old managed scripts directory"
  fi
  teardown_case
}

test_removed_pending_source_settings_warn_and_still_start_web(){
  setup_case
  PYTHON_BIN="$TEST_TMP/python" WUD_PENDING_SOURCE=file WUDUP_LEGACY_SCRIPTS=false run_entrypoint web
  assert_status 0
  assert_output_contains "Ignoring WUD_PENDING_SOURCE: the WebUI always reads pending updates from the WUD API"
  assert_output_contains "Ignoring WUDUP_LEGACY_SCRIPTS: the WebUI always reads pending updates from the WUD API"
  assert_output_contains "python [-m] [wudup.cli] [web] [--base] [$TEST_TMP/docker] [--file] [$TEST_TMP/out/images.todo] [--log-dir] [/logs]"
  teardown_case
}

test_debug_command_executes_directly(){
  setup_case
  run_entrypoint /bin/sh -c "printf 'debug [%s]\n' \"\$1\"" shell arg
  assert_status 0
  assert_output 'debug [arg]'
  teardown_case
}

run_test(){
  local name="$1"
  printf 'running %s\n' "$name"
  "$name"
  printf 'ok - %s\n' "$name"
}

main(){
  run_test test_default_runs_web
  run_test test_leading_flag_exits_with_guidance
  run_test test_leading_flag_guidance_prints_arguments_literally
  run_test test_doctor_dispatch_injects_paths
  run_test test_updater_dispatch_injects_missing_paths
  run_test test_updater_dispatch_preserves_explicit_paths
  run_test test_updater_dispatch_preserves_explicit_log_dir
  run_test test_web_dispatch_injects_paths
  run_test test_removed_commands_exit_with_guidance
  run_test test_removed_sync_settings_warn_and_do_not_sync
  run_test test_removed_pending_source_settings_warn_and_still_start_web
  run_test test_debug_command_executes_directly
}

trap teardown_case EXIT
main "$@"
