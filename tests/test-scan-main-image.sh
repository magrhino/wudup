#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/wud-scan-main-image-test.XXXXXX")"
trap 'rm -rf "$TEST_TMP"' EXIT

cat > "$TEST_TMP/docker" <<'FAKE_DOCKER'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"

if [[ "$*" == "buildx build "* ]]; then
  variant=default
  [[ "$*" != *"--target wudup-trivy"* ]] || variant=trivy
  platform="${4}"
  printf '%s %s\n' "$variant" "$platform" > "$FAKE_BUILT"
  [[ "$variant $platform" != "${FAIL_BUILD:-none}" ]] || exit 1
  dest="$(sed -n 's/.*--output type=docker,dest=\([^ ]*\).*/\1/p' <<<"$*")"
  : > "$dest"
elif [[ "$*" == *" image --input "* ]]; then
  [[ "$(cat "$FAKE_BUILT")" != "${FAIL_SCAN:-none}" ]] || exit 1
fi
FAKE_DOCKER
chmod +x "$TEST_TMP/docker"

export PATH="$TEST_TMP:$PATH"
export FAKE_DOCKER_LOG="$TEST_TMP/docker.log"
export FAKE_BUILT="$TEST_TMP/built"
export BUILDX_CACHE_FROM="type=gha,scope=test"
export APT_REFRESH="workflow-123-attempt-1"
export GITHUB_STEP_SUMMARY="$TEST_TMP/summary.md"

cd "$REPO_ROOT"
scanner_image="$(sed -n 's/^FROM \(aquasec\/trivy:[^ ]*\) AS trivy$/\1/p' Dockerfile)"

reset() {
  : > "$FAKE_DOCKER_LOG"
  : > "$GITHUB_STEP_SUMMARY"
}

count() {
  grep -c -- "$1" "$FAKE_DOCKER_LOG" || true
}

# All four images build locally and are scanned with the release policy.
reset
bash .github/scripts/scan-main-image.sh > "$TEST_TMP/out" 2>&1
[[ "$(count '^buildx build ')" == 4 ]]
[[ "$(count '--target wudup-trivy')" == 2 ]]
[[ "$(count '--scanners vuln --pkg-types os,library --severity HIGH,CRITICAL --ignore-unfixed=false --ignorefile /dev/null --exit-code 1 --exit-on-eol 1')" == 4 ]]
[[ "$(count "$scanner_image image --input /scan/image.tar")" == 4 ]]
for platform in linux/amd64 linux/arm64; do
  [[ "$(count "^buildx build --platform $platform ")" == 2 ]]
  [[ "$(count " --platform $platform --cache-dir")" == 2 ]]
done
[[ "$(count '--build-arg APT_REFRESH=workflow-123-attempt-1 --cache-from type=gha,scope=test --output type=docker,dest=')" == 4 ]]
if grep -Eq -- '--push|--cache-to|imagetools create' "$FAKE_DOCKER_LOG"; then
  printf 'scheduled scan pushed images or wrote the build cache\n' >&2
  exit 1
fi
[[ "$(grep -c ': passed$' "$GITHUB_STEP_SUMMARY")" == 4 ]]
grep -Fq 'All 4 images passed' "$TEST_TMP/out"

# A finding on one image fails the run, names it, and still scans the rest.
reset
if FAIL_SCAN="trivy linux/arm64" bash .github/scripts/scan-main-image.sh > "$TEST_TMP/out" 2>&1; then
  printf 'failed scan unexpectedly succeeded\n' >&2
  exit 1
fi
[[ "$(count '--scanners vuln')" == 4 ]]
grep -Fq 'trivy (linux/arm64): failed the image security policy' "$TEST_TMP/out"
grep -Fq 'trivy (linux/arm64): failed the image security policy' "$GITHUB_STEP_SUMMARY"
[[ "$(grep -c ': passed$' "$GITHUB_STEP_SUMMARY")" == 3 ]]

# A build failure is reported as a failure, not skipped silently.
reset
if FAIL_BUILD="default linux/amd64" bash .github/scripts/scan-main-image.sh > "$TEST_TMP/out" 2>&1; then
  printf 'failed build unexpectedly succeeded\n' >&2
  exit 1
fi
[[ "$(count '--scanners vuln')" == 3 ]]
grep -Fq 'default (linux/amd64): build failed' "$TEST_TMP/out"

# The workflow runs on a schedule, uses this script, and cannot publish.
workflow=.github/workflows/image-scan.yml
grep -Fq 'bash .github/scripts/scan-main-image.sh' "$workflow"
grep -Eq '^  schedule:' "$workflow"
if grep -Eq 'packages: write|BUILDX_CACHE_TO|publish-release-image' "$workflow"; then
  printf 'scheduled scan workflow can publish images or write the build cache\n' >&2
  exit 1
fi

printf 'ok - scheduled image scan builds locally and applies the release scan policy\n'
