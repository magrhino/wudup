#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/wud-prune-edge-test.XXXXXX")"
trap 'rm -rf "$TEST_TMP"' EXIT

cat > "$TEST_TMP/gh" <<'FAKE_GH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_GH_LOG"
if [[ "$*" == "api --method DELETE "* ]]; then
  [[ "${FAKE_DELETE_FAIL:-0}" != 1 ]] || exit 1
  exit 0
fi
[[ "${FAKE_LIST_FAIL:-0}" != 1 ]] || exit 1
jq -c '.[]' "$FAKE_VERSIONS"
FAKE_GH
chmod +x "$TEST_TMP/gh"

# Every index lists platform manifests named after it: sha256:<index>a/b.
cat > "$TEST_TMP/docker" <<'FAKE_DOCKER'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
ref="${*: -1}"
digest="${ref##*@}"
printf '{"manifests":[{"digest":"%sa"},{"digest":"%sb"}]}\n' "$digest" "$digest"
FAKE_DOCKER
chmod +x "$TEST_TMP/docker"

export PATH="$TEST_TMP:$PATH"
export FAKE_GH_LOG="$TEST_TMP/gh.log"
export FAKE_DOCKER_LOG="$TEST_TMP/docker.log"
export FAKE_VERSIONS="$TEST_TMP/versions.json"
export REGISTRY="ghcr.io"
export IMAGE_NAME="magrhino/wudup"
versions_path="users/magrhino/packages/container/wudup/versions"

sha_a="aaaaaaa0000000000000000000000000000000aa"
sha_b="bbbbbbb0000000000000000000000000000000bb"
sha_c="ccccccc0000000000000000000000000000000cc"
sha_d="ddddddd0000000000000000000000000000000dd"
sha_s="5555555000000000000000000000000000000055"

# id 1xx: edge build a (newest, edge points here); 2xx: build b; 3xx: build c;
# 4xx: build d (oldest, staged only: its scan failed); 5xx: stable release.
# Children of each index are untagged versions <id>1 and <id>2.
version() {
  local id="$1" created="$2" tags="$3"
  jq -nc --argjson id "$id" --arg created "$created" --arg tags "$tags" '
    {id: $id, name: "sha256:\($id)", created_at: $created,
     metadata: {container: {tags: ($tags | split(",") | map(select(. != "")))}}}'
  jq -nc --argjson id "${id}1" --arg created "$created" '
    {id: $id, name: "sha256:\($id / 10 | floor)a", created_at: $created, metadata: {container: {tags: []}}}'
  jq -nc --argjson id "${id}2" --arg created "$created" '
    {id: $id, name: "sha256:\($id / 10 | floor)b", created_at: $created, metadata: {container: {tags: []}}}'
}
{
  version 100 2026-10-04T00:00:00Z "edge,edge-${sha_a:0:7},staging-edge-$sha_a"
  version 101 2026-10-04T00:05:00Z "edge-trivy,edge-${sha_a:0:7}-trivy,staging-edge-$sha_a-trivy"
  version 200 2026-10-03T00:00:00Z "edge-${sha_b:0:7},staging-edge-$sha_b"
  version 201 2026-10-03T00:05:00Z "edge-${sha_b:0:7}-trivy,staging-edge-$sha_b-trivy"
  version 300 2026-10-02T00:00:00Z "edge-${sha_c:0:7},staging-edge-$sha_c"
  version 301 2026-10-02T00:05:00Z "edge-${sha_c:0:7}-trivy,staging-edge-$sha_c-trivy"
  version 400 2026-10-01T00:00:00Z "staging-edge-$sha_d"
  version 500 2026-09-01T00:00:00Z "v1.2.3,1.2.3,1.2,latest,staging-$sha_s"
  version 501 2026-09-01T00:05:00Z "v1.2.3-trivy,1.2.3-trivy,1.2-trivy,latest-trivy,staging-$sha_s-trivy"
  version 600 2026-08-01T00:00:00Z ""
} | jq -s . > "$FAKE_VERSIONS"

cd "$REPO_ROOT"

run_prune() {
  : > "$FAKE_GH_LOG"
  : > "$FAKE_DOCKER_LOG"
  bash .github/scripts/prune-edge-images.sh > "$TEST_TMP/out" 2> "$TEST_TMP/err"
}

deleted_ids() {
  sed -n "s|^api --method DELETE $versions_path/||p" "$FAKE_GH_LOG" | sort | tr '\n' ' '
}

# Keep the two newest candidate builds (b, c); delete d with its platform images.
EDGE_KEEP_BUILDS=2 run_prune
[[ "$(deleted_ids)" == "400 4001 4002 " ]] || {
  printf 'unexpected deletions with 2 kept builds: %s\n' "$(deleted_ids)" >&2
  exit 1
}
grep -Fxq "api --paginate $versions_path?per_page=100 --jq .[]" "$FAKE_GH_LOG"
grep -Fxq 'buildx imagetools inspect --raw ghcr.io/magrhino/wudup@sha256:400' "$FAKE_DOCKER_LOG"
# The index goes first so a failure never leaves a tag pointing at deleted images.
[[ "$(sed -n "s|^api --method DELETE $versions_path/||p" "$FAKE_GH_LOG" | head -1)" == 400 ]]

# Keep one: b stays, c and d go. edge (a) is never a candidate, nor are stable
# release versions, stable staging tags, or untagged versions with no edge index.
EDGE_KEEP_BUILDS=1 run_prune
[[ "$(deleted_ids)" == "300 3001 3002 301 3011 3012 400 4001 4002 " ]] || {
  printf 'unexpected deletions with 1 kept build: %s\n' "$(deleted_ids)" >&2
  exit 1
}

# The default keeps all of these builds.
run_prune
[[ -z "$(deleted_ids)" ]]
grep -Fq 'No edge builds older than the newest 50 to delete.' "$TEST_TMP/out"

# A dry run lists the plan and deletes nothing.
DRY_RUN=1 EDGE_KEEP_BUILDS=1 run_prune
[[ -z "$(deleted_ids)" ]]
grep -Fq "Would delete edge-${sha_c:0:7},staging-edge-$sha_c (version 300)" "$TEST_TMP/out"
grep -Fq 'Dry run: would delete 9 package versions' "$TEST_TMP/out"

# A platform image that is tagged is never deleted with its index.
jq '(.[] | select(.id == 4001) | .metadata.container.tags) = ["pinned"]' "$FAKE_VERSIONS" > "$TEST_TMP/v.json"
mv "$TEST_TMP/v.json" "$FAKE_VERSIONS"
EDGE_KEEP_BUILDS=2 run_prune
[[ "$(deleted_ids)" == "400 4002 " ]]
grep -Fq 'Keeping platform image sha256:400a' "$TEST_TMP/out"

# Bad settings, list failures, and delete failures stop with a clear message.
if EDGE_KEEP_BUILDS=0 run_prune; then
  printf 'zero kept builds unexpectedly succeeded\n' >&2
  exit 1
fi
[[ ! -s "$FAKE_GH_LOG" ]]
if FAKE_LIST_FAIL=1 run_prune; then
  printf 'package listing failure unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fq 'nothing was deleted' "$TEST_TMP/err"
if FAKE_DELETE_FAIL=1 EDGE_KEEP_BUILDS=2 run_prune; then
  printf 'delete failure unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fq 'needs the Admin role' "$TEST_TMP/err"
[[ "$(grep -c -- '--method DELETE' "$FAKE_GH_LOG")" == 1 ]]

# The edge workflow prunes only after a successful publish.
grep -Fq 'bash .github/scripts/prune-edge-images.sh' .github/workflows/edge.yml

printf 'prune-edge-images tests passed\n'
