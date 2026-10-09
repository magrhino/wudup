#!/usr/bin/env bash
# Shared image security policy for the release/edge publisher and the scheduled
# image scan. Source this file from the repository root; it only defines
# functions. See SECURITY.md#release-image-policy.

# Print the digest-pinned Trivy scanner image from Dockerfile, so scans never
# use the binaries inside the image being scanned.
image_scanner_ref() {
  local scanner
  scanner="$(sed -n 's/^FROM \(aquasec\/trivy:[^ ]*\) AS trivy$/\1/p' Dockerfile)"
  if [[ ! "$scanner" =~ ^aquasec/trivy:[^@]+@sha256:[0-9a-f]{64}$ ]]; then
    printf 'Image scan needs a digest-pinned Trivy image in Dockerfile.\n' >&2
    return 1
  fi
  printf '%s\n' "$scanner"
}

# scan_image_archive SCANNER SCAN_DIR ARCHIVE PLATFORM
# Scan SCAN_DIR/ARCHIVE (a docker image archive) for PLATFORM. Only SCAN_DIR is
# mounted, with no registry credentials or Docker socket; SCAN_DIR/cache keeps
# the vulnerability database between scans. Fails on any HIGH or CRITICAL
# finding (fixed or not), an end-of-life OS, or a scanner error, except
# findings matched by an unexpired entry in .trivyignore.yaml (validated by
# scripts/check_trivyignore.py). Ignored findings are still printed.
scan_image_archive() {
  local scanner="$1" scan_dir="$2" archive="$3" platform="$4"
  if ! cp .trivyignore.yaml "$scan_dir/trivyignore.yaml"; then
    printf 'Image scan needs .trivyignore.yaml in the repository root (use "vulnerabilities: []" when nothing is ignored).\n' >&2
    return 1
  fi
  docker run --rm --user "$(id -u):$(id -g)" --volume "$scan_dir:/scan" "$scanner" image \
    --input "/scan/$archive" --platform "$platform" --cache-dir /scan/cache \
    --scanners vuln --pkg-types os,library --severity HIGH,CRITICAL \
    --ignore-unfixed=false --ignorefile /scan/trivyignore.yaml --show-suppressed \
    --exit-code 1 --exit-on-eol 1 --timeout 10m --no-progress
}
