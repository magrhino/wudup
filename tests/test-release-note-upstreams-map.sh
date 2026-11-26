#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
MAP_FILE="${1:-$REPO_ROOT/wud/release-note-upstreams.txt}"

fail() {
  printf 'not ok - %s\n' "$*" >&2
  exit 1
}

[[ -f "$MAP_FILE" ]] || fail "release-note upstream map not found: $MAP_FILE"

export LC_ALL=C

github_key_re='^github\.com/[a-z0-9_.-]+/[a-z0-9_.-]+$'
image_key_re='^([a-z0-9.-]+(:[0-9]+)?/)?[a-z0-9._-]+(/[a-z0-9._-]+)*$'
repo_value_re='^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
url_value_re='^https://[^[:space:]/]+(/[^[:space:]]*)?$'

previous_key=""
line_no=0
entry_count=0

while IFS= read -r line || [[ -n "$line" ]]; do
  line_no=$((line_no + 1))
  [[ -z "$line" || "$line" == \#* ]] && continue

  [[ "$line" == *": "* ]] || fail "$MAP_FILE:$line_no must match '<key>: <value>'"
  key="${line%%: *}"
  value="${line#*: }"

  if [[ "$key" == github.com/* ]]; then
    [[ "$key" =~ $github_key_re ]] \
      || fail "$MAP_FILE:$line_no key must be lowercase 'github.com/<owner>/<repo>'"
  else
    [[ "$key" =~ $image_key_re ]] \
      || fail "$MAP_FILE:$line_no key must be a lowercase image without a tag or digest"
    case "$key" in
      docker.io/* | index.docker.io/* | registry-1.docker.io/* | library/*)
        fail "$MAP_FILE:$line_no write Docker Hub images without docker.io/ or library/"
        ;;
    esac
  fi

  if [[ ! "$value" =~ $repo_value_re && ! "$value" =~ $url_value_re ]]; then
    fail "$MAP_FILE:$line_no value must be 'Owner/Repo' or an https:// link"
  fi

  if [[ -n "$previous_key" ]]; then
    [[ "$key" != "$previous_key" ]] || fail "$MAP_FILE:$line_no duplicates $key"
    [[ "$key" > "$previous_key" ]] || fail "$MAP_FILE:$line_no sorts before $previous_key"
  fi

  previous_key="$key"
  entry_count=$((entry_count + 1))
done < "$MAP_FILE"

(( entry_count > 0 )) || fail "$MAP_FILE has no release-note upstream mappings"

printf 'ok - validated %s release-note upstream mappings\n' "$entry_count"
