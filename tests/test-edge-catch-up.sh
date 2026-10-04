#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/wud-edge-catch-up-test.XXXXXX")"
trap 'rm -rf "$TEST_TMP"' EXIT

cat > "$TEST_TMP/gh" <<'FAKE_GH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_GH_LOG"
case "$1 ${2:-}" in
  "api "*)
    [[ "${FAKE_API_FAIL:-0}" != 1 ]] || exit 1
    printf '%s\n' "${FAKE_MAIN_SHA:-}"
    ;;
  "run list")
    [[ "${FAKE_RUN_LIST_FAIL:-0}" != 1 ]] || exit 1
    printf '%s\n' "${FAKE_PUBLISHED:-0}"
    ;;
  "workflow run") ;;
  *) exit 99 ;;
esac
FAKE_GH
chmod +x "$TEST_TMP/gh"

export PATH="$TEST_TMP:$PATH"
export FAKE_GH_LOG="$TEST_TMP/gh.log"
export GITHUB_REPOSITORY="magrhino/wudup"
built_sha="0123456789abcdef0123456789abcdef01234567"
head_sha="fedcba9876543210fedcba9876543210fedcba98"
dispatch='workflow run edge.yml --ref main'

cd "$REPO_ROOT"

run_catch_up() {
  : > "$FAKE_GH_LOG"
  BUILT_SHA="$built_sha" bash .github/scripts/edge-catch-up.sh > "$TEST_TMP/out" 2> "$TEST_TMP/err"
}

assert_no_dispatch() {
  if grep -Fxq "$dispatch" "$FAKE_GH_LOG"; then
    printf 'edge catch-up unexpectedly started a run: %s\n' "$1" >&2
    exit 1
  fi
}

# This run built the head of main: nothing to do.
FAKE_MAIN_SHA="$built_sha" run_catch_up
grep -Fxq 'api repos/magrhino/wudup/branches/main --jq .commit.sha' "$FAKE_GH_LOG"
grep -Fq "Edge is built from the head of main" "$TEST_TMP/out"
assert_no_dispatch 'head already built by this run'

# The head of main moved on and has no edge build yet: start one for main.
FAKE_MAIN_SHA="$head_sha" FAKE_PUBLISHED=0 run_catch_up
grep -Fxq "run list --workflow edge.yml --commit $head_sha --status success --json databaseId --jq length" "$FAKE_GH_LOG"
grep -Fxq "$dispatch" "$FAKE_GH_LOG"
grep -Fq "Started an edge run for the head of main ($head_sha)" "$TEST_TMP/out"

# The head of main already has a successful edge build (for example, after a
# re-run of an older commit): do not rebuild it.
FAKE_MAIN_SHA="$head_sha" FAKE_PUBLISHED=1 run_catch_up
grep -Fq "already has a successful edge build" "$TEST_TMP/out"
assert_no_dispatch 'head already published'

# Lookups that fail or return junk stop without starting a run and tell the
# operator how to catch edge up by hand.
for case_name in api-fail api-null api-short run-list-fail run-list-junk; do
  api_fail=0 run_list_fail=0 main_sha="$head_sha" published=0
  case "$case_name" in
    api-fail) api_fail=1 ;;
    api-null) main_sha=null ;;
    api-short) main_sha=fedcba9 ;;
    run-list-fail) run_list_fail=1 ;;
    run-list-junk) published=null ;;
  esac
  if FAKE_API_FAIL="$api_fail" FAKE_RUN_LIST_FAIL="$run_list_fail" FAKE_MAIN_SHA="$main_sha" \
    FAKE_PUBLISHED="$published" run_catch_up; then
    printf 'edge catch-up unexpectedly succeeded: %s\n' "$case_name" >&2
    exit 1
  fi
  grep -Fq 'gh workflow run edge.yml --ref main' "$TEST_TMP/err"
  assert_no_dispatch "$case_name"
done

# A missing or malformed built SHA fails before any GitHub call.
for bad_sha in "" 0123456 "0123456789abcdef0123456789abcdef0123456z"; do
  : > "$FAKE_GH_LOG"
  if BUILT_SHA="$bad_sha" bash .github/scripts/edge-catch-up.sh 2>/dev/null; then
    printf 'edge catch-up accepted a bad built SHA: %s\n' "$bad_sha" >&2
    exit 1
  fi
  if [[ -s "$FAKE_GH_LOG" ]]; then
    printf 'edge catch-up called gh with a bad built SHA: %s\n' "$bad_sha" >&2
    exit 1
  fi
done

# The workflow runs catch-up only after a successful publish, from its own job.
grep -Fq 'bash .github/scripts/edge-catch-up.sh' .github/workflows/edge.yml
grep -Fq "BUILT_SHA: \${{ needs.publish-edge-image.outputs.sha }}" .github/workflows/edge.yml

printf 'ok - edge catch-up builds the head of main once\n'
