#!/usr/bin/env bash
set -euo pipefail

requested_variant="${1:-}"
case "$requested_variant" in
  all|default|trivy) ;;
  *)
    printf 'Usage: %s all|default|trivy\n' "$0" >&2
    exit 2
    ;;
esac

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
  staging_ref="$image:staging-${RELEASE_SHA}${suffix}"
  label_args=(
    --label "org.opencontainers.image.source=https://github.com/$GITHUB_REPOSITORY"
    --label "org.opencontainers.image.revision=$RELEASE_SHA"
    --label "org.opencontainers.image.version=$RELEASE_TAG"
  )
  build_args=(--build-arg "APT_REFRESH=$APT_REFRESH")
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

# No production tag moves until every requested variant/platform passes.
for index in "${!verified_refs[@]}"; do
  verified_ref="${verified_refs[$index]}"
  suffix="${verified_suffixes[$index]}"
  production_tags=(
    "$image:$RELEASE_TAG$suffix"
    "$image:$VERSION$suffix"
    "$image:$MINOR_VERSION$suffix"
    "$image:latest$suffix"
  )

  for ref in "${production_tags[@]}"; do
    docker buildx imagetools create --tag "$ref" "$verified_ref"
    platforms="$(
      docker buildx imagetools inspect --raw "$ref" |
        jq -r '[.manifests[].platform | "\(.os)/\(.architecture)"] | sort | unique | join(" ")'
    )"
    if [[ "$platforms" != "$expected_platforms" ]]; then
      printf 'Expected %s to publish platforms "%s", got "%s"\n' "$ref" "$expected_platforms" "$platforms" >&2
      exit 1
    fi
  done
done
