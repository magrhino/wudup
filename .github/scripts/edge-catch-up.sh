#!/usr/bin/env bash
set -euo pipefail

# After a successful edge run, start a run for the head of main if it has no
# edge build yet. A re-run of an older commit can replace the newest waiting
# run, and a [skip ci] push during a run starts none of its own. A [skip ci]
# push made while no edge run is active is not caught here.
if [[ ! "${BUILT_SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'Edge catch-up needs BUILT_SHA as a full commit SHA, got: %s\n' "${BUILT_SHA:-}" >&2
  exit 2
fi

if ! main_sha="$(gh api "repos/$GITHUB_REPOSITORY/branches/main" --jq .commit.sha)" ||
  [[ ! "$main_sha" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'Edge catch-up could not read the head of main, so no new edge run was started. If edge is behind main, start one with: gh workflow run edge.yml --ref main\n' >&2
  exit 1
fi

if [[ "$main_sha" == "$BUILT_SHA" ]]; then
  printf 'Edge is built from the head of main (%s).\n' "$main_sha"
  exit 0
fi

if ! published="$(
  gh run list --workflow edge.yml --commit "$main_sha" --status success \
    --json databaseId --jq length
)" || [[ ! "$published" =~ ^[0-9]+$ ]]; then
  printf 'Edge catch-up could not check whether %s already has an edge build, so no new edge run was started. If edge is behind main, start one with: gh workflow run edge.yml --ref main\n' \
    "$main_sha" >&2
  exit 1
fi
if (( published > 0 )); then
  printf 'The head of main (%s) already has a successful edge build.\n' "$main_sha"
  exit 0
fi

# A run for this head may already be waiting; the edge concurrency group keeps
# only the newest waiting run, so this never builds the same head twice at once.
gh workflow run edge.yml --ref main
printf 'Started an edge run for the head of main (%s); this run built %s.\n' "$main_sha" "$BUILT_SHA"
