#!/usr/bin/env bash
set -euo pipefail

requested_variant="${1:-}"
case "$requested_variant" in
  all|default|trivy|check) ;;
  *)
    printf 'Usage: %s all|default|trivy|check\n' "$0" >&2
    exit 2
    ;;
esac

# Stable releases move vX.Y.Z, X.Y.Z, X.Y, and latest. Edge builds of main move
# only the edge-<sha> tag passed as RELEASE_TAG and, while the commit is still
# the head of main, edge. Re-running an edge build rebuilds edge-<sha>.
release_channel="${RELEASE_CHANNEL:-stable}"
case "$release_channel" in
  stable)
    staging_prefix="staging-"
    ;;
  edge)
    if [[ ! "${RELEASE_TAG:-}" =~ ^edge-[0-9a-f]{7,40}$ ]]; then
      printf 'Edge images need RELEASE_TAG formatted as edge-<commit sha>, got: %s\n' "${RELEASE_TAG:-}" >&2
      exit 2
    fi
    if [[ "${RELEASE_SHA:-}" != "${RELEASE_TAG#edge-}"* ]]; then
      printf 'Edge tag %s does not match commit %s; no images were built. Check the edge workflow inputs.\n' \
        "$RELEASE_TAG" "${RELEASE_SHA:-}" >&2
      exit 2
    fi
    # Keep edge staging separate so a release of the same commit cannot race it.
    staging_prefix="staging-edge-"
    ;;
  *)
    printf 'RELEASE_CHANNEL must be stable or edge, got: %s\n' "$release_channel" >&2
    exit 2
    ;;
esac

image="$REGISTRY/$IMAGE_NAME"

# A stable release is already published when its version tags hold images
# built from this commit for both platforms. Floating X.Y and latest tags are
# not checked because later releases move them.
release_already_published() {
  local suffix ref images
  for suffix in "$@"; do
    for ref in "$image:$RELEASE_TAG$suffix" "$image:$VERSION$suffix"; do
      if ! images="$(docker buildx imagetools inspect --format '{{json .Image}}' "$ref" 2>/dev/null)"; then
        printf 'Could not find %s in the registry (not published yet, or the registry could not be reached).\n' "$ref"
        return 1
      fi
      if ! jq -e --arg version "$RELEASE_TAG" --arg revision "$RELEASE_SHA" '
        (keys | sort) == ["linux/amd64", "linux/arm64"] and
        all(.[].config.Labels;
          ."org.opencontainers.image.version" == $version and
          ."org.opencontainers.image.revision" == $revision)
      ' <<<"$images" >/dev/null 2>&1; then
        printf '%s exists but does not hold linux/amd64 and linux/arm64 images built from %s.\n' "$ref" "$RELEASE_SHA"
        return 1
      fi
    done
  done
}

# Every stable release starts several publisher runs (tag push and Release
# Please dispatches). Only the first one builds; later runs leave the published
# digests alone. FORCE_REPUBLISH=true rebuilds anyway, e.g. for a security
# refresh. "check" exits 0 only when the release is already published.
if [[ "$release_channel" == stable ]]; then
  case "$requested_variant" in
    all|check) published_suffixes=("" "-trivy") ;;
    default) published_suffixes=("") ;;
    trivy) published_suffixes=("-trivy") ;;
  esac
  if [[ "${FORCE_REPUBLISH:-false}" == true ]]; then
    printf 'Forced re-publish requested: %s will be rebuilt and its tags moved to new image digests.\n' "$RELEASE_TAG"
    [[ "$requested_variant" != check ]] || exit 1
  elif release_already_published "${published_suffixes[@]}"; then
    printf '%s is already published from %s; skipping the image rebuild so its digests stay the same. To rebuild it on purpose, dispatch the release workflow with force_republish=true.\n' \
      "$RELEASE_TAG" "$RELEASE_SHA"
    exit 0
  elif [[ "$requested_variant" == check ]]; then
    exit 1
  fi
elif [[ "$requested_variant" == check ]]; then
  printf 'The published-release check only applies to stable releases; edge images are rebuilt on every run.\n' >&2
  exit 2
fi

# Use the same pinned scanner as the optional image, outside the scanned image.
scanner_image="$(sed -n 's/^FROM \(aquasec\/trivy:[^ ]*\) AS trivy$/\1/p' Dockerfile)"
if [[ ! "$scanner_image" =~ ^aquasec/trivy:[^@]+@sha256:[0-9a-f]{64}$ ]]; then
  printf 'Release scan needs a digest-pinned Trivy image in Dockerfile.\n' >&2
  exit 1
fi
scan_dir="$(mktemp -d)"
trap 'rm -rf "$scan_dir"' EXIT
verified_refs=()
verified_suffixes=()

stage_and_verify() {
  variant="$1"
  case "$variant" in
    default)
      suffix=""
      ;;
    trivy)
      suffix="-trivy"
      ;;
    *)
      printf 'Usage: %s default|trivy\n' "$0" >&2
      exit 2
      ;;
  esac

  image="$REGISTRY/$IMAGE_NAME"
  expected_platforms="linux/amd64 linux/arm64"
  staging_ref="$image:${staging_prefix}${RELEASE_SHA}${suffix}"
  label_args=(
    --label "org.opencontainers.image.source=https://github.com/$GITHUB_REPOSITORY"
    --label "org.opencontainers.image.revision=$RELEASE_SHA"
    --label "org.opencontainers.image.version=$RELEASE_TAG"
  )
  build_args=(--build-arg "APT_REFRESH=$APT_REFRESH")
  if [[ "$release_channel" == edge ]]; then
    build_args+=(--build-arg "WUDUP_BUILD_VERSION=$RELEASE_TAG")
  fi
  tag_args=(-t "$staging_ref")

  build_image() {
    local platform="$1"
    local output_arg="$2"
    local docker_args=(buildx build --platform "$platform")
    shift 2
    if [[ "$variant" == "trivy" ]]; then
      docker_args+=(--target wudup-trivy)
    fi

    docker "${docker_args[@]}" \
      "$@" \
      "${label_args[@]}" \
      "${build_args[@]}" \
      "${tag_args[@]}" \
      --cache-from "$BUILDX_CACHE_FROM" \
      --cache-to "$BUILDX_CACHE_TO" \
      "$output_arg" \
      .
  }

  build_image linux/amd64 --load
  bash tests/smoke-container-image.sh "$staging_ref"
  if [[ "$variant" == "trivy" ]]; then
    docker run --rm "$staging_ref" trivy --version
  fi

  build_image linux/amd64,linux/arm64 --push --provenance=false
  staging_digest="$(
    docker buildx imagetools inspect "$staging_ref" |
      sed -n 's/^Digest:[[:space:]]*//p'
  )"
  if [[ ! "$staging_digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    printf 'Could not resolve staged manifest digest for %s\n' "$staging_ref" >&2
    exit 1
  fi
  verified_ref="${staging_ref}@${staging_digest}"
  release_manifest="$(docker buildx imagetools inspect --raw "$verified_ref")"
  if ! jq -e '
    (.manifests | length) == 2 and
    ([.manifests[].platform | "\(.os)/\(.architecture)"] | sort) ==
      ["linux/amd64", "linux/arm64"]
  ' <<<"$release_manifest" >/dev/null; then
    printf 'Release blocked: staged image must contain exactly linux/amd64 and linux/arm64. Rebuild and retry.\n' >&2
    exit 1
  fi
  expected_trivy_version="$(sed -n 's/^FROM aquasec\/trivy:\([^@ ]*\).*/\1/p' Dockerfile)"
  for platform in $expected_platforms; do
    os="${platform%/*}"
    architecture="${platform#*/}"
    digest="$(
      jq -r \
        --arg os "$os" \
        --arg architecture "$architecture" \
        '.manifests[] | select(.platform.os == $os and .platform.architecture == $architecture) | .digest' \
        <<<"$release_manifest"
    )"
    if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      printf 'Could not resolve %s platform digest for %s\n' "$platform" "$staging_ref" >&2
      exit 1
    fi

    immutable_ref="${staging_ref}@${digest}"
    docker pull --platform "$platform" "$immutable_ref"
    labels="$(docker image inspect --format '{{json .Config.Labels}}' "$immutable_ref")"
    jq -e \
      --arg version "$RELEASE_TAG" \
      --arg revision "$RELEASE_SHA" \
      '."org.opencontainers.image.version" == $version and ."org.opencontainers.image.revision" == $revision' \
      <<<"$labels" >/dev/null

    # Export the exact pulled platform digest; no registry credentials or Docker
    # socket are exposed to the scanner. Reuse only its database cache.
    docker image save --platform "$platform" --output "$scan_dir/image.tar" "$immutable_ref"
    printf 'Scanning %s (%s): %s\n' "$variant" "$platform" "$immutable_ref"
    if ! docker run --rm --user "$(id -u):$(id -g)" --volume "$scan_dir:/scan" "$scanner_image" image \
      --input /scan/image.tar --platform "$platform" --cache-dir /scan/cache \
      --scanners vuln --pkg-types os,library --severity HIGH,CRITICAL \
      --ignore-unfixed=false --ignorefile /dev/null --exit-code 1 \
      --exit-on-eol 1 --timeout 10m --no-progress; then
      printf 'Release blocked: %s (%s) failed the image security policy or could not be scanned. Review the scan output, fix the image or scanner failure, and retry.\n' \
        "$variant" "$platform" >&2
      exit 1
    fi

    if [[ "$variant" == "trivy" ]]; then
      actual_trivy_version="$(
        docker run --rm --platform "$platform" "$immutable_ref" \
          trivy version --format json | jq -r '.Version'
      )"
      if [[ "$actual_trivy_version" != "$expected_trivy_version" ]]; then
        printf 'Expected Trivy %s for %s, got %s\n' \
          "$expected_trivy_version" "$platform" "$actual_trivy_version" >&2
        exit 1
      fi
    fi
  done

  verified_refs+=("$verified_ref")
  verified_suffixes+=("$suffix")
}

if [[ "$requested_variant" == all ]]; then
  stage_and_verify default
  stage_and_verify trivy
else
  stage_and_verify "$requested_variant"
fi

# Re-runs keep their original commit, so only move edge for the head of main;
# otherwise a re-run of an older commit would move edge backwards.
move_edge=1
if [[ "$release_channel" == edge ]]; then
  if ! main_sha="$(gh api "repos/$GITHUB_REPOSITORY/branches/main" --jq .commit.sha)" ||
    [[ ! "$main_sha" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'Edge publish stopped: could not confirm whether %s is still the head of main, so no edge tags were moved. Re-run the edge workflow.\n' \
      "$RELEASE_SHA" >&2
    exit 1
  fi
  if [[ "$main_sha" != "$RELEASE_SHA" ]]; then
    move_edge=0
    printf 'Leaving edge unchanged: %s is no longer the head of main (now %s). Publishing only %s.\n' \
      "$RELEASE_SHA" "$main_sha" "$RELEASE_TAG"
  fi
fi

# No production tag moves until every requested variant/platform passes.
# Stable releases move X.Y and latest first and the version tags last, so a run
# interrupted part-way leaves a version tag unset and the next run rebuilds
# instead of treating the release as already published.
production_tags=()
production_sources=()
for index in "${!verified_refs[@]}"; do
  verified_ref="${verified_refs[$index]}"
  suffix="${verified_suffixes[$index]}"
  if [[ "$release_channel" == edge ]]; then
    production_tags+=("$image:$RELEASE_TAG$suffix")
    production_sources+=("$verified_ref")
    if (( move_edge )); then
      production_tags+=("$image:edge$suffix")
      production_sources+=("$verified_ref")
    fi
  else
    production_tags+=("$image:$MINOR_VERSION$suffix" "$image:latest$suffix")
    production_sources+=("$verified_ref" "$verified_ref")
  fi
done
if [[ "$release_channel" == stable ]]; then
  for index in "${!verified_refs[@]}"; do
    suffix="${verified_suffixes[$index]}"
    production_tags+=("$image:$RELEASE_TAG$suffix" "$image:$VERSION$suffix")
    production_sources+=("${verified_refs[$index]}" "${verified_refs[$index]}")
  done
fi

for index in "${!production_tags[@]}"; do
  ref="${production_tags[$index]}"
  docker buildx imagetools create --tag "$ref" "${production_sources[$index]}"
  platforms="$(
    docker buildx imagetools inspect --raw "$ref" |
      jq -r '[.manifests[].platform | "\(.os)/\(.architecture)"] | sort | unique | join(" ")'
  )"
  if [[ "$platforms" != "$expected_platforms" ]]; then
    printf 'Expected %s to publish platforms "%s", got "%s"\n' "$ref" "$expected_platforms" "$platforms" >&2
    exit 1
  fi
done
