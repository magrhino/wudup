#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO_ROOT"

TEST_TMP=""
IMAGE="${WUDUP_TEST_IMAGE:-}"
cleanup_image=0
REGISTRY_NAME=""
REGISTRY_REF=""
APP_REPO=""
APP_IMAGE=""
OLD_LOCAL_IMAGE=""
NEW_LOCAL_IMAGE=""
MOVED_LOCAL_IMAGE=""
BROKEN_LOCAL_IMAGE=""
NEW_APP_DIGEST=""
PUSH_IMAGE_DIGEST=""
STACK_DIR=""
COMPOSE_FILE=""
OUT_VOLUME=""
LOG_VOLUME=""
DOCKER_WRAPPER_DIR=""
LATEST_RUN_STATUS_QUERY="SELECT status FROM update_runs ORDER BY id DESC LIMIT 1"

fail(){
  printf 'not ok - %s\n' "$*" >&2
  if [[ -n "${COMPOSE_FILE:-}" && -f "$COMPOSE_FILE" ]]; then
    (
      cd "$STACK_DIR"
      printf '# compose ps:\n' >&2
      docker compose -f docker-compose.yml ps >&2 || true
      printf '# compose logs:\n' >&2
      docker compose -f docker-compose.yml logs --no-color >&2 || true
    )
  fi
  if [[ -n "${LOG_VOLUME:-}" && -n "${IMAGE:-}" ]]; then
    printf '# log volume files:\n' >&2
    docker run --rm -v "$LOG_VOLUME:/mnt" "$IMAGE" find /mnt -maxdepth 1 -type f -print >&2 || true
  fi
  exit 1
}

cleanup(){
  if [[ -n "${COMPOSE_FILE:-}" && -f "$COMPOSE_FILE" ]]; then
    (
      cd "$STACK_DIR"
      docker compose -f docker-compose.yml down -v --remove-orphans >/dev/null 2>&1 || true
    )
  fi
  if [[ -n "$REGISTRY_NAME" ]]; then
    docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  fi
  if [[ -n "$APP_IMAGE" ]]; then
    docker image rm "$APP_IMAGE" >/dev/null 2>&1 || true
  fi
  if [[ -n "$APP_REPO" ]]; then
    docker image rm "$APP_REPO:1.0" "$APP_REPO:2.0" "$APP_REPO:e2e-moved" >/dev/null 2>&1 || true
  fi
  if [[ -n "$OUT_VOLUME" ]]; then
    docker volume rm "$OUT_VOLUME" >/dev/null 2>&1 || true
  fi
  if [[ -n "$LOG_VOLUME" ]]; then
    docker volume rm "$LOG_VOLUME" >/dev/null 2>&1 || true
  fi
  if [[ -n "$OLD_LOCAL_IMAGE" ]]; then
    docker image rm "$OLD_LOCAL_IMAGE" >/dev/null 2>&1 || true
  fi
  if [[ -n "$NEW_LOCAL_IMAGE" ]]; then
    docker image rm "$NEW_LOCAL_IMAGE" >/dev/null 2>&1 || true
  fi
  if [[ -n "$MOVED_LOCAL_IMAGE" ]]; then
    docker image rm "$MOVED_LOCAL_IMAGE" >/dev/null 2>&1 || true
  fi
  if [[ -n "$BROKEN_LOCAL_IMAGE" ]]; then
    docker image rm "$BROKEN_LOCAL_IMAGE" >/dev/null 2>&1 || true
  fi
  if [[ "$cleanup_image" -eq 1 && -n "$IMAGE" ]]; then
    docker image rm "$IMAGE" >/dev/null 2>&1 || true
  fi
  if [[ -n "$TEST_TMP" && -d "$TEST_TMP" ]]; then
    rm -rf "$TEST_TMP"
  fi
}
trap cleanup EXIT

run(){
  printf '==> %s\n' "$*"
  "$@"
}

need_cmd(){
  command -v "$1" >/dev/null 2>&1 || {
    printf 'Missing required command: %s\n' "$1" >&2
    exit 127
  }
}

compose(){
  (
    cd "$STACK_DIR"
    docker compose -f docker-compose.yml "$@"
  )
}

volume_file_content(){
  local volume="$1" path="$2"
  docker run --rm -v "$volume:/mnt" "$IMAGE" bash -lc 'cat "$1"' _ "/mnt/$path"
}

write_volume_file(){
  local volume="$1" path="$2" content="$3"
  printf '%s\n' "$content" | docker run -i --rm -v "$volume:/mnt" "$IMAGE" \
    bash -lc 'cat > "$1"' _ "/mnt/$path"
}

assert_volume_file_equals(){
  local volume="$1" path="$2" expected="$3" actual
  actual="$(volume_file_content "$volume" "$path")"
  [[ "$actual" == "$expected" ]] || fail "expected $volume:$path to contain [$expected], got [$actual]"
}

assert_volume_file_empty(){
  local volume="$1" path="$2"
  docker run --rm -v "$volume:/mnt" "$IMAGE" bash -lc 'test ! -s "$1"' _ "/mnt/$path" ||
    fail "expected $volume:$path to be empty"
}

assert_volume_file_owner(){
  local volume="$1" path="$2" expected="$3" actual
  actual="$(docker run --rm -v "$volume:/mnt" "$IMAGE" stat -c '%u:%g' "/mnt/$path")"
  [[ "$actual" == "$expected" ]] || fail "expected $volume:$path owner $expected, got $actual"
}

assert_volume_glob_exists(){
  local volume="$1" pattern="$2"
  docker run --rm -v "$volume:/mnt" "$IMAGE" bash -lc 'compgen -G "$1" >/dev/null' _ "/mnt/$pattern" ||
    fail "expected $volume to contain $pattern"
}

volume_file_count(){
  local volume="$1" pattern="$2"
  docker run --rm -v "$volume:/mnt" "$IMAGE" \
    bash -lc 'find /mnt -maxdepth 1 -type f -name "$1" -print | wc -l | tr -d " "' _ "$pattern"
}

volume_central_log_count(){
  local volume="$1"
  docker run --rm -v "$volume:/mnt" "$IMAGE" \
    bash -lc 'find /mnt -maxdepth 1 -type f -name "update-from-wud-v2-*.log" ! -name "*.errors.log" -print | wc -l | tr -d " "'
}

latest_volume_file_name(){
  local volume="$1" pattern="$2" exclude_pattern="${3:-}"
  docker run --rm -v "$volume:/mnt" "$IMAGE" \
    bash -lc '
find_args=(/mnt -maxdepth 1 -type f -name "$1")
if [[ -n "$2" ]]; then
  find_args+=(! -name "$2")
fi
find "${find_args[@]}" -printf "%f\n" | sort | tail -n 1
' _ "$pattern" "$exclude_pattern"
}

latest_central_log_file_name(){
  local volume="$1"
  latest_volume_file_name "$volume" 'update-from-wud-v2-*.log' '*.errors.log'
}

assert_volume_sqlite_scalar(){
  local volume="$1" path="$2" query="$3" expected="$4" actual
  actual="$(
    docker run --rm -v "$volume:/mnt" "$IMAGE" python -c '
import sqlite3
import sys
from contextlib import closing

with closing(sqlite3.connect(sys.argv[1])) as conn:
    row = conn.execute(sys.argv[2]).fetchone()
print("" if row is None else row[0])
' "/mnt/$path" "$query"
  )"
  [[ "$actual" == "$expected" ]] ||
    fail "expected $volume:$path query [$query] to return [$expected], got [$actual]"
}

container_id(){
  service_container_id app
}

service_container_id(){
  local service="$1"
  compose ps -q "$service"
}

wait_for_service_version(){
  local service="$1" expected="$2" attempt cid version status health

  for attempt in $(seq 1 45); do
    cid="$(service_container_id "$service" || true)"
    if [[ -n "$cid" ]]; then
      version="$(docker exec "$cid" cat /wud-e2e-version 2>/dev/null || true)"
      status="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || true)"
      health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || true)"
      if [[ "$version" == "$expected" && "$status" == "running" && "$health" == "healthy" ]]; then
        return 0
      fi
    fi
    sleep 1
  done

  fail "$service container did not reach version $expected"
}

wait_for_container_version(){
  local expected="$1"
  wait_for_service_version app "$expected"
}

start_registry(){
  REGISTRY_NAME="wudup-e2e-registry-${GITHUB_RUN_ID:-local}-$$"
  local registry_port

  run docker run -d --name "$REGISTRY_NAME" -p 127.0.0.1::5000 registry:2
  registry_port="$(docker inspect -f '{{(index (index .NetworkSettings.Ports "5000/tcp") 0).HostPort}}' "$REGISTRY_NAME")"
  REGISTRY_REF="127.0.0.1:${registry_port}"
  APP_REPO="${REGISTRY_REF}/wud-e2e-app"
  APP_IMAGE="${APP_REPO}:latest"
}

image_repo_digest(){
  local image="$1" repo ref digest

  repo="${image%:*}"
  digest="$(
    docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$image" |
      while IFS= read -r ref; do
        case "$ref" in
          "$repo@sha256:"*)
            printf '%s\n' "${ref#*@}"
            break
            ;;
          *)
            ;;
        esac
      done
  )"
  printf '%s' "$digest"
}

push_image(){
  local image="$1" attempt output digest

  PUSH_IMAGE_DIGEST=""

  printf '==> docker push %s\n' "$image"
  for attempt in $(seq 1 30); do
    output=""
    if output="$(docker push "$image" 2>&1)"; then
      printf '%s\n' "$output"
      digest="$(
        printf '%s\n' "$output" |
          sed -n 's/.*digest: \(sha256:[[:xdigit:]]\{64\}\).*/\1/p' |
          tail -n 1
      )"
      if [[ -z "$digest" ]]; then
        digest="$(image_repo_digest "$image")"
      fi
      PUSH_IMAGE_DIGEST="$digest"
      return 0
    fi
    printf '%s\n' "$output" >&2
    printf 'docker push attempt %s failed for %s\n' "$attempt" "$image" >&2
    sleep 1
  done

  return 1
}

write_fixture_dockerfile(){
  cat > "$TEST_TMP/fixture.Dockerfile" <<'EOF'
ARG BASE_IMAGE=python:3.14-slim-bookworm
FROM ${BASE_IMAGE}
ARG FIXTURE_VERSION
RUN printf '%s\n' "${FIXTURE_VERSION}" > /wud-e2e-version
LABEL org.opencontainers.image.source="https://github.com/magrhino/wudup-e2e-fixture"
HEALTHCHECK --interval=1s --timeout=1s --retries=30 CMD test -f /wud-e2e-version
ENTRYPOINT []
CMD ["bash", "-lc", "trap 'exit 0' TERM INT; while :; do sleep 1; done"]
EOF
}

write_docker_manifest_insecure_wrapper(){
  DOCKER_WRAPPER_DIR="$TEST_TMP/docker-wrapper"
  mkdir -p "$DOCKER_WRAPPER_DIR"
  cat > "$DOCKER_WRAPPER_DIR/docker" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

wrapper_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
docker_path="$(
  filtered_path=""
  IFS=:
  read -r -a path_dirs <<< "$PATH"
  for path_dir in "${path_dirs[@]}"; do
    [[ -n "$path_dir" && "$path_dir" != "$wrapper_dir" ]] || continue
    filtered_path="${filtered_path:+$filtered_path:}$path_dir"
  done
  PATH="$filtered_path" command -v docker
)" || {
  printf 'Missing real docker command behind e2e wrapper\n' >&2
  exit 127
}

if [[ "${1:-}" == "manifest" && "${2:-}" == "inspect" ]]; then
  exec "$docker_path" manifest inspect --insecure "${@:3}"
fi

# Optionally move a registry tag once, right after the updater's Compose pull
# and before it recreates services: "REGISTRY REPO SOURCE_TAG TARGET_TAG".
if [[ "${1:-}" == "compose" && -n "${E2E_RETAG_AFTER_COMPOSE_PULL:-}" ]]; then
  for arg in "${@:2}"; do
    [[ "$arg" == "pull" ]] || continue
    "$docker_path" "$@"
    if [[ ! -e "${E2E_RETAG_MARKER:?}" ]]; then
      read -r registry repo source_tag target_tag <<< "$E2E_RETAG_AFTER_COMPOSE_PULL"
      python "$wrapper_dir/registry-retag.py" "$registry" "$repo" "$source_tag" "$target_tag"
      : > "$E2E_RETAG_MARKER"
    fi
    exit 0
  done
fi

exec "$docker_path" "$@"
EOF
  chmod +x "$DOCKER_WRAPPER_DIR/docker"

  cat > "$DOCKER_WRAPPER_DIR/registry-retag.py" <<'EOF'
"""Point a registry tag at another tag's manifest without touching local images."""

import sys
import urllib.request

registry, repo, source_tag, target_tag = sys.argv[1:5]
accept = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
manifests = f"http://{registry}/v2/{repo}/manifests/"
source = urllib.request.Request(manifests + source_tag, headers={"Accept": accept})
with urllib.request.urlopen(source, timeout=30) as response:
    manifest = response.read()
    content_type = response.headers["Content-Type"]
target = urllib.request.Request(
    manifests + target_tag,
    data=manifest,
    method="PUT",
    headers={"Content-Type": content_type},
)
with urllib.request.urlopen(target, timeout=30) as response:
    digest = response.headers.get("Docker-Content-Digest", "")
print(f"e2e: moved {repo}:{target_tag} to {source_tag} ({digest})", file=sys.stderr)
EOF
}

build_fixture_image(){
  local tag="$1" version="$2"

  run docker build \
    -f "$TEST_TMP/fixture.Dockerfile" \
    --build-arg "BASE_IMAGE=$IMAGE" \
    --build-arg "FIXTURE_VERSION=$version" \
    -t "$tag" \
    "$TEST_TMP"
}

prepare_images(){
  OLD_LOCAL_IMAGE="wudup-e2e-app-old:${GITHUB_RUN_ID:-local}-$$"
  NEW_LOCAL_IMAGE="wudup-e2e-app-new:${GITHUB_RUN_ID:-local}-$$"

  write_fixture_dockerfile
  build_fixture_image "$OLD_LOCAL_IMAGE" old
  run docker tag "$OLD_LOCAL_IMAGE" "$APP_IMAGE"
  push_image "$APP_IMAGE"

  build_fixture_image "$NEW_LOCAL_IMAGE" new
  run docker tag "$NEW_LOCAL_IMAGE" "$APP_IMAGE"
  push_image "$APP_IMAGE"
  NEW_APP_DIGEST="$PUSH_IMAGE_DIGEST"
  if [[ -z "$NEW_APP_DIGEST" ]]; then
    NEW_APP_DIGEST="$(image_repo_digest "$APP_IMAGE")"
  fi
  case "$NEW_APP_DIGEST" in
    sha256:*) ;;
    *) fail "expected pushed new image digest for $APP_IMAGE, got [${NEW_APP_DIGEST:-<empty>}]" ;;
  esac
  run docker tag "$OLD_LOCAL_IMAGE" "$APP_IMAGE"
}

# Write the Compose file from a container, as write_volume_file does. On macOS
# Docker VMs (seen with OrbStack), once a container has written a bind-mounted
# file, later host-side rewrites can stay invisible to new containers for a few
# seconds, so the updater would read the new contents cut off at the old size.
# Writing from a container keeps every write on the updater's side of the file
# share; running as the host user keeps the file owner unchanged.
write_stack_compose_file(){
  docker run -i --rm --user "$(id -u):$(id -g)" -v "$STACK_DIR:$STACK_DIR" "$IMAGE" \
    bash -c 'cat > "$1"' _ "$COMPOSE_FILE"
}

write_compose_file(){
  mkdir -p "$STACK_DIR"
  write_stack_compose_file <<YAML
services:
  app:
    image: ${APP_IMAGE}
    healthcheck:
      test: ["CMD-SHELL", "test -f /wud-e2e-version"]
      interval: 1s
      timeout: 1s
      retries: 30
YAML
}

write_stack_recreate_compose_file(){
  mkdir -p "$STACK_DIR"
  write_stack_compose_file <<YAML
services:
  app:
    image: ${APP_IMAGE}
    labels:
      - WUD-UPDATER-RECREATE-STACK=true
    healthcheck:
      test: ["CMD-SHELL", "test -f /wud-e2e-version"]
      interval: 1s
      timeout: 1s
      retries: 30
  sidecar:
    image: ${OLD_LOCAL_IMAGE}
    depends_on:
      app:
        condition: service_started
    healthcheck:
      test: ["CMD-SHELL", "test -f /wud-e2e-version"]
      interval: 1s
      timeout: 1s
      retries: 30
YAML
}

write_preflight_failure_compose_file(){
  mkdir -p "$STACK_DIR/data"
  write_stack_compose_file <<YAML
services:
  app:
    image: ${APP_IMAGE}
    volumes:
      - ./data:/data
YAML
}

write_pull_policy_compose_file(){
  mkdir -p "$STACK_DIR"
  write_stack_compose_file <<YAML
services:
  app:
    image: ${APP_IMAGE}
    pull_policy: always
    healthcheck:
      test: ["CMD-SHELL", "test -f /wud-e2e-version"]
      interval: 1s
      timeout: 1s
      retries: 30
YAML
}

write_tag_rollback_compose_file(){
  mkdir -p "$STACK_DIR"
  write_stack_compose_file <<YAML
services:
  app:
    image: ${APP_REPO}:1.0
    healthcheck:
      test: ["CMD-SHELL", "test -f /wud-e2e-version && ! grep -qx broken /wud-e2e-version"]
      interval: 1s
      timeout: 1s
      retries: 3
YAML
}

reset_basic_stack(){
  run compose down -v --remove-orphans
  write_compose_file
  run docker tag "$OLD_LOCAL_IMAGE" "$APP_IMAGE"
  run compose up -d
  wait_for_container_version old
}

assert_compose_file_contains(){
  local expected="$1"
  grep -Fq -- "$expected" "$COMPOSE_FILE" ||
    fail "expected compose file to contain [$expected]"
}

run_updater_e2e(){
  write_compose_file
  run compose up -d
  wait_for_container_version old

  write_volume_file "$OUT_VOLUME" images.todo "$APP_IMAGE"
  run docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$STACK_DIR:$STACK_DIR" \
    -v "$OUT_VOLUME:/out" \
    -v "$LOG_VOLUME:/logs" \
    -e WUDUP_BANNER=false \
    -e WUD_DB_PATH=/logs/e2e.sqlite \
    -e OUT_UID="$(id -u)" \
    -e OUT_GID="$(id -g)" \
    "$IMAGE" \
    docker-update-from-wud \
    --base "$STACK_DIR" \
    --file /out/images.todo \
    --log-dir /logs \
    --mode live \
    --max-wait 30 \
    --yes \
    --no-color

  assert_volume_file_empty "$OUT_VOLUME" images.todo
  assert_volume_file_owner "$OUT_VOLUME" images.todo "$(id -u):$(id -g)"
  wait_for_container_version new
  docker run --rm -v "$LOG_VOLUME:/mnt" "$IMAGE" test -s /mnt/e2e.sqlite ||
    fail "expected audit database to be created"
  assert_volume_glob_exists "$LOG_VOLUME" 'update-from-wud-v2-*.log'
}

run_digest_pin_e2e(){
  local pinned_image

  reset_basic_stack
  pinned_image="${APP_IMAGE%:*}@${NEW_APP_DIGEST}"
  write_volume_file "$OUT_VOLUME" images.todo "${APP_IMAGE}@${NEW_APP_DIGEST}"

  run docker run --rm \
    --network host \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$STACK_DIR:$STACK_DIR" \
    -v "$OUT_VOLUME:/out" \
    -v "$LOG_VOLUME:/logs" \
    -v "$DOCKER_WRAPPER_DIR:/e2e-bin:ro" \
    -e PATH=/e2e-bin:/app/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    -e WUDUP_BANNER=false \
    -e WUD_DB_PATH=/logs/e2e-digest.sqlite \
    -e WUD_DIGEST_PIN_UPDATES=true \
    -e OUT_UID="$(id -u)" \
    -e OUT_GID="$(id -g)" \
    "$IMAGE" \
    docker-update-from-wud \
    --base "$STACK_DIR" \
    --file /out/images.todo \
    --log-dir /logs \
    --mode live \
    --max-wait 30 \
    --yes \
    --no-color

  assert_volume_file_empty "$OUT_VOLUME" images.todo
  assert_volume_file_owner "$OUT_VOLUME" images.todo "$(id -u):$(id -g)"
  wait_for_container_version new
  assert_compose_file_contains "$pinned_image"
  assert_compose_file_contains "# wudup.resolved-tag=latest"
  assert_compose_file_contains 'wud.tag.include=^latest$$'
  docker run --rm -v "$LOG_VOLUME:/mnt" "$IMAGE" test -s /mnt/e2e-digest.sqlite ||
    fail "expected digest audit database to be created"
  assert_volume_sqlite_scalar "$LOG_VOLUME" e2e-digest.sqlite \
    "SELECT status || ':' || status_reason FROM pending_updates ORDER BY line_no LIMIT 1" \
    resolved:updated
  assert_volume_sqlite_scalar "$LOG_VOLUME" e2e-digest.sqlite \
    "SELECT digest_target_digest FROM update_events ORDER BY id DESC LIMIT 1" \
    "$NEW_APP_DIGEST"
}

run_stack_recreate_label_e2e(){
  local log_name log_content

  reset_basic_stack
  run compose down -v --remove-orphans
  run docker tag "$OLD_LOCAL_IMAGE" "$APP_IMAGE"
  write_stack_recreate_compose_file
  run compose up -d
  wait_for_container_version old
  wait_for_service_version sidecar old

  write_volume_file "$OUT_VOLUME" images.todo "$APP_IMAGE"
  run docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$STACK_DIR:$STACK_DIR" \
    -v "$OUT_VOLUME:/out" \
    -v "$LOG_VOLUME:/logs" \
    -e WUDUP_BANNER=false \
    -e WUD_DB_PATH=/logs/e2e-stack-recreate.sqlite \
    -e OUT_UID="$(id -u)" \
    -e OUT_GID="$(id -g)" \
    "$IMAGE" \
    docker-update-from-wud \
    --base "$STACK_DIR" \
    --file /out/images.todo \
    --log-dir /logs \
    --mode stop \
    --max-wait 30 \
    --yes \
    --no-color

  assert_volume_file_empty "$OUT_VOLUME" images.todo
  assert_volume_file_owner "$OUT_VOLUME" images.todo "$(id -u):$(id -g)"
  wait_for_container_version new
  wait_for_service_version sidecar old
  docker run --rm -v "$LOG_VOLUME:/mnt" "$IMAGE" test -s /mnt/e2e-stack-recreate.sqlite ||
    fail "expected stack recreate audit database to be created"
  assert_volume_sqlite_scalar "$LOG_VOLUME" e2e-stack-recreate.sqlite \
    "$LATEST_RUN_STATUS_QUERY" \
    success
  log_name="$(latest_central_log_file_name "$LOG_VOLUME")"
  [[ -n "$log_name" ]] || fail "expected latest central log name"
  log_content="$(volume_file_content "$LOG_VOLUME" "$log_name")"
  [[ "$log_content" == *"stack-level recreate"* ]] ||
    fail "stack recreate log missing stack-level recreate message"
  [[ "$log_content" == *"Bringing running stack service(s) up:"* ]] ||
    fail "stack recreate log missing stack up message"
}

run_preflight_failure_e2e(){
  local output rc log_count_before log_count_after error_count_before error_count_after
  local report report_content

  run compose down -v --remove-orphans
  write_preflight_failure_compose_file
  write_volume_file "$OUT_VOLUME" images.todo "$APP_IMAGE"
  log_count_before="$(volume_central_log_count "$LOG_VOLUME")"
  error_count_before="$(volume_file_count "$LOG_VOLUME" 'update-from-wud-v2-*.errors.log')"

  set +e
  output="$(
    docker run --rm \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$STACK_DIR:/host/docker" \
      -v "$OUT_VOLUME:/out" \
      -v "$LOG_VOLUME:/logs" \
      -e WUDUP_BANNER=false \
      -e WUD_DB_PATH=/logs/e2e-preflight.sqlite \
      -e OUT_UID="$(id -u)" \
      -e OUT_GID="$(id -g)" \
      "$IMAGE" \
      docker-update-from-wud \
      --base /host/docker \
      --file /out/images.todo \
      --log-dir /logs \
      --mode live \
      --max-wait 30 \
      --yes \
      --no-color 2>&1
  )"
  rc=$?
  set -e
  printf '%s\n' "$output"

  [[ "$rc" -eq 1 ]] || fail "expected preflight failure exit 1, got $rc"
  [[ "$output" == *"error report:"* ]] || fail "expected preflight output to include error report path"
  assert_volume_file_equals "$OUT_VOLUME" images.todo "$APP_IMAGE"

  log_count_after="$(volume_central_log_count "$LOG_VOLUME")"
  error_count_after="$(volume_file_count "$LOG_VOLUME" 'update-from-wud-v2-*.errors.log')"
  [[ "$log_count_after" -gt "$log_count_before" ]] || fail "expected a new central updater log"
  [[ "$error_count_after" -gt "$error_count_before" ]] || fail "expected a new updater error report"

  report="$(latest_volume_file_name "$LOG_VOLUME" 'update-from-wud-v2-*.errors.log')"
  [[ -n "$report" ]] || fail "expected latest error report name"
  report_content="$(volume_file_content "$LOG_VOLUME" "$report")"
  [[ "$report_content" == *"phase=preflight"* ]] || fail "report missing preflight phase"
  [[ "$report_content" == *"reason=bind-mount-path-invalid"* ]] || fail "report missing bind mount reason"
  [[ "$report_content" == *"/host/docker/data -> /data"* ]] || fail "report missing bad bind source"
  [[ "$report_content" == *"helper-only prefix /host"* ]] || fail "report missing helper-only guidance"

  docker run --rm -v "$LOG_VOLUME:/mnt" "$IMAGE" test -s /mnt/e2e-preflight.sqlite ||
    fail "expected preflight audit database to be created"
  assert_volume_sqlite_scalar "$LOG_VOLUME" e2e-preflight.sqlite \
    "$LATEST_RUN_STATUS_QUERY" \
    failure
  assert_volume_sqlite_scalar "$LOG_VOLUME" e2e-preflight.sqlite \
    "SELECT status || ':' || status_reason FROM pending_updates ORDER BY line_no LIMIT 1" \
    failed:bind-mount-path-invalid
}

run_registry_retag_guard_e2e(){
  local retag_state="$TEST_TMP/retag-state" app_image_id new_image_id

  MOVED_LOCAL_IMAGE="wudup-e2e-app-moved:${GITHUB_RUN_ID:-local}-$$"
  build_fixture_image "$MOVED_LOCAL_IMAGE" moved
  run docker tag "$MOVED_LOCAL_IMAGE" "$APP_REPO:e2e-moved"
  push_image "$APP_REPO:e2e-moved"

  # pull_policy: always would make a plain `compose up` pull the moved tag.
  run compose down -v --remove-orphans
  write_pull_policy_compose_file
  run docker tag "$OLD_LOCAL_IMAGE" "$APP_IMAGE"
  run compose up -d --pull never
  wait_for_container_version old

  mkdir -p "$retag_state"
  write_volume_file "$OUT_VOLUME" images.todo "$APP_IMAGE"
  run docker run --rm \
    --network host \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$STACK_DIR:$STACK_DIR" \
    -v "$OUT_VOLUME:/out" \
    -v "$LOG_VOLUME:/logs" \
    -v "$DOCKER_WRAPPER_DIR:/e2e-bin:ro" \
    -v "$retag_state:/e2e-state" \
    -e PATH=/e2e-bin:/app/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    -e E2E_RETAG_AFTER_COMPOSE_PULL="$REGISTRY_REF wud-e2e-app e2e-moved latest" \
    -e E2E_RETAG_MARKER=/e2e-state/registry-retagged \
    -e WUDUP_BANNER=false \
    -e WUD_DB_PATH=/logs/e2e-retag-guard.sqlite \
    -e OUT_UID="$(id -u)" \
    -e OUT_GID="$(id -g)" \
    "$IMAGE" \
    docker-update-from-wud \
    --base "$STACK_DIR" \
    --file /out/images.todo \
    --log-dir /logs \
    --mode live \
    --max-wait 30 \
    --yes \
    --no-color

  [[ -e "$retag_state/registry-retagged" ]] ||
    fail "expected the registry tag to move between the updater's pull and recreate"
  assert_volume_file_empty "$OUT_VOLUME" images.todo
  wait_for_container_version new
  app_image_id="$(docker image inspect -f '{{.Id}}' "$APP_IMAGE")"
  new_image_id="$(docker image inspect -f '{{.Id}}' "$NEW_LOCAL_IMAGE")"
  [[ "$app_image_id" == "$new_image_id" ]] ||
    fail "expected local $APP_IMAGE to stay on the pulled image $new_image_id, got $app_image_id"
  assert_volume_sqlite_scalar "$LOG_VOLUME" e2e-retag-guard.sqlite \
    "$LATEST_RUN_STATUS_QUERY" \
    success

  # Put the registry tag back so later scenarios see the expected new image.
  run docker tag "$NEW_LOCAL_IMAGE" "$APP_IMAGE"
  push_image "$APP_IMAGE"
}

run_tag_update_rollback_e2e(){
  local old_tag_image="$APP_REPO:1.0" broken_tag_image="$APP_REPO:2.0"
  local compose_before="$TEST_TMP/tag-rollback-compose.before"
  local output rc incident incident_content

  BROKEN_LOCAL_IMAGE="wudup-e2e-app-broken:${GITHUB_RUN_ID:-local}-$$"
  build_fixture_image "$BROKEN_LOCAL_IMAGE" broken
  run docker tag "$OLD_LOCAL_IMAGE" "$old_tag_image"
  push_image "$old_tag_image"
  run docker tag "$BROKEN_LOCAL_IMAGE" "$broken_tag_image"
  push_image "$broken_tag_image"
  # Drop the local 2.0 tag so the updater has to pull it from the registry.
  run docker image rm "$broken_tag_image"

  run compose down -v --remove-orphans
  write_tag_rollback_compose_file
  cp "$COMPOSE_FILE" "$compose_before"
  run compose up -d
  wait_for_container_version old

  write_volume_file "$OUT_VOLUME" images.todo "$old_tag_image tag=2.0"
  set +e
  output="$(
    docker run --rm \
      --network host \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$STACK_DIR:$STACK_DIR" \
      -v "$OUT_VOLUME:/out" \
      -v "$LOG_VOLUME:/logs" \
      -v "$DOCKER_WRAPPER_DIR:/e2e-bin:ro" \
      -e PATH=/e2e-bin:/app/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
      -e WUDUP_BANNER=false \
      -e WUD_DB_PATH=/logs/e2e-tag-rollback.sqlite \
      -e OUT_UID="$(id -u)" \
      -e OUT_GID="$(id -g)" \
      "$IMAGE" \
      docker-update-from-wud \
      --base "$STACK_DIR" \
      --file /out/images.todo \
      --log-dir /logs \
      --mode live \
      --max-wait 30 \
      --allow-tag-updates \
      --yes \
      --no-color 2>&1
  )"
  rc=$?
  set -e
  printf '%s\n' "$output"

  [[ "$rc" -eq 1 ]] || fail "expected unhealthy tag update to exit 1, got $rc"
  assert_volume_file_equals "$OUT_VOLUME" images.todo "$old_tag_image tag=2.0"
  cmp -s "$compose_before" "$COMPOSE_FILE" ||
    fail "expected compose file to be restored after the failed tag update"
  wait_for_container_version old

  incident="$(find "$STACK_DIR" -maxdepth 1 -type f -name 'error-2.0-*.logs' -print | sort | tail -n 1)"
  [[ -n "$incident" ]] || fail "expected a tag update incident log in $STACK_DIR"
  incident_content="$(cat "$incident")"
  [[ "$incident_content" == *"$old_tag_image -> $broken_tag_image"* ]] ||
    fail "incident log missing attempted tag update"
  [[ "$incident_content" == *"rollback=restored-and-healthy"* ]] ||
    fail "incident log missing successful rollback"
  [[ "$incident_content" == *"manual_review_required=no"* ]] ||
    fail "incident log should not require manual review"
  assert_volume_sqlite_scalar "$LOG_VOLUME" e2e-tag-rollback.sqlite \
    "$LATEST_RUN_STATUS_QUERY" \
    failure
}

main(){
  need_cmd docker

  run docker version
  run docker compose version

  TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/wudup-e2e.XXXXXX")"
  STACK_DIR="$TEST_TMP/docker/wud-e2e-stack"
  COMPOSE_FILE="$STACK_DIR/docker-compose.yml"
  OUT_VOLUME="wudup-e2e-out-${GITHUB_RUN_ID:-local}-$$"
  LOG_VOLUME="wudup-e2e-logs-${GITHUB_RUN_ID:-local}-$$"
  write_docker_manifest_insecure_wrapper

  if [[ -z "$IMAGE" ]]; then
    IMAGE="wudup:e2e-${GITHUB_RUN_ID:-local}-$$"
    cleanup_image=1
    run docker build -t "$IMAGE" .
  fi

  run docker volume create "$OUT_VOLUME"
  run docker volume create "$LOG_VOLUME"
  start_registry
  prepare_images
  run_updater_e2e
  run_digest_pin_e2e
  run_stack_recreate_label_e2e
  run_preflight_failure_e2e
  run_registry_retag_guard_e2e
  run_tag_update_rollback_e2e
}

main "$@"
