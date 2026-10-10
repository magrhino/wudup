#!/usr/bin/env bash
set -euo pipefail

# Build both image variants for amd64 and arm64 from the checked-out commit and
# scan each with the release image policy, without pushing anything. Running
# this on a schedule reports new vulnerability data (for example a CVE in a
# bundled Go binary) before it blocks the next edge or release publish.
# Every image is scanned even after a failure so one run lists all of them.

# shellcheck source=.github/scripts/image-scan-policy.sh
source "$(dirname "${BASH_SOURCE[0]}")/image-scan-policy.sh"
scanner_image="$(image_scanner_ref)"

scan_dir="$(mktemp -d)"
trap 'rm -rf "$scan_dir"' EXIT

cache_args=()
if [[ -n "${BUILDX_CACHE_FROM:-}" ]]; then
  cache_args=(--cache-from "$BUILDX_CACHE_FROM")
fi

passed=()
failed=()
for variant in default trivy; do
  target_args=()
  [[ "$variant" == default ]] || target_args=(--target wudup-trivy)
  for platform in linux/amd64 linux/arm64; do
    name="$variant ($platform)"
    rm -f "$scan_dir/image.tar"
    printf 'Building %s\n' "$name"
    if ! docker buildx build --platform "$platform" \
      ${target_args[@]+"${target_args[@]}"} \
      --build-arg "APT_REFRESH=${APT_REFRESH:-scheduled-scan}" \
      ${cache_args[@]+"${cache_args[@]}"} \
      --output "type=docker,dest=$scan_dir/image.tar" \
      .; then
      printf 'Could not build %s, so it was not scanned. Fix the build error above and re-run the scan.\n' "$name" >&2
      failed+=("$name: build failed")
      continue
    fi
    printf 'Scanning %s\n' "$name"
    if scan_image_archive "$scanner_image" "$scan_dir" image.tar "$platform"; then
      passed+=("$name")
    else
      failed+=("$name: failed the image security policy or could not be scanned")
    fi
  done
done

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  {
    printf '## Scheduled image scan\n\n'
    for name in ${passed[@]+"${passed[@]}"}; do
      printf -- '- %s: passed\n' "$name"
    done
    for name in ${failed[@]+"${failed[@]}"}; do
      printf -- '- %s\n' "$name"
    done
  } >> "$GITHUB_STEP_SUMMARY"
fi

if (( ${#failed[@]} )); then
  printf '\nScheduled image scan found problems that will block the next edge or release publish:\n' >&2
  printf '  - %s\n' ${failed[@]+"${failed[@]}"} >&2
  printf 'Review the scan output above and update the affected dependency (often a Dockerfile base image), then re-run this workflow.\n' >&2
  exit 1
fi
printf 'All %s images passed the image security policy.\n' "${#passed[@]}"
