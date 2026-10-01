#!/usr/bin/env bash
set -Eeuo pipefail

app_dir="${WUD_APP_DIR:-/app}"
docker_base="${DOCKER_BASE:-/host/docker}"
wud_out_file="${WUD_OUT_FILE:-/out/images.todo}"
wud_log_dir="${WUD_LOG_DIR:-/logs}"

has_arg(){
  local wanted="$1" arg
  shift
  for arg in "$@"; do
    case "$arg" in
      "$wanted"|"$wanted"=*)
        return 0
        ;;
      *)
        ;;
    esac
  done
  return 1
}

removed_command(){
  printf '%s\n' "$1" >&2
  printf 'Use the WebUI (default command), doctor, or docker-update-from-wud. The legacy-file-mode git tag keeps the old behavior for reference.\n' >&2
  exit 2
}

removed_feature_message(){
  printf '%s was removed: WUDup now runs as the WebUI container and no longer ships WUD shell scripts, the host updates wrapper, or TrueNAS status checks.' "$1"
}

warn_ignored_env(){
  local name
  for name in WUD_SYNC_SCRIPTS WUD_SCRIPTS_DIR; do
    if [[ -n "${!name+x}" ]]; then
      printf 'Ignoring %s: WUD script sync was removed, so this setting has no effect and can be deleted.\n' "$name" >&2
    fi
  done
  for name in WUD_PENDING_SOURCE WUDUP_LEGACY_SCRIPTS; do
    if [[ -n "${!name+x}" ]]; then
      printf 'Ignoring %s: the WebUI always reads pending updates from the WUD API, so this setting has no effect and can be deleted.\n' "$name" >&2
    fi
  done
}

if [[ "$#" -eq 0 ]]; then
  set -- web
elif [[ "$1" == -* ]]; then
  removed_command "Options without a command (such as $1) used to run the removed updates wrapper. Pass a command before the options."
fi

warn_ignored_env

case "$1" in
  sync-wud-scripts|updates|truenas-status-export)
    removed_command "$(removed_feature_message "$1")"
    ;;
  doctor)
    shift
    doctor_args=()
    has_arg --base "$@" || doctor_args+=(--base "$docker_base")
    has_arg --file "$@" || doctor_args+=(--file "$wud_out_file")
    has_arg --log-dir "$@" || doctor_args+=(--log-dir "$wud_log_dir")
    if [[ -n "${PYTHONPATH:-}" ]]; then
      export PYTHONPATH="$app_dir/src:$PYTHONPATH"
    else
      export PYTHONPATH="$app_dir/src"
    fi
    export WUD_APP_DIR="$app_dir"
    exec "${PYTHON_BIN:-python3}" -m wudup.cli doctor "${doctor_args[@]}" "$@"
    ;;
  docker-update-from-wud)
    shift
    updater_args=()
    has_arg --base "$@" || updater_args+=(--base "$docker_base")
    has_arg --file "$@" || updater_args+=(--file "$wud_out_file")
    has_arg --log-dir "$@" || updater_args+=(--log-dir "$wud_log_dir")
    exec "$app_dir/bin/docker-update-from-wud" "${updater_args[@]}" "$@"
    ;;
  web)
    shift
    web_args=()
    has_arg --base "$@" || web_args+=(--base "$docker_base")
    has_arg --file "$@" || web_args+=(--file "$wud_out_file")
    has_arg --log-dir "$@" || web_args+=(--log-dir "$wud_log_dir")
    if [[ -n "${PYTHONPATH:-}" ]]; then
      export PYTHONPATH="$app_dir/src:$PYTHONPATH"
    else
      export PYTHONPATH="$app_dir/src"
    fi
    exec "${PYTHON_BIN:-python3}" -m wudup.cli web "${web_args[@]}" "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
