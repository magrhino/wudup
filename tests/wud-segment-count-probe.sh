#!/usr/bin/env bash
# Opt-in probe for an upstream WUD tag-candidate bug; not part of tests/run-all.sh.
#
# WUD's getTagCandidates keeps only tags with the same number of dotted version
# parts as the running tag, even when wud.tag.include admits others. So a
# container on LinuxServer Jellyfin 10.11.11ubu2604-ls47 is never offered
# 12.3ubu2604-ls56. This runs that function from a released WUD image against
# a fixed snapshot of real Jellyfin tags (no registry access) and passes only
# once WUD offers the newer, differently shaped release.
#
# Usage: tests/wud-segment-count-probe.sh [wud-image-tag ...]   (default: 9.6.0)
set -Eeuo pipefail

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  printf 'skip - Docker is not available\n' >&2
  exit 0
fi

PROBE_JS="$(cat <<'JS'
const { getTagCandidates } = require('/home/node/app/dist/watchers/providers/docker/Docker.js');
const log = { warn() {}, debug() {}, info() {} };
const include = String.raw`^\d+(?:\.\d+)+ubu\d+-ls\d+$`;
const tags = [
  '10.11.11ubu2604-ls45', '10.11.11ubu2604-ls46', '10.11.11ubu2604-ls47',
  '12.0ubu2604-ls48', '12.1ubu2604-ls49', '12.1ubu2604-ls50', '12.1ubu2604-ls51',
  '12.2ubu2604-ls52', '12.2ubu2604-ls53', '12.2ubu2604-ls54', '12.2ubu2604-ls55',
  '12.3ubu2604-ls56',
];
const cases = [
  // Control: same part count, so WUD already handles it.
  { name: 'same part count 12.2 -> 12.3', current: '12.2ubu2604-ls55' },
  // The bug: three-part 10.11.11 never sees the two-part 12.x releases.
  { name: 'part count change 10.11.11 -> 12.3', current: '10.11.11ubu2604-ls47' },
];
let failed = 0;
for (const c of cases) {
  const container = { name: 'jellyfin', includeTags: include, image: { tag: { value: c.current, semver: true } } };
  const best = getTagCandidates(container, tags, log)[0] || '(none)';
  const ok = best === '12.3ubu2604-ls56';
  if (!ok) failed += 1;
  console.log(`${ok ? 'ok' : 'not ok'} - ${c.name}: best candidate ${best}, expected 12.3ubu2604-ls56`);
}
process.exit(failed ? 1 : 0);
JS
)"

status=0
for wud_tag in "${@:-9.6.0}"; do
  printf '# ghcr.io/getwud/wud:%s\n' "$wud_tag"
  docker run --rm --quiet --entrypoint node "ghcr.io/getwud/wud:$wud_tag" -e "$PROBE_JS" || status=1
done
exit "$status"
