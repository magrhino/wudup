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
elif [[ "$*" == "buildx imagetools create --tag "*":${FAIL_CREATE_TAG:-none} "* ]]; then
  exit 1
elif [[ "$*" == "buildx imagetools inspect --format "* ]]; then
  # Already-published lookup: only answer for tags marked as published.
  ref="${@: -1}"
  if [[ -z "${FAKE_PUBLISHED_REVISION:-}" || "$ref" == *"${FAKE_MISSING_REF:-none}" ]]; then
    exit 1
  fi
  labels="$(printf '{"org.opencontainers.image.version":"%s","org.opencontainers.image.revision":"%s"}' \
    "$RELEASE_TAG" "$FAKE_PUBLISHED_REVISION")"
  printf '{"linux/amd64":{"config":{"Labels":%s}},"linux/arm64":{"config":{"Labels":%s}}}\n' "$labels" "$labels"
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

cat > "$TEST_TMP/gh" <<'FAKE_GH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_GH_LOG"
[[ "${FAKE_GH_FAIL:-0}" != 1 ]] || exit 1
printf '%s\n' "${FAKE_MAIN_SHA:-}"
FAKE_GH
chmod +x "$TEST_TMP/gh"

export PATH="$TEST_TMP:$PATH"
export FAKE_GH_LOG="$TEST_TMP/gh.log"
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
grep -Fq -- "--scanners vuln --pkg-types os,library --severity HIGH,CRITICAL --ignore-unfixed=false --ignorefile /scan/trivyignore.yaml --show-suppressed --exit-code 1 --exit-on-eol 1" "$FAKE_DOCKER_LOG"
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

# Version tags, which the already-published check reads, move last; X.Y and
# latest move first. A run that stops part-way through promotion leaves the
# version tags unset, so the next run rebuilds instead of skipping.
: > "$FAKE_DOCKER_LOG"
bash .github/scripts/publish-release-image.sh all > /dev/null
promoted_tags="$(sed -n 's/^buildx imagetools create --tag ghcr.io\/magrhino\/wudup:\([^ ]*\) .*/\1/p' "$FAKE_DOCKER_LOG")"
expected_promoted_tags="$(printf '%s\n' 1.2 latest 1.2-trivy latest-trivy v1.2.3 1.2.3 v1.2.3-trivy 1.2.3-trivy)"
if [[ "$promoted_tags" != "$expected_promoted_tags" ]]; then
  printf 'stable tags were promoted in an unexpected order:\n%s\n' "$promoted_tags" >&2
  exit 1
fi
for failed_tag in 1.2-trivy latest-trivy; do
  : > "$FAKE_DOCKER_LOG"
  if FAIL_CREATE_TAG="$failed_tag" bash .github/scripts/publish-release-image.sh all > /dev/null 2>&1; then
    printf 'failed promotion of %s unexpectedly succeeded\n' "$failed_tag" >&2
    exit 1
  fi
  if grep -Eq -- '^buildx imagetools create --tag ghcr.io/magrhino/wudup:v?1\.2\.3(-trivy)? ' "$FAKE_DOCKER_LOG"; then
    printf 'version tags moved although promotion stopped at %s\n' "$failed_tag" >&2
    exit 1
  fi
done

# A release whose version tags already hold this commit is not rebuilt, so
# duplicate publisher runs leave its digests alone.
: > "$FAKE_DOCKER_LOG"
FAKE_PUBLISHED_REVISION="$RELEASE_SHA" bash .github/scripts/publish-release-image.sh check
FAKE_PUBLISHED_REVISION="$RELEASE_SHA" bash .github/scripts/publish-release-image.sh all > "$TEST_TMP/published.out"
grep -Fq 'v1.2.3 is already published from' "$TEST_TMP/published.out"
for ref in v1.2.3 1.2.3 v1.2.3-trivy 1.2.3-trivy; do
  grep -Fxq -- "buildx imagetools inspect --format {{json .Image}} ghcr.io/magrhino/wudup:$ref" "$FAKE_DOCKER_LOG"
done
if grep -Eq -- '^(buildx build|run |pull |buildx imagetools create)' "$FAKE_DOCKER_LOG"; then
  printf 'already-published release was rebuilt or re-promoted\n' >&2
  exit 1
fi

# A different commit, or a missing version tag, still publishes.
for published in "revision:$(printf 'f%.0s' {1..40})" missing:1.2.3-trivy missing:v1.2.3; do
  : > "$FAKE_DOCKER_LOG"
  revision="$RELEASE_SHA"
  missing=none
  if [[ "${published%%:*}" == revision ]]; then
    revision="${published#*:}"
  else
    missing="${published#*:}"
  fi
  if FAKE_PUBLISHED_REVISION="$revision" FAKE_MISSING_REF="$missing" \
    bash .github/scripts/publish-release-image.sh check > /dev/null; then
    printf 'unpublished release reported as published: %s\n' "$published" >&2
    exit 1
  fi
  FAKE_PUBLISHED_REVISION="$revision" FAKE_MISSING_REF="$missing" \
    bash .github/scripts/publish-release-image.sh all > /dev/null
  [[ "$(grep -c -- 'imagetools create --tag' "$FAKE_DOCKER_LOG")" == 8 ]]
done

# A forced re-publish rebuilds and re-promotes an already-published release.
: > "$FAKE_DOCKER_LOG"
if FORCE_REPUBLISH=true FAKE_PUBLISHED_REVISION="$RELEASE_SHA" \
  bash .github/scripts/publish-release-image.sh check > /dev/null; then
  printf 'forced re-publish was reported as already published\n' >&2
  exit 1
fi
FORCE_REPUBLISH=true FAKE_PUBLISHED_REVISION="$RELEASE_SHA" \
  bash .github/scripts/publish-release-image.sh all > "$TEST_TMP/forced.out"
grep -Fq 'Forced re-publish requested: v1.2.3' "$TEST_TMP/forced.out"
[[ "$(grep -c -- '--scanners vuln' "$FAKE_DOCKER_LOG")" == 4 ]]
[[ "$(grep -c -- 'imagetools create --tag' "$FAKE_DOCKER_LOG")" == 8 ]]

# Stable releases never bake an edge build version into the image.
if grep -Fq -- "WUDUP_BUILD_VERSION" "$FAKE_DOCKER_LOG"; then
  printf 'stable release build unexpectedly set WUDUP_BUILD_VERSION\n' >&2
  exit 1
fi

# Stable releases never look up the head of main.
if [[ -s "$FAKE_GH_LOG" ]]; then
  printf 'stable release publish unexpectedly called gh\n' >&2
  exit 1
fi

# Edge builds of main move only edge tags and never touch stable release tags.
: > "$FAKE_DOCKER_LOG"
env -u VERSION -u MINOR_VERSION RELEASE_CHANNEL=edge RELEASE_TAG=edge-0123456 \
  FAKE_MAIN_SHA="$RELEASE_SHA" bash .github/scripts/publish-release-image.sh all
grep -Fxq 'api repos/magrhino/wudup/branches/main --jq .commit.sha' "$FAKE_GH_LOG"
edge_staging_ref="ghcr.io/magrhino/wudup:staging-edge-${RELEASE_SHA}"
grep -Fq -- "-t ${edge_staging_ref} " "$FAKE_DOCKER_LOG"
grep -Fq -- "-t ${edge_staging_ref}-trivy " "$FAKE_DOCKER_LOG"
grep -Fq -- "--label org.opencontainers.image.version=edge-0123456" "$FAKE_DOCKER_LOG"
grep -Fq -- "--build-arg WUDUP_BUILD_VERSION=edge-0123456" "$FAKE_DOCKER_LOG"
[[ "$(grep -c -- '--scanners vuln' "$FAKE_DOCKER_LOG")" == 4 ]]
expected_edge_tags="$(printf '%s\n' \
  ghcr.io/magrhino/wudup:edge \
  ghcr.io/magrhino/wudup:edge-0123456 \
  ghcr.io/magrhino/wudup:edge-0123456-trivy \
  ghcr.io/magrhino/wudup:edge-trivy)"
actual_edge_tags="$(sed -n 's/^buildx imagetools create --tag \([^ ]*\) .*/\1/p' "$FAKE_DOCKER_LOG" | sort)"
if [[ "$actual_edge_tags" != "$expected_edge_tags" ]]; then
  printf 'edge publish moved unexpected tags:\n%s\n' "$actual_edge_tags" >&2
  exit 1
fi
last_scan="$(grep -n -- '--scanners vuln' "$FAKE_DOCKER_LOG" | tail -1 | cut -d: -f1)"
first_promote="$(grep -n -m1 -- 'imagetools create --tag' "$FAKE_DOCKER_LOG" | cut -d: -f1)"
(( last_scan < first_promote ))

# Edge re-runs always rebuild edge-<sha>, even if its tags already exist.
: > "$FAKE_DOCKER_LOG"
RELEASE_CHANNEL=edge RELEASE_TAG=edge-0123456 FAKE_MAIN_SHA="$RELEASE_SHA" FAKE_PUBLISHED_REVISION="$RELEASE_SHA" \
  bash .github/scripts/publish-release-image.sh all > /dev/null
[[ "$(grep -c -- '--scanners vuln' "$FAKE_DOCKER_LOG")" == 4 ]]
if grep -Fq -- 'imagetools inspect --format' "$FAKE_DOCKER_LOG"; then
  printf 'edge publish checked for an already-published release\n' >&2
  exit 1
fi
if RELEASE_CHANNEL=edge RELEASE_TAG=edge-0123456 bash .github/scripts/publish-release-image.sh check 2>/dev/null; then
  printf 'edge channel unexpectedly reported as already published\n' >&2
  exit 1
fi

: > "$FAKE_DOCKER_LOG"
: > "$FAKE_GH_LOG"
if FAIL_SCAN_VARIANT=trivy FAIL_SCAN_PLATFORM=linux/arm64 RELEASE_CHANNEL=edge RELEASE_TAG=edge-0123456 \
  FAKE_MAIN_SHA="$RELEASE_SHA" bash .github/scripts/publish-release-image.sh all; then
  printf 'failed edge scan unexpectedly succeeded\n' >&2
  exit 1
fi
if grep -Fq -- 'imagetools create --tag' "$FAKE_DOCKER_LOG"; then
  printf 'edge tags changed after a failed scan\n' >&2
  exit 1
fi
if [[ -s "$FAKE_GH_LOG" ]]; then
  printf 'edge publish reached promotion after a failed scan\n' >&2
  exit 1
fi

# Re-running an older commit publishes edge-<sha> but never moves edge back.
: > "$FAKE_DOCKER_LOG"
RELEASE_CHANNEL=edge RELEASE_TAG=edge-0123456 FAKE_MAIN_SHA="$(printf 'f%.0s' {1..40})" \
  bash .github/scripts/publish-release-image.sh all > "$TEST_TMP/edge-stale.out"
grep -Fq "Leaving edge unchanged: ${RELEASE_SHA} is no longer the head of main" "$TEST_TMP/edge-stale.out"
stale_edge_tags="$(sed -n 's/^buildx imagetools create --tag \([^ ]*\) .*/\1/p' "$FAKE_DOCKER_LOG" | sort)"
expected_stale_tags="$(printf '%s\n' \
  ghcr.io/magrhino/wudup:edge-0123456 \
  ghcr.io/magrhino/wudup:edge-0123456-trivy)"
if [[ "$stale_edge_tags" != "$expected_stale_tags" ]]; then
  printf 'edge publish of an older commit moved unexpected tags:\n%s\n' "$stale_edge_tags" >&2
  exit 1
fi

# If the head of main cannot be confirmed (lookup failure or malformed SHA),
# no edge tags move.
for main_lookup in fail:x ok:null ok:0123456 ok:; do
  : > "$FAKE_DOCKER_LOG"
  gh_fail=0
  [[ "${main_lookup%%:*}" != fail ]] || gh_fail=1
  if RELEASE_CHANNEL=edge RELEASE_TAG=edge-0123456 FAKE_GH_FAIL="$gh_fail" FAKE_MAIN_SHA="${main_lookup#*:}" \
    bash .github/scripts/publish-release-image.sh all 2> "$TEST_TMP/edge-unconfirmed.err"; then
    printf 'edge publish without a confirmed main head unexpectedly succeeded: %s\n' "$main_lookup" >&2
    exit 1
  fi
  grep -Fq 'could not confirm whether' "$TEST_TMP/edge-unconfirmed.err"
  grep -Fq 'no edge tags were moved. Re-run the edge workflow.' "$TEST_TMP/edge-unconfirmed.err"
  if grep -Fq -- 'imagetools create --tag' "$FAKE_DOCKER_LOG"; then
    printf 'edge tags changed without a confirmed main head: %s\n' "$main_lookup" >&2
    exit 1
  fi
done

for bad_edge in edge:v1.2.3 edge:edge-fedcba9 nightly:edge-0123456; do
  : > "$FAKE_DOCKER_LOG"
  if RELEASE_CHANNEL="${bad_edge%%:*}" RELEASE_TAG="${bad_edge#*:}" \
    bash .github/scripts/publish-release-image.sh all 2>/dev/null; then
    printf 'invalid edge publish settings unexpectedly succeeded: %s\n' "$bad_edge" >&2
    exit 1
  fi
  if [[ -s "$FAKE_DOCKER_LOG" ]]; then
    printf 'invalid edge publish settings ran docker: %s\n' "$bad_edge" >&2
    exit 1
  fi
done

# All runs for one release tag share a queue, and duplicates skip validation
# and publishing once the release is published.
grep -Fq "group: release-\${{ inputs.release_tag || github.ref_name }}" .github/workflows/release.yml
grep -Fq 'bash .github/scripts/publish-release-image.sh check' .github/workflows/release.yml
grep -Fq 'The check published images job finished with %s, so release validations did not run' .github/workflows/release.yml
[[ "$(grep -c "FORCE_REPUBLISH: \${{ inputs.force_republish }}" .github/workflows/release.yml)" == 2 ]]
[[ "$(grep -c "if: \${{ needs.check-published.outputs.published != 'true' }}" .github/workflows/release.yml)" == 6 ]]

grep -Fq 'RELEASE_CHANNEL: edge' .github/workflows/edge.yml
grep -Fq "GH_TOKEN: \${{ github.token }}" .github/workflows/edge.yml
grep -Fq 'bash .github/scripts/publish-release-image.sh all' .github/workflows/edge.yml

printf 'ok - release image freshness, immutable scans, and publication barrier\n'
