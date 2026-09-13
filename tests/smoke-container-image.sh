#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO_ROOT"

IMAGE="${1:?container image reference is required}"

docker_name_component(){
  local value="${1:-local}"
  local digest sanitized

  sanitized="$(printf '%s' "$value" | LC_ALL=C tr -c 'A-Za-z0-9_.-' '-')"
  if [[ -z "$sanitized" ]]; then
    sanitized="local"
  fi
  if (( ${#sanitized} > 48 )); then
    digest="$(printf '%s' "$value" | cksum | awk '{ print $1 }')"
    sanitized="${sanitized:0:40}-${digest}"
  fi

  printf '%s' "$sanitized"
}

RUN_ID_COMPONENT="$(docker_name_component "${GITHUB_RUN_ID:-local}")"
SMOKE_LABEL="wudup.image-smoke=${RUN_ID_COMPONENT}-$$"
SYNC_TMP=""
HEALTH_TMP=""
HEALTH_CONTAINER=""

cleanup(){
  local containers=()
  local container

  while IFS= read -r container; do
    if [[ -n "$container" ]]; then
      containers+=("$container")
    fi
  done < <(docker ps -aq --filter "label=$SMOKE_LABEL" 2>/dev/null || true)
  if ((${#containers[@]})); then
    docker rm -f "${containers[@]}" >/dev/null 2>&1 || true
  fi
  if [[ -n "$SYNC_TMP" && -d "$SYNC_TMP" ]]; then
    rm -rf "$SYNC_TMP"
  fi
  if [[ -n "$HEALTH_TMP" && -d "$HEALTH_TMP" ]]; then
    rm -rf "$HEALTH_TMP"
  fi
}
trap cleanup EXIT

run(){
  local description="$*"
  local -a command=("$@")

  printf '==> %s\n' "$description"
  "${command[@]}"
}

run_with_timeout(){
  local seconds="$1"
  shift
  local elapsed=0
  local pid status

  printf '==> timeout %ss %s\n' "$seconds" "$*"
  "$@" &
  pid=$!

  while kill -0 "$pid" 2>/dev/null; do
    if (( elapsed >= seconds )); then
      printf 'Command timed out after %ss: %s\n' "$seconds" "$*" >&2
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 124
    fi

    sleep 1
    elapsed=$((elapsed + 1))
  done

  set +e
  wait "$pid"
  status=$?
  set -e
  return "$status"
}

run_docker_smoke(){
  local seconds="$1"
  shift

  run_with_timeout "$seconds" docker run --rm --label "$SMOKE_LABEL" "$@"
}

wait_for_default_web_health(){
  local status

  for _ in {1..90}; do
    status="$(
      docker inspect \
        -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
        "$HEALTH_CONTAINER"
    )"
    case "$status" in
      healthy)
        return 0
        ;;
      unhealthy)
        printf 'Default WebUI container became unhealthy.\n' >&2
        docker inspect -f '{{json .State.Health}}' "$HEALTH_CONTAINER" >&2 || true
        docker logs "$HEALTH_CONTAINER" >&2 || true
        return 1
        ;;
      *)
        sleep 1
        ;;
    esac
  done

  printf 'Timed out waiting for default WebUI container healthcheck.\n' >&2
  docker inspect -f '{{json .State.Health}}' "$HEALTH_CONTAINER" >&2 || true
  docker logs "$HEALTH_CONTAINER" >&2 || true
  return 1
}

smoke_default_web_health(){
  HEALTH_TMP="$(mktemp -d "${TMPDIR:-/tmp}/wud-health-test.XXXXXX")"
  HEALTH_CONTAINER="wudup-health-${RUN_ID_COMPONENT}-$$"
  mkdir -p "$HEALTH_TMP/host-docker" "$HEALTH_TMP/out" "$HEALTH_TMP/logs"
  chmod 700 "$HEALTH_TMP/logs"
  touch "$HEALTH_TMP/out/images.todo"

  run_with_timeout 60 docker run -d \
    --name "$HEALTH_CONTAINER" \
    --label "$SMOKE_LABEL" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$HEALTH_TMP/host-docker:/host/docker" \
    -v "$HEALTH_TMP/out:/out" \
    -v "$HEALTH_TMP/logs:/logs" \
    -e OUT_UID="$(id -u)" \
    -e OUT_GID="$(id -g)" \
    "$IMAGE"
  run wait_for_default_web_health
}

need_cmd(){
  local cmd="$1"

  command -v "$cmd" >/dev/null 2>&1 || {
    printf 'Missing required command: %s\n' "$cmd" >&2
    exit 127
  }
}

need_cmd docker

run_docker_smoke 90 "$IMAGE" test -f /app/src/wudup/web_static/index.html
run smoke_default_web_health
run_docker_smoke 180 "$IMAGE" updates --dry-run
run_docker_smoke 180 -e WUDUP_PYTHON=true "$IMAGE" updates --dry-run

SYNC_TMP="$(mktemp -d "${TMPDIR:-/tmp}/wud-script-sync-test.XXXXXX")"
run_docker_smoke 90 -v "$SYNC_TMP:/managed-wud" "$IMAGE" sync-wud-scripts
[[ -x "$SYNC_TMP/on-update.sh" ]]
[[ -x "$SYNC_TMP/append-updates.sh" ]]
[[ -x "$SYNC_TMP/release-parser.sh" ]]
[[ -x "$SYNC_TMP/release-notes-to-discord.sh" ]]
[[ -x "$SYNC_TMP/github-release-embed.sh" ]]
[[ -x "$SYNC_TMP/tag-manager.sh" ]]
[[ -f "$SYNC_TMP/upstreams.txt" ]]

run_docker_smoke 90 "$IMAGE" docker-update-from-wud --help
