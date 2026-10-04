#!/usr/bin/env bash
set -euo pipefail

# Keep the newest EDGE_KEEP_BUILDS edge builds of main and delete the package
# versions of older ones, with their platform manifests. A version is pruned
# only when every tag on it is an edge-<sha> or staging-edge-<sha> tag, so the
# versions behind edge, edge-trivy, stable release tags, and stable staging-*
# tags are never deleted. DRY_RUN=1 lists what would be deleted.
keep_builds="${EDGE_KEEP_BUILDS:-50}"
if [[ ! "$keep_builds" =~ ^[1-9][0-9]*$ ]]; then
  printf 'EDGE_KEEP_BUILDS must be a positive whole number, got: %s\n' "$keep_builds" >&2
  exit 2
fi
dry_run="${DRY_RUN:-0}"
if [[ ! "$IMAGE_NAME" =~ ^[^/]+/[^/]+$ ]]; then
  printf 'IMAGE_NAME must be <owner>/<package>, got: %s\n' "$IMAGE_NAME" >&2
  exit 2
fi
versions_path="users/${IMAGE_NAME%%/*}/packages/container/${IMAGE_NAME#*/}/versions"

if ! versions="$(gh api --paginate "$versions_path?per_page=100" --jq '.[]' | jq -s .)"; then
  printf 'Edge cleanup stopped: could not list the %s package versions, so nothing was deleted. Re-run the edge workflow, or check that its token can read packages.\n' \
    "$IMAGE_NAME" >&2
  exit 1
fi

# One build is the default and -trivy images of one commit, plus any staged
# image that failed its scan. Builds are ordered by their newest version.
prune_plan="$(
  jq -r --argjson keep "$keep_builds" '
    def edge_tag: "^(staging-edge-[0-9a-f]{40}|edge-[0-9a-f]{7,40})(-trivy)?$";
    [ .[]
      | .metadata.container.tags as $tags
      | select(($tags | length) > 0 and all($tags[]; test(edge_tag)))
      | {id, digest: .name, created_at, tags: ($tags | join(",")),
         build: ($tags[0] | capture("edge-(?<sha>[0-9a-f]{7})").sha)} ]
    | group_by(.build)
    | sort_by(map(.created_at) | max)
    | reverse
    | .[$keep:]
    | flatten[]
    | [.id, .digest, .tags]
    | @tsv
  ' <<<"$versions"
)"

if [[ -z "$prune_plan" ]]; then
  printf 'No edge builds older than the newest %s to delete.\n' "$keep_builds"
  exit 0
fi

delete_version() {
  local id="$1"
  local label="$2"
  if [[ "$dry_run" == 1 ]]; then
    printf 'Would delete %s (version %s)\n' "$label" "$id"
    return
  fi
  if ! gh api --method DELETE "$versions_path/$id" >/dev/null; then
    printf 'Edge cleanup stopped: could not delete %s (version %s). The edge workflow token needs the Admin role on the %s package: in the package settings, open Manage Actions access and give this repository Admin. Versions deleted so far can be restored from the package settings for 30 days.\n' \
      "$label" "$id" "$IMAGE_NAME" >&2
    exit 1
  fi
  printf 'Deleted %s (version %s)\n' "$label" "$id"
}

deleted=0
while IFS=$'\t' read -r id digest tags; do
  if ! manifest="$(docker buildx imagetools inspect --raw "$REGISTRY/$IMAGE_NAME@$digest")"; then
    printf 'Edge cleanup stopped: could not read the manifest of %s (%s), so its platform images were not found and it was not deleted. Re-run the edge workflow.\n' \
      "$tags" "$digest" >&2
    exit 1
  fi

  # Look up the platform manifests before deleting the index that lists them.
  # Edge images carry their own edge-<sha> version label, so these are never
  # shared with a stable release; skip any that GHCR shows as tagged anyway.
  child_ids=()
  while IFS= read -r child; do
    child_id="$(
      jq -r --arg digest "$child" '
        .[] | select(.name == $digest and (.metadata.container.tags | length) == 0) | .id
      ' <<<"$versions"
    )"
    if [[ -n "$child_id" ]]; then
      child_ids+=("$child_id")
    else
      printf 'Keeping platform image %s of %s: it is tagged or not listed in the package.\n' "$child" "$tags"
    fi
  done < <(jq -r '.manifests[]?.digest' <<<"$manifest")

  delete_version "$id" "$tags"
  deleted=$((deleted + 1))
  for child_id in ${child_ids[@]+"${child_ids[@]}"}; do
    delete_version "$child_id" "platform image of $tags"
    deleted=$((deleted + 1))
  done
done <<<"$prune_plan"

if [[ "$dry_run" == 1 ]]; then
  printf 'Dry run: would delete %s package versions and keep the newest %s edge builds.\n' "$deleted" "$keep_builds"
else
  printf 'Deleted %s package versions and kept the newest %s edge builds.\n' "$deleted" "$keep_builds"
fi
