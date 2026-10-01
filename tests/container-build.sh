#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO_ROOT"

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

COMPOSE_EXAMPLE="docs/examples/docker-compose.example.yml"
COMPOSE_WEBUI="docs/examples/docker-compose.webui.yml"
COMPOSE_HARDENED="docs/examples/docker-compose.hardened.yml"
COMPOSE_BUILD="docs/examples/docker-compose.build.yml"
cleanup_image=0
RUN_ID_COMPONENT="$(docker_name_component "${GITHUB_RUN_ID:-local}")"
if [[ -n "${WUDUP_TEST_IMAGE:-}" ]]; then
  IMAGE="$WUDUP_TEST_IMAGE"
else
  IMAGE="wudup:test-${RUN_ID_COMPONENT}-$$"
  cleanup_image=1
fi
TRIVY_IMAGE="${WUDUP_TEST_TRIVY_IMAGE:-${IMAGE}-trivy}"

cleanup(){
  if [[ "$cleanup_image" -eq 1 ]]; then
    docker image rm "$IMAGE" >/dev/null 2>&1 || true
    docker image rm "$TRIVY_IMAGE" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

run(){
  local description="$*"
  local -a command=("$@")

  printf '==> %s\n' "$description"
  "${command[@]}"
}

run_quiet(){
  local description="$*"
  local output
  local -a command=("$@")

  printf '==> %s\n' "$description"
  if ! output="$("${command[@]}" 2>&1)"; then
    printf '%s\n' "$output" >&2
    return 1
  fi
}

assert_duration(){
  local label="$1" actual="$2" expected_text="$3" expected_nanos="$4"

  [[ "$actual" == "$expected_text" || "$actual" == "$expected_nanos" ]] || {
    printf 'Expected %s=%s, got %s\n' "$label" "$expected_text" "$actual" >&2
    return 1
  }
}

assert_image_metadata(){
  local image_ref="${1:-$IMAGE}"
  local cmd web_host
  local health_test_len health_test_type health_test_command
  local health_interval health_timeout health_retries health_start_period

  cmd="$(docker image inspect -f '{{json .Config.Cmd}}' "$image_ref")"
  [[ "$cmd" == '["web"]' ]] || {
    printf 'Expected image Cmd ["web"], got %s\n' "$cmd" >&2
    return 1
  }

  web_host="$(
    docker image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$image_ref" |
      awk -F= '$1 == "WUD_WEB_HOST" { print $2; exit }'
  )"
  [[ "$web_host" == "0.0.0.0" ]] || {
    printf 'Expected WUD_WEB_HOST=0.0.0.0, got %s\n' "${web_host:-<unset>}" >&2
    return 1
  }

  health_test_len="$(docker image inspect -f '{{if .Config.Healthcheck}}{{len .Config.Healthcheck.Test}}{{else}}0{{end}}' "$image_ref")"
  [[ "$health_test_len" == "2" ]] || {
    printf 'Expected healthcheck test with 2 entries, got %s\n' "$health_test_len" >&2
    return 1
  }
  health_test_type="$(docker image inspect -f '{{index .Config.Healthcheck.Test 0}}' "$image_ref")"
  health_test_command="$(docker image inspect -f '{{index .Config.Healthcheck.Test 1}}' "$image_ref")"
  [[ "$health_test_type" == "CMD-SHELL" ]] || {
    printf 'Expected healthcheck type CMD-SHELL, got %s\n' "$health_test_type" >&2
    return 1
  }
  [[ " $health_test_command " == *" curl "* ]] || {
    printf 'Expected healthcheck command to run curl, got %s\n' "$health_test_command" >&2
    return 1
  }
  [[ " $health_test_command " == *" -fsS "* ]] || {
    printf 'Expected healthcheck curl flags -fsS, got %s\n' "$health_test_command" >&2
    return 1
  }
  [[ " $health_test_command " == *" -o /dev/null "* ]] || {
    printf 'Expected healthcheck to discard response body, got %s\n' "$health_test_command" >&2
    return 1
  }
  [[ "$health_test_command" == *"http://127.0.0.1:\${WUD_WEB_PORT:-7417}/readyz"* ]] || {
    printf 'Expected healthcheck command to call the readyz endpoint, got %s\n' "$health_test_command" >&2
    return 1
  }
  [[ "$health_test_command" == *'|| exit 1'* ]] || {
    printf 'Expected healthcheck command to fail on unsuccessful readyz probe, got %s\n' "$health_test_command" >&2
    return 1
  }

  health_interval="$(docker image inspect -f '{{.Config.Healthcheck.Interval}}' "$image_ref")"
  health_timeout="$(docker image inspect -f '{{.Config.Healthcheck.Timeout}}' "$image_ref")"
  health_retries="$(docker image inspect -f '{{.Config.Healthcheck.Retries}}' "$image_ref")"
  health_start_period="$(docker image inspect -f '{{.Config.Healthcheck.StartPeriod}}' "$image_ref")"

  assert_duration "healthcheck.interval" "$health_interval" "30s" "30000000000"
  assert_duration "healthcheck.timeout" "$health_timeout" "5s" "5000000000"
  [[ "$health_retries" == "3" ]] || {
    printf 'Expected healthcheck.retries=3, got %s\n' "$health_retries" >&2
    return 1
  }
  assert_duration "healthcheck.start_period" "$health_start_period" "10s" "10000000000"
}

need_cmd(){
  local cmd="$1"

  command -v "$cmd" >/dev/null 2>&1 || {
    printf 'Missing required command: %s\n' "$cmd" >&2
    exit 127
  }
}

need_cmd docker

run docker version
run docker compose version
run_quiet docker compose -f "$COMPOSE_EXAMPLE" config
run_quiet docker compose -f "$COMPOSE_WEBUI" config
run_quiet docker compose -f "$COMPOSE_HARDENED" config
run_quiet docker compose -f "$COMPOSE_BUILD" config
run docker build -t "$IMAGE" .
run assert_image_metadata "$IMAGE"
run bash tests/smoke-container-image.sh "$IMAGE"
run docker build --target wudup-trivy -t "$TRIVY_IMAGE" .
run assert_image_metadata "$TRIVY_IMAGE"
run docker run --rm "$TRIVY_IMAGE" trivy --version
