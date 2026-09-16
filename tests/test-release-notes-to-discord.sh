#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SCRIPT="$REPO_ROOT/wud/release-notes-to-discord.sh"
GITHUB_EMBED="$REPO_ROOT/wud/github-release-embed.sh"
TAG_MANAGER="$REPO_ROOT/wud/tag-manager.sh"
PARITY_SPEC="$REPO_ROOT/tests/fixtures/release-note-parity.json"
TEST_TMP=""

fail(){
  printf 'not ok - %s\n' "$*" >&2
  if [[ -n "${TEST_TMP:-}" && -f "$TEST_TMP/output.log" ]]; then
    sed 's/^/# /' "$TEST_TMP/output.log" >&2 || true
  fi
  if [[ -n "${TEST_TMP:-}" && -f "$TEST_TMP/payload.diff" ]]; then
    sed 's/^/# diff: /' "$TEST_TMP/payload.diff" >&2 || true
  fi
  exit 1
}

parity_value(){
  jq -r "$1" "$PARITY_SPEC"
}

setup_case(){
  TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/wud-release-notes-test.XXXXXX")"
  mkdir -p "$TEST_TMP/bin"
  printf '%s: %s\n' \
    "$(parity_value '.lsio_radarr.lsio_repo')" \
    "$(parity_value '.lsio_radarr.upstream_repo')" \
    > "$TEST_TMP/upstreams.txt"
  write_fakes
}

teardown_case(){
  [[ -n "${TEST_TMP:-}" && -d "$TEST_TMP" ]] && rm -rf "$TEST_TMP"
  TEST_TMP=""
}

write_fakes(){
  cat > "$TEST_TMP/bin/docker" <<'FAKE_DOCKER'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "$1 $2" == "image inspect" ]]; then
  printf '%s\n' "${FAKE_IMAGE_SOURCE:-}"
  exit 0
fi
exit 1
FAKE_DOCKER

  cat > "$TEST_TMP/bin/curl" <<'FAKE_CURL'
#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -n "${FAKE_CURL_ARGS_LOG:-}" ]]; then
  printf '%q ' "$@" >> "$FAKE_CURL_ARGS_LOG"
  printf '\n' >> "$FAKE_CURL_ARGS_LOG"
fi

out_file=""
write_out=""
payload=""
url=""

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -o)
      out_file="$2"
      shift 2
      ;;
    -w)
      write_out="$2"
      shift 2
      ;;
    -d)
      payload="$2"
      shift 2
      ;;
    -H|-X|--retry|--retry-delay|--connect-timeout|--max-time)
      shift 2
      ;;
    -*)
      shift
      ;;
    *)
      url="$1"
      shift
      ;;
  esac
done

write_body(){
  if [[ -n "$out_file" ]]; then
    printf '%s' "$1" > "$out_file"
  else
    printf '%s' "$1"
  fi
}

release_json(){
  jq -n \
    --arg tag "$1" \
    --arg repo "$2" \
    --arg body "$3" \
    '{
      tag_name:$tag,
      name:$tag,
      html_url:("https://github.com/" + $repo + "/releases/tag/" + $tag),
      body:$body,
      published_at:"2026-01-02T00:00:00Z"
    }'
}

if [[ "$url" == "https://discord.test/webhook" ]]; then
  printf '%s' "$payload" > "${FAKE_WEBHOOK_PAYLOAD:?}"
  if [[ "$write_out" == *"%{http_code}"* ]]; then
    printf '\n204'
  fi
  exit 0
fi
if [[ "$url" == "https://discord.test/admin" ]]; then
  printf '%s' "$payload" > "${FAKE_ADMIN_PAYLOAD:?}"
  if [[ "$write_out" == *"%{http_code}"* ]]; then
    printf '\n204'
  fi
  exit 0
fi
if [[ "$url" == https://discord.test/fail/* ]]; then
  if [[ "$write_out" == *"%{http_code}"* ]]; then
    printf '\n500'
  fi
  exit 0
fi

case "$url" in
  https://api.github.com/repos/linuxserver/docker-socket-proxy/releases/*|https://api.github.com/repos/linuxserver/docker-image-only-example/releases/*)
    repo="${url#https://api.github.com/repos/}"
    repo="${repo%/releases/*}"
    case "$url" in
      */releases/latest|*/releases/tags/3.4.4-r0-ls97)
        write_body "$(release_json "3.4.4-r0-ls97" "$repo" $'## Changes\n- Routine image maintenance')"
        ;;
      *) write_body '{"message":"Not Found"}' ;;
    esac
    ;;
  https://api.github.com/repos/acme/app/releases/latest)
    write_body "$(release_json "v2.0.0" "acme/app" $'## Changes\n- Routine maintenance')"
    ;;
  https://api.github.com/repos/acme/app/releases/tags/v2.0.0)
    write_body "$(release_json "v2.0.0" "acme/app" $'## Changes\n- Routine maintenance')"
    ;;
  https://api.github.com/repos/linuxserver/docker-radarr/releases/latest)
    write_body "$(release_json "5.1.0-ls1" "linuxserver/docker-radarr" $'LinuxServer Changes:\n- Rebase to Alpine 3.20\n- Add package\n\nRemote Changes:\n- Updating to 5.1.0')"
    ;;
  https://api.github.com/repos/Radarr/Radarr/releases/tags/v5.1.0)
    write_body "$(release_json "v5.1.0" "Radarr/Radarr" $'## Changes\n- New queue view')"
    ;;
  *)
    printf 'unexpected curl URL: %s\n' "$url" >&2
    exit 1
    ;;
esac
FAKE_CURL

  chmod +x "$TEST_TMP/bin/docker" "$TEST_TMP/bin/curl"
}

run_notes(){
  local image="$1" current="$2" payload_file="$3"
  local webhook="${DISCORD_WEBHOOK:-https://discord.test/webhook}"

  PATH="$TEST_TMP/bin:$PATH" \
    DISCORD_WEBHOOK="$webhook" \
    FAKE_WEBHOOK_PAYLOAD="$payload_file" \
    FAKE_CURL_ARGS_LOG="$TEST_TMP/curl.args" \
    FAKE_IMAGE_SOURCE="${FAKE_IMAGE_SOURCE:-}" \
    UPSTREAM_MAP="$TEST_TMP/upstreams.txt" \
    "$SCRIPT" "$image" "container" "$current" > "$TEST_TMP/output.log" 2>&1 || fail "release note script failed"
}

assert_curl_policy_for_url(){
  local url="$1" line
  line="$(grep -F -- "$url" "$TEST_TMP/curl.args" | head -n 1 || true)"
  [[ -n "$line" ]] || fail "no curl call captured for $url"
  [[ "$line" == *"--retry 3"* ]] || fail "curl call for $url did not set retry policy"
  [[ "$line" == *"--connect-timeout 5"* ]] || fail "curl call for $url did not set connect timeout"
  [[ "$line" == *"--max-time 20"* ]] || fail "curl call for $url did not set max time"
}

assert_payload_matches_expected(){
  local actual_file="$1" expected_file="$2" message="$3"
  local actual_sorted="$TEST_TMP/actual.sorted.json"
  local expected_sorted="$TEST_TMP/expected.sorted.json"

  jq -S . "$actual_file" > "$actual_sorted" || fail "actual payload was not valid JSON"
  jq -S . "$expected_file" > "$expected_sorted" || fail "expected payload was not valid JSON"
  diff -u "$expected_sorted" "$actual_sorted" > "$TEST_TMP/payload.diff" || fail "$message"
  rm -f "$TEST_TMP/payload.diff"
}

write_expected_legacy_github_payload(){
  local output_file="$1" image="$2" container="$3" repo="$4" tag="$5" current="$6" desc="$7" breaking="$8"
  local color="${9:-5763719}"
  local url version
  url="https://github.com/${repo}/releases/tag/${tag}"
  version="$tag"
  [[ -n "$current" ]] && version="${current} -> ${tag}"

  jq -n \
    --arg title "Release ${tag} for ${repo}" \
    --arg url "$url" \
    --arg desc "$desc" \
    --arg breaking "$breaking" \
    --arg image "$image" \
    --arg container "$container" \
    --arg repo "$repo" \
    --arg version "$version" \
    --arg links "[GitHub release](${url}) - [Full changelog](${url}#user-content-changes)" \
    --argjson color "$color" \
    '{
      username: "GitHub Release Notes",
      allowed_mentions: {parse: []},
      embeds: [{
        title: $title,
        url: $url,
        color: $color,
        description: $desc,
        fields: [
          {name: "Breaking", value: $breaking, inline: true},
          (if $image != "" then {name: "Image", value: $image, inline: true} else empty end),
          (if $container != "" then {name: "Container", value: $container, inline: true} else empty end),
          {name: "Repository", value: $repo, inline: true},
          {name: "Version", value: $version, inline: true},
          {name: "Links", value: $links, inline: false}
        ],
        footer: {text: "Built from GitHub Release"},
        timestamp: "2026-01-02T00:00:00Z"
      }]
    }' > "$output_file"
}

write_expected_legacy_lsio_payload(){
  local output_file="$1"
  local lsio_url="https://github.com/linuxserver/docker-radarr/releases/tag/5.1.0-ls1"
  local upstream_url="https://github.com/Radarr/Radarr/releases/tag/v5.1.0"

  jq -n \
    --arg title "linuxserver/docker-radarr -> Radarr v5.1.0" \
    --arg url "$upstream_url" \
    --arg desc $'`linuxserver/docker-radarr` - ls1 - Alpine 3.20\n\n**Key changes**\n- New queue view' \
    --arg links "[LSIO release](${lsio_url}) - [Upstream release](${upstream_url}) - [Full changelog](${upstream_url}#user-content-changes)" \
    --arg lsio_changes $'**Rebase**: Alpine 3.20\n- Add package\n' \
    '{
      username: "GitHub Release Notes",
      allowed_mentions: {parse: []},
      embeds: [{
        title: $title,
        url: $url,
        color: 5763719,
        description: $desc,
        fields: [
          {name: "LSIO Tag", value: "`5.1.0-ls1`", inline: true},
          {name: "Upstream Version", value: "`v5.1.0`", inline: true},
          {name: "LinuxServer Changes", value: $lsio_changes, inline: false},
          {name: "About", value: "**Repo**: Radarr/Radarr\n**Tag**: `v5.1.0`\n**Date**: 2026-01-02", inline: false},
          {name: "Links", value: $links, inline: false}
        ],
        footer: {text: "Built from LSIO Remote Changes"},
        timestamp: "2026-01-02T00:00:00Z"
      }]
    }' > "$output_file"
}

test_ghcr_image_uses_github_release_engine(){
  setup_case
  local payload_file="$TEST_TMP/payload.json"
  local image current repo tag breaking

  image="$(parity_value '.ghcr_major.image')"
  current="$(parity_value '.ghcr_major.current_tag')"
  repo="$(parity_value '.ghcr_major.repo')"
  tag="$(parity_value '.ghcr_major.release_tag')"
  breaking="$(parity_value 'if .ghcr_major.breaking then "yes" else "no" end')"

  FAKE_IMAGE_SOURCE="" run_notes "$image" "$current" "$payload_file"

  [[ -s "$payload_file" ]] || fail "webhook payload was not captured"
  assert_curl_policy_for_url "https://discord.test/webhook"
  jq -e '.allowed_mentions.parse == []' "$payload_file" >/dev/null || fail "allowed_mentions was not disabled"
  jq -e '.username == "GitHub Release Notes"' "$payload_file" >/dev/null || fail "release engine username missing"
  jq -e '.embeds[0].fields[] | select(.name == "Container" and .value == "container")' "$payload_file" >/dev/null || fail "container field was not preserved"
  jq -e --arg repo "$repo" '.embeds[0].fields[] | select(.name == "Repository" and .value == $repo)' "$payload_file" >/dev/null || fail "GHCR repo normalization changed"
  jq -e --arg version "$current -> $tag" '.embeds[0].fields[] | select(.name == "Version" and .value == $version)' "$payload_file" >/dev/null || fail "current to new field was not rendered"
  jq -e --arg breaking "$breaking" '.embeds[0].fields[] | select(.name == "Breaking" and .value == $breaking)' "$payload_file" >/dev/null || fail "major bump was not marked breaking"
  teardown_case
}

test_legacy_release_notes_oci_payload_matches_snapshot(){
  setup_case
  local payload_file="$TEST_TMP/payload.json"
  local expected_file="$TEST_TMP/expected.json"

  FAKE_IMAGE_SOURCE="https://github.com/acme/app" run_notes "docker.io/acme/app:1.0.0" "1.0.0" "$payload_file"
  write_expected_legacy_github_payload \
    "$expected_file" \
    "docker.io/acme/app:1.0.0" \
    "container" \
    "acme/app" \
    "v2.0.0" \
    "1.0.0" \
    $'`docker.io/acme/app:1.0.0` - v2.0.0\n\n**Key changes**\n- Routine maintenance' \
    "yes"

  assert_payload_matches_expected "$payload_file" "$expected_file" "release-notes OCI payload changed from legacy shape"
  teardown_case
}

test_direct_repo_arg_uses_github_release_engine(){
  setup_case
  local payload_file="$TEST_TMP/payload.json"

  PATH="$TEST_TMP/bin:$PATH" \
    FAKE_WEBHOOK_PAYLOAD="$payload_file" \
    FAKE_CURL_ARGS_LOG="$TEST_TMP/curl.args" \
    "$SCRIPT" --repo acme/app --webhook https://discord.test/webhook > "$TEST_TMP/output.log" 2>&1 || fail "direct repo release note script failed"

  [[ -s "$payload_file" ]] || fail "webhook payload was not captured"
  jq -e '.username == "GitHub Release Notes"' "$payload_file" >/dev/null || fail "release engine username missing"
  jq -e '.embeds[0].fields[] | select(.name == "Repository" and .value == "acme/app")' "$payload_file" >/dev/null || fail "direct repo was not used"
  jq -e '.embeds[0].fields[] | select(.name == "Links" and (.value | contains("GitHub release")))' "$payload_file" >/dev/null || fail "GitHub release link was not rendered"
  teardown_case
}

test_legacy_github_release_embed_wrapper_accepts_compat_args(){
  setup_case
  local payload_file="$TEST_TMP/payload.json"
  local expected_file="$TEST_TMP/expected.json"

  PATH="$TEST_TMP/bin:$PATH" \
    FAKE_WEBHOOK_PAYLOAD="$payload_file" \
    FAKE_CURL_ARGS_LOG="$TEST_TMP/curl.args" \
    "$GITHUB_EMBED" \
      --repo acme/app \
      --webhook https://discord.test/webhook \
      --image ghcr.io/acme/app:1.0.0 \
      --container container \
      --current-tag 1.0.0 \
      --max-commits 9 \
      --color 0x123456 \
      --debug > "$TEST_TMP/output.log" 2>&1 || fail "legacy github-release-embed wrapper failed"

  [[ -s "$payload_file" ]] || fail "webhook payload was not captured"
  jq -e '.embeds[0].color == 1193046' "$payload_file" >/dev/null || fail "legacy color option was not forwarded"
  write_expected_legacy_github_payload \
    "$expected_file" \
    "ghcr.io/acme/app:1.0.0" \
    "container" \
    "acme/app" \
    "v2.0.0" \
    "1.0.0" \
    $'`ghcr.io/acme/app:1.0.0` - v2.0.0\n\n**Key changes**\n- Routine maintenance' \
    "yes" \
    1193046
  assert_payload_matches_expected "$payload_file" "$expected_file" "github-release-embed compatibility payload changed from legacy shape"
  teardown_case
}

test_oci_source_label_uses_github_release_engine(){
  setup_case
  local payload_file="$TEST_TMP/payload.json"
  local image current source repo

  image="$(parity_value '.oci_source.image')"
  current="$(parity_value '.oci_source.current_tag')"
  source="$(parity_value '.oci_source.source')"
  repo="$(parity_value '.oci_source.repo')"

  FAKE_IMAGE_SOURCE="$source" run_notes "$image" "$current" "$payload_file"

  [[ -s "$payload_file" ]] || fail "webhook payload was not captured"
  jq -e --arg repo "$repo" '.embeds[0].fields[] | select(.name == "Repository" and .value == $repo)' "$payload_file" >/dev/null || fail "OCI source label repo was not used"
  teardown_case
}

test_legacy_release_notes_linuxserver_payload_matches_snapshot_without_upstream_map(){
  setup_case
  local payload_file="$TEST_TMP/payload.json"
  local expected_file="$TEST_TMP/expected.json"
  : > "$TEST_TMP/upstreams.txt"

  FAKE_IMAGE_SOURCE="" run_notes "linuxserver/radarr:latest" "latest" "$payload_file"

  [[ -s "$payload_file" ]] || fail "webhook payload was not captured"
  write_expected_legacy_github_payload \
    "$expected_file" \
    "linuxserver/radarr:latest" \
    "container" \
    "linuxserver/docker-radarr" \
    "5.1.0-ls1" \
    "latest" \
    $'`linuxserver/radarr:latest` - 5.1.0-ls1\n\n**Key changes**\n- Rebase to Alpine 3.20\n- Add package\n- Updating to 5.1.0' \
    "no"
  assert_payload_matches_expected "$payload_file" "$expected_file" "release-notes LinuxServer payload changed from legacy fallback shape"
  teardown_case
}

test_image_only_mapping_uses_container_release_for_all_routes(){
  local name mode repo payload_file kind remote
  for name in socket-proxy image-only-example; do
    for mode in auto auto_digest explicit manager manager_digest; do
      setup_case
      kind=tag
      remote=3.4.4-r0-ls97
      if [[ "$mode" == *_digest ]]; then
        kind=digest
        remote=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      fi
      repo="linuxserver/docker-$name"
      payload_file="$TEST_TMP/payload.json"
      printf '%s: %s\n' "$repo" "$repo" > "$TEST_TMP/upstreams.txt"
      if [[ "$mode" == auto* ]]; then
        FAKE_IMAGE_SOURCE="https://github.com/acme/app" \
          update_kind_kind="$kind" update_kind_remote_value="$remote" \
          result_tag=3.4.4-r0-ls97 \
          run_notes "ghcr.io/linuxserver/$name:latest" "latest" "$payload_file"
      elif [[ "$mode" == explicit ]]; then
        PATH="$TEST_TMP/bin:$PATH" \
          DISCORD_WEBHOOK=https://discord.test/webhook \
          FAKE_WEBHOOK_PAYLOAD="$payload_file" \
          FAKE_CURL_ARGS_LOG="$TEST_TMP/curl.args" \
          "$GITHUB_EMBED" --provider lsio --lsio "$repo" --upstream "$repo" \
          --tag 3.4.4-r0-ls97 > "$TEST_TMP/output.log" 2>&1 || fail "self-mapped wrapper failed"
      else
        PATH="$TEST_TMP/bin:$PATH" \
          DISCORD_WEBHOOK=https://discord.test/webhook \
          FAKE_WEBHOOK_PAYLOAD="$payload_file" \
          FAKE_CURL_ARGS_LOG="$TEST_TMP/curl.args" \
          UPSTREAM_MAP="$TEST_TMP/upstreams.txt" LOG_DIR="$TEST_TMP/logs" \
          update_available=true image_name="linuxserver/$name" \
          image_registry_url=https://ghcr.io update_kind_kind="$kind" \
          update_kind_remote_value="$remote" result_tag=3.4.4-r0-ls97 \
          "$TAG_MANAGER" > "$TEST_TMP/output.log" 2>&1 || fail "self-mapped tag-manager failed"
      fi
      jq -e --arg repo "$repo" \
        '.embeds[0].fields[] | select(.name == "Repository" and .value == $repo)' \
        "$payload_file" >/dev/null || fail "$mode used the wrong release repository"
      jq -e '.embeds[0].description | contains("Routine image maintenance")' \
        "$payload_file" >/dev/null || fail "$mode omitted image release notes"
      grep -F "$repo/releases/tags/3.4.4-r0-ls97" "$TEST_TMP/curl.args" >/dev/null || fail "$mode lost the image build tag"
      ! grep -F '/releases/latest' "$TEST_TMP/curl.args" >/dev/null || fail "$mode ignored the requested release tag"
      teardown_case
    done
  done
}

test_legacy_tag_manager_missing_lsio_mapping_posts_admin_only(){
  setup_case
  local admin_payload_file="$TEST_TMP/admin-payload.json"
  : > "$TEST_TMP/upstreams.txt"

  PATH="$TEST_TMP/bin:$PATH" \
    DISCORD_WEBHOOK="https://discord.test/webhook" \
    ADMIN_WEBHOOK="https://discord.test/admin" \
    FAKE_WEBHOOK_PAYLOAD="$TEST_TMP/payload.json" \
    FAKE_ADMIN_PAYLOAD="$admin_payload_file" \
    FAKE_CURL_ARGS_LOG="$TEST_TMP/curl.args" \
    LOG_DIR="$TEST_TMP/logs" \
    RELEASE_EMBED="$GITHUB_EMBED" \
    UPSTREAM_MAP="$TEST_TMP/upstreams.txt" \
    image_name="linuxserver/radarr" \
    image_registry_url="docker.io" \
    update_available="true" \
    update_kind_kind="" \
    update_kind_remote_value="" \
    result_tag="" \
    "$TAG_MANAGER" > "$TEST_TMP/output.log" 2>&1 || fail "missing mapping tag-manager script failed"

  [[ -s "$admin_payload_file" ]] || fail "admin webhook payload was not captured"
  [[ ! -s "$TEST_TMP/payload.json" ]] || fail "normal webhook received a missing-mapping notice"
  jq -e '.content | contains("Missing upstream mapping")' "$admin_payload_file" >/dev/null || fail "admin missing-mapping alert was not sent"
  teardown_case
}

test_legacy_tag_manager_ghcr_env_uses_wrapper(){
  setup_case
  local payload_file="$TEST_TMP/payload.json"
  local expected_file="$TEST_TMP/expected.json"

  PATH="$TEST_TMP/bin:$PATH" \
    DISCORD_WEBHOOK="https://discord.test/webhook" \
    FAKE_WEBHOOK_PAYLOAD="$payload_file" \
    FAKE_CURL_ARGS_LOG="$TEST_TMP/curl.args" \
    LOG_DIR="$TEST_TMP/logs" \
    RELEASE_EMBED="$GITHUB_EMBED" \
    image_name="acme/app" \
    image_registry_url="ghcr.io" \
    update_available="true" \
    update_kind_kind="tag" \
    update_kind_remote_value="2.0.0" \
    result_tag="" \
    "$TAG_MANAGER" > "$TEST_TMP/output.log" 2>&1 || fail "legacy tag-manager GHCR wrapper failed"

  [[ -s "$payload_file" ]] || fail "webhook payload was not captured"
  ! grep -q 'discord.test/webhook' "$TEST_TMP/output.log" || fail "legacy tag-manager leaked webhook URL"
  write_expected_legacy_github_payload \
    "$expected_file" \
    "" \
    "" \
    "acme/app" \
    "v2.0.0" \
    "" \
    $'`acme/app` - v2.0.0\n\n**Key changes**\n- Routine maintenance' \
    "no"
  assert_payload_matches_expected "$payload_file" "$expected_file" "tag-manager GHCR payload changed from legacy shape"
  teardown_case
}

test_legacy_tag_manager_lsio_env_uses_upstream_map(){
  setup_case
  local payload_file="$TEST_TMP/payload.json"
  local expected_file="$TEST_TMP/expected.json"
  local image_name

  image_name="$(parity_value '.lsio_radarr.image')"
  image_name="${image_name%%:*}"
  PATH="$TEST_TMP/bin:$PATH" \
    DISCORD_WEBHOOK="https://discord.test/webhook" \
    FAKE_WEBHOOK_PAYLOAD="$payload_file" \
    FAKE_CURL_ARGS_LOG="$TEST_TMP/curl.args" \
    LOG_DIR="$TEST_TMP/logs" \
    RELEASE_EMBED="$GITHUB_EMBED" \
    UPSTREAM_MAP="$TEST_TMP/upstreams.txt" \
    image_name="$image_name" \
    image_registry_url="docker.io" \
    update_available="true" \
    update_kind_kind="" \
    update_kind_remote_value="" \
    result_tag="" \
    "$TAG_MANAGER" > "$TEST_TMP/output.log" 2>&1 || fail "legacy tag-manager LSIO wrapper failed"

  [[ -s "$payload_file" ]] || fail "webhook payload was not captured"
  write_expected_legacy_lsio_payload "$expected_file"
  assert_payload_matches_expected "$payload_file" "$expected_file" "tag-manager LSIO payload changed from legacy shape"
  teardown_case
}

test_missing_source_posts_minimal_notice(){
  setup_case
  local payload_file="$TEST_TMP/payload.json"

  FAKE_IMAGE_SOURCE="" run_notes "docker.io/library/redis:latest" "latest" "$payload_file"

  [[ -s "$payload_file" ]] || fail "webhook payload was not captured"
  assert_curl_policy_for_url "https://discord.test/webhook"
  jq -e '.allowed_mentions.parse == []' "$payload_file" >/dev/null || fail "minimal notice did not disable mentions"
  jq -e '.embeds[0].title == "Update available: docker.io/library/redis:latest"' "$payload_file" >/dev/null || fail "minimal notice title was wrong"
  jq -e '.embeds[0].description == "No GitHub source label found. Unable to fetch release notes."' "$payload_file" >/dev/null || fail "minimal notice description was wrong"
  teardown_case
}

test_missing_source_webhook_failure_is_nonzero_and_redacted(){
  setup_case
  local payload_file="$TEST_TMP/payload.json"

  if PATH="$TEST_TMP/bin:$PATH" \
    DISCORD_WEBHOOK="https://discord.test/fail/secret-token" \
    FAKE_WEBHOOK_PAYLOAD="$payload_file" \
    FAKE_CURL_ARGS_LOG="$TEST_TMP/curl.args" \
    FAKE_IMAGE_SOURCE="" \
    "$SCRIPT" "docker.io/library/redis:latest" "container" "latest" > "$TEST_TMP/output.log" 2>&1; then
    fail "minimal notice webhook failure returned success"
  fi
  assert_curl_policy_for_url "https://discord.test/fail/secret-token"
  grep -q 'Discord webhook error 500' "$TEST_TMP/output.log" || fail "webhook failure message missing"
  ! grep -q 'secret-token' "$TEST_TMP/output.log" || fail "webhook secret leaked"
  teardown_case
}

run_test(){
  local name="$1"
  printf 'running %s\n' "$name"
  "$name"
  printf 'ok - %s\n' "$name"
}

main(){
  run_test test_ghcr_image_uses_github_release_engine
  run_test test_legacy_release_notes_oci_payload_matches_snapshot
  run_test test_direct_repo_arg_uses_github_release_engine
  run_test test_legacy_github_release_embed_wrapper_accepts_compat_args
  run_test test_oci_source_label_uses_github_release_engine
  run_test test_legacy_release_notes_linuxserver_payload_matches_snapshot_without_upstream_map
  run_test test_legacy_tag_manager_missing_lsio_mapping_posts_admin_only
  run_test test_image_only_mapping_uses_container_release_for_all_routes
  run_test test_legacy_tag_manager_ghcr_env_uses_wrapper
  run_test test_legacy_tag_manager_lsio_env_uses_upstream_map
  run_test test_missing_source_posts_minimal_notice
  run_test test_missing_source_webhook_failure_is_nonzero_and_redacted
}

trap teardown_case EXIT
main "$@"
