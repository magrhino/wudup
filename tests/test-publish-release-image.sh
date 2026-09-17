#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/wud-release-image-test.XXXXXX")"
trap 'rm -rf "$TEST_TMP"' EXIT
REAL_BASH="$(command -v bash)"

cat > "$TEST_TMP/bash" <<'FAKE_BASH'
#!/bin/bash
if [[ "${1:-}" == "tests/smoke-container-image.sh" ]]; then
  exit 0
fi
exec "$REAL_BASH" "$@"
FAKE_BASH
chmod +x "$TEST_TMP/bash"

cat > "$TEST_TMP/docker" <<'FAKE_DOCKER'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"

if [[ "$*" == "image save --platform "* ]]; then
  printf '%s\n' "${@: -1}" > "$FAKE_SCAN_TARGET"
elif [[ "$*" == *"--scanners vuln"* ]]; then
  target="$(cat "$FAKE_SCAN_TARGET")"
  variant=default
  [[ "$target" != *-trivy@* ]] || variant=trivy
  if [[ "$variant" == "${FAIL_SCAN_VARIANT:-}" && "$*" == *"--platform ${FAIL_SCAN_PLATFORM:-none} "* ]]; then
    exit "${FAIL_SCAN_CODE:-1}"
  fi
elif [[ "${FAIL_ARM64_VERIFY:-0}" == "1" && "$*" == "run --rm --platform linux/arm64 "* ]]; then
  exit 42
elif [[ "$*" == "buildx imagetools inspect --raw "* ]]; then
  if [[ "${BAD_MANIFEST:-0}" == 1 ]]; then
    printf '{"manifests":[{"digest":"sha256:%064d","platform":{"os":"linux","architecture":"amd64"}}]}\n' 1
    exit 0
  fi
  printf '{"manifests":[{"digest":"sha256:%064d","platform":{"os":"linux","architecture":"amd64"}},{"digest":"sha256:%064d","platform":{"os":"linux","architecture":"arm64"}}]}\n' 1 2
elif [[ "$*" == "buildx imagetools inspect "* ]]; then
  printf 'Name: test\nDigest: sha256:%064d\n' 3
elif [[ "$*" == "image inspect --format "* ]]; then
  printf '{"org.opencontainers.image.version":"%s","org.opencontainers.image.revision":"%s"}\n' "$RELEASE_TAG" "$RELEASE_SHA"
elif [[ "$*" == *"trivy version --format json"* ]]; then
  printf '{"Version":"%s"}\n' "$EXPECTED_TRIVY_VERSION"
fi
FAKE_DOCKER
chmod +x "$TEST_TMP/docker"

export PATH="$TEST_TMP:$PATH"
export REAL_BASH
export FAKE_DOCKER_LOG="$TEST_TMP/docker.log"
export FAKE_SCAN_TARGET="$TEST_TMP/scan-target"
export REGISTRY="ghcr.io"
export IMAGE_NAME="magrhino/wudup"
export RELEASE_TAG="v1.2.3"
export VERSION="1.2.3"
export MINOR_VERSION="1.2"
export RELEASE_SHA="0123456789abcdef0123456789abcdef01234567"
export APT_REFRESH="workflow-123-attempt-1"
export GITHUB_REPOSITORY="magrhino/wudup"
export BUILDX_CACHE_FROM="type=gha,scope=test"
export BUILDX_CACHE_TO="type=gha,scope=test,mode=max"

cd "$REPO_ROOT"
EXPECTED_TRIVY_VERSION="$(sed -n 's/^FROM aquasec\/trivy:\([^@ ]*\).*/\1/p' Dockerfile)"
export EXPECTED_TRIVY_VERSION
if [[ -z "$EXPECTED_TRIVY_VERSION" ]]; then
  printf 'could not derive the Trivy version from Dockerfile\n' >&2
  exit 1
fi
bash .github/scripts/publish-release-image.sh trivy

grep -Fq -- "--build-arg APT_REFRESH=workflow-123-attempt-1" "$FAKE_DOCKER_LOG"
staging_ref="ghcr.io/magrhino/wudup:staging-${RELEASE_SHA}-trivy"
grep -Fq -- "pull --platform linux/amd64 ${staging_ref}@sha256:" "$FAKE_DOCKER_LOG"
grep -Fq -- "pull --platform linux/arm64 ${staging_ref}@sha256:" "$FAKE_DOCKER_LOG"
grep -Fq -- "buildx imagetools inspect --raw ${staging_ref}@sha256:" "$FAKE_DOCKER_LOG"
grep -Fq -- "run --rm --platform linux/amd64" "$FAKE_DOCKER_LOG"
grep -Fq -- "run --rm --platform linux/arm64" "$FAKE_DOCKER_LOG"
grep -Fq -- "--scanners vuln --pkg-types os,library --severity HIGH,CRITICAL --ignore-unfixed=false --ignorefile /dev/null --exit-code 1 --exit-on-eol 1" "$FAKE_DOCKER_LOG"
grep -Fq -- "buildx imagetools create --tag ghcr.io/magrhino/wudup:v1.2.3-trivy ${staging_ref}@sha256:" "$FAKE_DOCKER_LOG"

push_line="$(grep -n -m1 -- '--push' "$FAKE_DOCKER_LOG" | cut -d: -f1)"
verify_line="$(grep -n -m1 -- 'pull --platform linux/amd64' "$FAKE_DOCKER_LOG" | cut -d: -f1)"
promote_line="$(grep -n -m1 -- 'imagetools create --tag' "$FAKE_DOCKER_LOG" | cut -d: -f1)"
if (( push_line >= verify_line || verify_line >= promote_line )); then
  printf 'release image was not staged, verified, then promoted\n' >&2
  exit 1
fi

: > "$FAKE_DOCKER_LOG"
export FAIL_ARM64_VERIFY=1
if bash .github/scripts/publish-release-image.sh trivy; then
  printf 'release image verification failure unexpectedly succeeded\n' >&2
  exit 1
fi
if grep -Fq -- 'imagetools create --tag' "$FAKE_DOCKER_LOG"; then
  printf 'failed release image was promoted to production tags\n' >&2
  exit 1
fi
unset FAIL_ARM64_VERIFY

: > "$FAKE_DOCKER_LOG"
export APT_REFRESH="workflow-123-attempt-2"
bash .github/scripts/publish-release-image.sh trivy
grep -Fq -- "--build-arg APT_REFRESH=workflow-123-attempt-2" "$FAKE_DOCKER_LOG"
if grep -Fq -- "--build-arg APT_REFRESH=workflow-123-attempt-1" "$FAKE_DOCKER_LOG"; then
  printf 'release retry reused the previous apt freshness token\n' >&2
  exit 1
fi

: > "$FAKE_DOCKER_LOG"
bash .github/scripts/publish-release-image.sh default
default_staging_ref="ghcr.io/magrhino/wudup:staging-${RELEASE_SHA}"
grep -Fq -- "pull --platform linux/amd64 ${default_staging_ref}@sha256:" "$FAKE_DOCKER_LOG"
grep -Fq -- "pull --platform linux/arm64 ${default_staging_ref}@sha256:" "$FAKE_DOCKER_LOG"
grep -Fq -- "buildx imagetools create --tag ghcr.io/magrhino/wudup:v1.2.3 ${default_staging_ref}@sha256:" "$FAKE_DOCKER_LOG"
if grep -Fq -- "--target wudup-trivy" "$FAKE_DOCKER_LOG"; then
  printf 'default release image unexpectedly used the Trivy target\n' >&2
  exit 1
fi

# The workflow must use the shared barrier before publishing a GitHub release.
grep -Fq 'bash .github/scripts/publish-release-image.sh all' .github/workflows/release.yml
if grep -Eq 'publish-release-image.sh (default|trivy)' .github/workflows/release.yml; then
  printf 'workflow bypasses the shared image scan barrier\n' >&2
  exit 1
fi

: > "$FAKE_DOCKER_LOG"
bash .github/scripts/publish-release-image.sh all
[[ "$(grep -c -- '--scanners vuln' "$FAKE_DOCKER_LOG")" == 4 ]]
[[ "$(grep -c -- 'imagetools create --tag' "$FAKE_DOCKER_LOG")" == 8 ]]
last_scan="$(grep -n -- '--scanners vuln' "$FAKE_DOCKER_LOG" | tail -1 | cut -d: -f1)"
first_promote="$(grep -n -m1 -- 'imagetools create --tag' "$FAKE_DOCKER_LOG" | cut -d: -f1)"
(( last_scan < first_promote ))
scanner_image="$(sed -n 's/^FROM \(aquasec\/trivy:[^ ]*\) AS trivy$/\1/p' Dockerfile)"
grep -Fq -- "$scanner_image image --input /scan/image.tar" "$FAKE_DOCKER_LOG"
for ref in "$default_staging_ref" "$staging_ref"; do
  for digest_number in 1 2; do
    printf -v digest 'sha256:%064d' "$digest_number"
    grep -Eq -- "image save --platform linux/(amd64|arm64) --output [^ ]+/image.tar $ref@$digest$" "$FAKE_DOCKER_LOG"
  done
done

# Findings and scanner/runtime errors on any of the four images block all tags.
for variant in default trivy; do
  for platform in linux/amd64 linux/arm64; do
    for code in 1 42; do
      : > "$FAKE_DOCKER_LOG"
      if FAIL_SCAN_VARIANT="$variant" FAIL_SCAN_PLATFORM="$platform" FAIL_SCAN_CODE="$code" \
        bash .github/scripts/publish-release-image.sh all; then
        printf 'failed scan unexpectedly succeeded: %s %s exit %s\n' "$variant" "$platform" "$code" >&2
        exit 1
      fi
      if grep -Fq -- 'imagetools create --tag' "$FAKE_DOCKER_LOG"; then
        printf 'release tags changed after a failed scan\n' >&2
        exit 1
      fi
    done
  done
done

: > "$FAKE_DOCKER_LOG"
if BAD_MANIFEST=1 bash .github/scripts/publish-release-image.sh all; then
  printf 'incomplete platform manifest unexpectedly succeeded\n' >&2
  exit 1
fi
if grep -Fq -- 'imagetools create --tag' "$FAKE_DOCKER_LOG"; then
  printf 'incomplete platform manifest was promoted\n' >&2
  exit 1
fi

printf 'ok - release image freshness, immutable scans, and publication barrier\n'
