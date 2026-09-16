#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
# shellcheck source=wud/http.sh
source "${SCRIPT_DIR}/http.sh"

RELEASE_EMBED="${RELEASE_EMBED:-${SCRIPT_DIR}/github-release-embed.sh}"
UPSTREAM_MAP="${UPSTREAM_MAP:-/wud/upstreams.txt}"
LOG_DIR="${LOG_DIR:-/out}"
LOG_FILE="${LOG_DIR}/tag-manager.$(date +%Y%m%d).log"
DISCORD_WEBHOOK="${DISCORD_WEBHOOK:-}"
ADMIN_WEBHOOK="${ADMIN_WEBHOOK:-$DISCORD_WEBHOOK}"

mkdir -p "$LOG_DIR"

log() {
  printf '%s %s\n' "$(date -Iseconds)" "$*" | tee -a "$LOG_FILE" >&2
}

redacted_args() {
  local redact_next=0
  local arg
  for arg in "$@"; do
    if (( redact_next )); then
      printf '<redacted> '
      redact_next=0
      continue
    fi
    if [[ "$arg" == "--webhook" ]]; then
      printf '%q ' "$arg"
      redact_next=1
      continue
    fi
    printf '%q ' "$arg"
  done
}

lookup_upstream() {
  local key="$1"
  [[ -r "$UPSTREAM_MAP" ]] || return 1
  awk -v k="$key" -F: '
    $0 ~ /^[[:space:]]*#/ { next }
    NF >= 2 {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", $1)
      if ($1 == k) {
        sub(/^[[:space:]]+/, "", $2)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2)
        print $2
        exit 0
      }
    }' "$UPSTREAM_MAP"
}

send_admin_notice() {
  local message="$1" payload
  [[ -n "$ADMIN_WEBHOOK" ]] || return 0
  payload="$(jq -n --arg content "$message" '{content:$content, allowed_mentions:{parse:[]}}')"
  http_post_discord_json "$ADMIN_WEBHOOK" "$payload"
}

run_embed() {
  local output rc printable
  printable="$(redacted_args "$@")"
  log "+ ${printable% }"
  if output="$("$@" 2>&1)"; then
    rc=0
  else
    rc=$?
  fi
  if [[ -n "$output" ]]; then
    printf '%s\n' "$output" | tee -a "$LOG_FILE" >&2
  fi
  log "cmd rc=${rc}"
  return "$rc"
}

[[ -x "$RELEASE_EMBED" ]] || {
  log "ERROR: github-release-embed.sh not found or not executable at $RELEASE_EMBED"
  exit 1
}

log "---- WUD ENV (subset) ----"
log "image_name=${image_name:-}"
log "image_registry_url=${image_registry_url:-}"
log "update_available=${update_available:-}"
log "update_kind_kind=${update_kind_kind:-}"
log "update_kind_remote_value=${update_kind_remote_value:-}"
log "result_tag=${result_tag:-}"
log "DISCORD_WEBHOOK=$([[ -n "$DISCORD_WEBHOOK" ]] && printf '<set>' || printf '<empty>')"
log "--------------------------"

if [[ "${update_available:-false}" != "true" ]]; then
  log "No update_available=true; exiting."
  exit 0
fi

image_name="${image_name:-}"
image_registry_url="${image_registry_url:-}"
update_kind_kind="${update_kind_kind:-}"
update_kind_remote_value="${update_kind_remote_value:-}"
result_tag="${result_tag:-}"

if [[ "$image_name" == linuxserver/* ]]; then
  base="${image_name#linuxserver/}"
  if [[ "$base" == docker-* ]]; then
    lsio_repo="linuxserver/$base"
  else
    lsio_repo="linuxserver/docker-$base"
  fi
  upstream_repo="$(lookup_upstream "$lsio_repo" || true)"
  if [[ -z "$upstream_repo" ]]; then
    message="⚠️ Missing upstream mapping for \`${lsio_repo}\`. Please add a line:
\`${lsio_repo}: Owner/Repo\` in \`${UPSTREAM_MAP}\`."
    log "$message"
    send_admin_notice "$message" || log "WARN: admin webhook send failed"
    log "Skipping embed call due to missing upstream."
    exit 0
  fi
  args=("$RELEASE_EMBED" --provider lsio --lsio "$lsio_repo" --upstream "$upstream_repo" --debug)
  if [[ "$upstream_repo" == "$lsio_repo" ]]; then
    if [[ "$update_kind_kind" == "tag" && -n "$update_kind_remote_value" ]]; then
      args+=(--tag "$update_kind_remote_value")
    elif [[ -n "$result_tag" ]]; then
      args+=(--tag "$result_tag")
    fi
  fi
  [[ -n "$DISCORD_WEBHOOK" ]] && args+=(--webhook "$DISCORD_WEBHOOK")
  run_embed "${args[@]}"
elif [[ "$image_registry_url" == *ghcr.io* ]]; then
  args=("$RELEASE_EMBED" --provider github --repo "$image_name" --debug)
  if [[ "$update_kind_kind" == "tag" && -n "$update_kind_remote_value" ]]; then
    args+=(--tag "$update_kind_remote_value")
  elif [[ -n "$result_tag" ]]; then
    args+=(--tag "$result_tag")
  fi
  [[ -n "$DISCORD_WEBHOOK" ]] && args+=(--webhook "$DISCORD_WEBHOOK")
  run_embed "${args[@]}"
else
  log "Non-LSIO and non-GHCR image ($image_name @ $image_registry_url); skipping."
fi
