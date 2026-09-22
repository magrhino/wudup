"""GitHub transport, source discovery, and GitHub/LinuxServer release lookup.

Owns request bounds, provider fallback order, and release classification. This
module does not access SQLite or decide cache freshness or security urgency."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from .images import image_repo_ref
from .lsio_updates import (
    LSIOUpdateClassification,
    classify_lsio_update,
    normalize_lsio_version,
    parse_lsio_tag,
)
from .release_note_models import (
    ReleaseNoteContext,
    ReleaseNoteInfo,
    ReleaseNoteLink,
)

DEFAULT_GITHUB_TIMEOUT_SECONDS = 6.0
LSIO_RELEASE_SCAN_MAX_PAGES = 10
GITHUB_HOST = "github.com"
UPSTREAM_MAP_FILENAME = "upstreams.txt"
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SEMVER_RE = re.compile(
    r"(?<![0-9A-Za-z])v?([0-9]+)(?:\.[0-9]+){1,3}"
    r"(?:[._-][0-9A-Za-z]+)*(?![0-9A-Za-z])"
)
COMPOSITE_UPSTREAM_RE = re.compile(r"^([vV]?\d+(?:\.\d+){1,3})_v?\d", re.ASCII)
BREAKING_RE = re.compile(
    r"breaking|migration|incompatible|manual step|major change|"
    r"requires [^ \n]+ [0-9]|deprecated[^.\n]*remov|remove[ds] feature",
    re.IGNORECASE,
)
GHSA_ID_RE = re.compile(
    r"^GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}$",
    re.IGNORECASE | re.ASCII,
)
CVE_ID_RE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE | re.ASCII)
SECURITY_ADVISORY_FETCH_MAX = 4


class GitHubClient:
    """Small GitHub Releases API client using only the standard library."""

    def __init__(
        self,
        *,
        token: str = "",
        timeout: float = DEFAULT_GITHUB_TIMEOUT_SECONDS,
        fetch_json: Callable[[str], object] | None = None,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self._fetch_json = fetch_json

    def get_json(self, url: str) -> object:
        if self._fetch_json is not None:
            return self._fetch_json(url)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "wudup-webui-release-notes/1.0",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"message": "Not Found"}
            raise

    def repository_advisory(self, repo: str, advisory_id: str) -> object:
        if not _github_repo_valid(repo) or not GHSA_ID_RE.fullmatch(advisory_id):
            raise ValueError("invalid GitHub repository advisory lookup")
        return self.get_json(
            f"https://api.github.com/repos/{repo}/security-advisories/"
            f"{advisory_id.upper()}"
        )

    def global_advisory(self, advisory_id: str) -> object:
        if not GHSA_ID_RE.fullmatch(advisory_id):
            raise ValueError("invalid GitHub advisory lookup")
        return self.get_json(
            f"https://api.github.com/advisories/{advisory_id.upper()}"
        )

    def global_advisories_for_cve(self, cve_id: str) -> object:
        if not CVE_ID_RE.fullmatch(cve_id):
            raise ValueError("invalid CVE advisory lookup")
        query = urllib.parse.urlencode(
            {"cve_id": cve_id.upper(), "per_page": str(SECURITY_ADVISORY_FETCH_MAX)}
        )
        return self.get_json(f"https://api.github.com/advisories?{query}")


def detect_breaking(body: str, current_tag: str, release_tag: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if BREAKING_RE.search(body):
        reasons.append("Release notes mention a migration, incompatibility, or removal.")
    current_major = _semver_major(current_tag)
    release_major = _semver_major(release_tag)
    if (
        current_major is not None
        and release_major is not None
        and release_major > current_major
    ):
        reasons.append(f"Major version changes from {current_major} to {release_major}.")
    return bool(reasons), reasons


def _github_release_link_tag(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.netloc.lower() != GITHUB_HOST:
        return ""
    marker = "/releases/tag/"
    if marker not in parsed.path:
        return ""
    tag = parsed.path.split(marker, 1)[1].strip("/")
    return urllib.parse.unquote(tag)


def _fetch_github_release_note(
    context: ReleaseNoteContext,
    client: GitHubClient,
    timestamp: str,
) -> ReleaseNoteInfo:
    release = _fetch_release(client, context.upstream_repo, context.target_tag)
    if release is None:
        project_url = _project_url(client, context.upstream_repo)
        return ReleaseNoteInfo(
            line_no=context.line_no,
            status="not_found",
            provider=context.provider,
            image_repo=context.image_repo,
            upstream_repo=context.upstream_repo,
            release_tag=context.target_tag,
            title=f"{context.upstream_repo} releases",
            links=[ReleaseNoteLink("GitHub project", project_url, "github_project")],
            refreshed_at=timestamp,
        )
    body = str(release.get("body") or "")
    release_tag = str(release.get("tag_name") or "")
    breaking, reasons = detect_breaking(body, context.current_tag, release_tag)
    return ReleaseNoteInfo(
        line_no=context.line_no,
        status="ready",
        provider=context.provider,
        image_repo=context.image_repo,
        upstream_repo=context.upstream_repo,
        release_tag=release_tag,
        title=str(release.get("name") or release_tag),
        published_at=str(release.get("published_at") or release.get("created_at") or ""),
        breaking=breaking,
        breaking_reasons=reasons,
        links=[
            ReleaseNoteLink(
                "GitHub release",
                str(release.get("html_url") or _github_url(context.upstream_repo)),
                "github_release",
            )
        ],
        refreshed_at=timestamp,
        body=body,
    )


def _fetch_lsio_release_note(
    context: ReleaseNoteContext,
    client: GitHubClient,
    timestamp: str,
) -> ReleaseNoteInfo:
    lsio_release = _fetch_lsio_release(client, context)
    if lsio_release is None:
        return ReleaseNoteInfo(
            line_no=context.line_no,
            status="error",
            provider=context.provider,
            image_repo=context.image_repo,
            upstream_repo=context.upstream_repo,
            refreshed_at=timestamp,
            error=f"LSIO release not found for {context.image_repo}",
            classification=_classify_context(context),
        )
    lsio_body = str(lsio_release.get("body") or "")
    lsio_tag = str(lsio_release.get("tag_name") or "")
    upstream_version = _lsio_context_upstream_version(context, lsio_body, lsio_tag)
    classification = _classify_context(
        context,
        lsio_tag=lsio_tag,
        upstream_version=upstream_version,
    )
    links = [
        ReleaseNoteLink(
            "LSIO release",
            str(lsio_release.get("html_url") or _github_url(context.image_repo)),
            "lsio_release",
        )
    ]
    breaking, reasons = detect_breaking(lsio_body, context.current_tag, lsio_tag)
    lsio_info = ReleaseNoteInfo(
        line_no=context.line_no,
        status="ready",
        provider=context.provider,
        image_repo=context.image_repo,
        upstream_repo=context.upstream_repo,
        release_tag=lsio_tag,
        title=str(lsio_release.get("name") or lsio_tag),
        published_at=str(
            lsio_release.get("published_at")
            or lsio_release.get("created_at")
            or ""
        ),
        breaking=breaking,
        breaking_reasons=reasons,
        links=links,
        refreshed_at=timestamp,
        body=lsio_body,
        classification=classification,
    )
    if context.upstream_repo == context.image_repo or _lsio_only_update(
        context, classification
    ):
        return lsio_info
    upstream_release = _fetch_lsio_upstream_release(
        client,
        context.upstream_repo,
        upstream_version,
    )
    if upstream_release is None:
        return lsio_info
    body = str(upstream_release.get("body") or "")
    release_tag = str(upstream_release.get("tag_name") or "")
    breaking, reasons = detect_breaking(
        "\n".join([body, lsio_body]),
        context.current_tag,
        release_tag,
    )
    links.append(
        ReleaseNoteLink(
            "Upstream release",
            str(upstream_release.get("html_url") or _github_url(context.upstream_repo)),
            "github_release",
        )
    )
    return ReleaseNoteInfo(
        line_no=context.line_no,
        status="ready",
        provider=context.provider,
        image_repo=context.image_repo,
        upstream_repo=context.upstream_repo,
        release_tag=release_tag,
        title=str(upstream_release.get("name") or release_tag),
        published_at=str(
            upstream_release.get("published_at")
            or upstream_release.get("created_at")
            or ""
        ),
        breaking=breaking,
        breaking_reasons=reasons,
        links=links,
        refreshed_at=timestamp,
        body="\n\n".join(part for part in (body, lsio_body) if part),
        classification=classification,
    )


def _lsio_only_update(
    context: ReleaseNoteContext,
    classification: LSIOUpdateClassification,
) -> bool:
    """Return true for classified rebuilds and same-tag digest-only updates."""

    if classification.change_type == "image_rebuild":
        return True
    return bool(
        context.target_digest
        and context.current_tag
        and context.current_tag.lower() == context.target_tag.lower()
    )


def _fetch_lsio_release(
    client: GitHubClient,
    context: ReleaseNoteContext,
) -> dict[str, Any] | None:
    arch, branch, upstream_version, build_suffix = _lsio_release_target(context)
    target = parse_lsio_tag(context.target_tag)
    if not branch:
        if target.kind == "build" and target.build_suffix:
            url = (
                f"https://api.github.com/repos/{context.image_repo}/"
                f"releases/tags/{context.target_tag}"
            )
            return _object_or_none(client.get_json(url))
        if not arch:
            return _fetch_latest(client, context.image_repo)
    for page in range(1, LSIO_RELEASE_SCAN_MAX_PAGES + 1):
        releases = _object_list(
            client.get_json(_lsio_releases_url(context.image_repo, page))
        )
        release = _matching_lsio_release(
            releases,
            arch=arch,
            branch=branch,
            upstream_version=upstream_version,
            build_suffix=build_suffix,
        )
        if release is not None:
            return release
        if len(releases) < 30:
            return None
    return None


def _matching_lsio_release(
    releases: Iterable[dict[str, Any]],
    *,
    arch: str,
    branch: str,
    upstream_version: str,
    build_suffix: str,
) -> dict[str, Any] | None:
    for release in releases:
        parts = parse_lsio_tag(str(release.get("tag_name") or ""))
        if parts.arch.lower() != arch.lower():
            continue
        if parts.branch != branch:
            continue
        if upstream_version and normalize_lsio_version(
            parts.upstream_version
        ) != normalize_lsio_version(upstream_version):
            continue
        if build_suffix and parts.build_suffix != build_suffix:
            continue
        return release
    return None


def _lsio_releases_url(repo: str, page: int) -> str:
    url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
    if page > 1:
        url = f"{url}&page={page}"
    return url


def _lsio_release_target(context: ReleaseNoteContext) -> tuple[str, str, str, str]:
    target = parse_lsio_tag(context.target_tag)
    if target.kind in {"build", "version"} and (target.arch or target.branch):
        return (
            target.arch,
            target.branch,
            target.upstream_version,
            target.build_suffix,
        )
    arch, branch = _lsio_tracking_target(context)
    return arch, branch, "", ""


def _lsio_tracking_target(context: ReleaseNoteContext) -> tuple[str, str]:
    for tag in (context.target_tag, context.current_tag):
        parts = parse_lsio_tag(tag)
        if parts.kind in {"build", "version"} and (parts.arch or parts.branch):
            return parts.arch, parts.branch
    return "", ""


def _lsio_context_upstream_version(
    context: ReleaseNoteContext,
    lsio_body: str,
    lsio_tag: str,
) -> str:
    target = parse_lsio_tag(context.target_tag)
    if target.kind in {"build", "version"}:
        return target.upstream_version
    return _lsio_upstream_version(lsio_body, lsio_tag)


def _fetch_lsio_upstream_release(
    client: GitHubClient,
    repo: str,
    tag: str,
) -> dict[str, Any] | None:
    release = _fetch_release(client, repo, tag)
    if release is not None:
        return release
    fallback = _composite_upstream_base(tag)
    if fallback and fallback != tag:
        return _fetch_release(client, repo, fallback)
    return None


def _classify_context(
    context: ReleaseNoteContext,
    *,
    lsio_tag: str = "",
    upstream_version: str = "",
) -> LSIOUpdateClassification:
    return classify_lsio_update(
        image_repo=context.image_repo,
        current_tag=context.current_tag,
        target_tag=context.target_tag,
        lsio_tag=lsio_tag,
        upstream_version=upstream_version,
    )


def _fetch_release(
    client: GitHubClient,
    repo: str,
    tag: str,
) -> dict[str, Any] | None:
    if tag.lower() == "latest":
        return _fetch_latest(client, repo)
    if tag:
        candidates = [tag] if tag[:1].lower() == "v" else [f"v{tag}", tag]
        for candidate in candidates:
            release = _object_or_none(
                client.get_json(f"https://api.github.com/repos/{repo}/releases/tags/{candidate}")
            )
            if release is not None:
                return release
        return None
    return _fetch_latest(client, repo)


def _fetch_latest(client: GitHubClient, repo: str) -> dict[str, Any] | None:
    return _object_or_none(
        client.get_json(f"https://api.github.com/repos/{repo}/releases/latest")
    )


def _project_url(client: GitHubClient, repo: str) -> str:
    repo_json = _object_or_none(client.get_json(f"https://api.github.com/repos/{repo}"))
    if repo_json is not None:
        html_url = str(repo_json.get("html_url") or "")
        if html_url.startswith("https://github.com/"):
            return html_url
    return _github_url(repo)


def _object_or_none(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if str(value.get("message") or "") == "Not Found":
        return None
    return value


def _object_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _lsio_upstream_version(body: str, lsio_tag: str) -> str:
    remote = _extract_block(body, "remote changes:")
    match = re.search(r"[Uu]pdat(?:e|ing)[^0-9vV]*(v?[0-9][0-9A-Za-z._-]*)", remote)
    if match:
        found = _first_semver(match.group(1))
        if found:
            return found
    return _strip_lsio_suffix(_first_semver(lsio_tag))


def _extract_block(body: str, header: str) -> str:
    target = header.lower()
    lines = body.replace("\r", "").splitlines()
    collecting = False
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        header = _block_header_text(stripped)
        is_header = bool(
            header
            and not _markdown_bullet(stripped)
            and re.fullmatch(r"[A-Za-z0-9 _-]+:", header)
        )
        if header.lower() == target:
            collecting = True
            result.append(line)
            continue
        if collecting and is_header:
            break
        if collecting:
            result.append(line)
    return "\n".join(result)


def _block_header_text(value: str) -> str:
    header = re.sub(r"^#{1,6}\s*", "", value.strip())
    match = re.fullmatch(r"\*\*([^*]+:)\*\*", header)
    if match:
        header = match.group(1)
    return header.strip()


def _markdown_bullet(value: str) -> bool:
    return bool(re.match(r"^[*+•-]\s+", value))


def _load_upstream_map(environ: Mapping[str, str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for path in _upstream_map_paths(environ):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            key = key.strip()
            value = value.strip()
            if _github_repo_valid(key) and _github_repo_valid(value):
                mapping[key] = value
        if mapping:
            return mapping
    return mapping


def _upstream_map_paths(environ: Mapping[str, str]) -> list[Path]:
    paths: list[Path] = []
    for name in ("WUD_WEB_UPSTREAM_MAP", "UPSTREAM_MAP"):
        value = environ.get(name, "")
        if value:
            paths.append(Path(value))
    scripts_dir = environ.get("WUD_SCRIPTS_DIR", "/managed-wud")
    if scripts_dir:
        paths.append(Path(scripts_dir) / UPSTREAM_MAP_FILENAME)
    app_dir = environ.get("WUD_APP_DIR", "/app")
    if app_dir:
        paths.append(Path(app_dir) / "wud" / UPSTREAM_MAP_FILENAME)
    paths.append(Path("/app/wud/upstreams.txt"))
    paths.append(Path(__file__).resolve().parents[2] / "wud" / UPSTREAM_MAP_FILENAME)
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def _lsio_repo(repo: str) -> str:
    if not repo.startswith("linuxserver/"):
        return ""
    name = repo.split("/", 1)[1]
    if not name:
        return ""
    if name.startswith("docker-"):
        return f"linuxserver/{name}"
    return f"linuxserver/docker-{name}"


def github_repo_from_ghcr_image(image: str) -> str:
    repo = image_repo_ref(image)
    registry, sep, candidate = repo.partition("/")
    if registry.lower() != "ghcr.io" or not sep:
        return ""
    return candidate if _github_repo_valid(candidate) else ""


def github_repo_from_source(source: str) -> str:
    return _github_source_repo(source)


def _github_source_repo(source: str) -> str:
    value = source.strip()
    if not value or GITHUB_HOST not in value.lower():
        return ""
    if value.startswith("git@github.com:"):
        candidate = value.removeprefix("git@github.com:")
    else:
        parse_value = value if "://" in value else f"//{value}"
        parsed = urllib.parse.urlsplit(parse_value)
        if parsed.netloc.lower() != GITHUB_HOST:
            return ""
        candidate = parsed.path.lstrip("/")
    candidate = candidate.removesuffix(".git").strip("/")
    parts = [part for part in candidate.split("/") if part]
    if len(parts) < 2:
        return ""
    repo = f"{parts[0]}/{parts[1].removesuffix('.git')}"
    return repo if _github_repo_valid(repo) else ""


def _github_repo_valid(value: str) -> bool:
    return bool(GITHUB_REPO_RE.fullmatch(value))


def _github_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def _first_semver(value: str) -> str:
    match = SEMVER_RE.search(value)
    return match.group(0) if match else ""


def _strip_lsio_suffix(value: str) -> str:
    return re.sub(r"(?i)[._-]ls[0-9]+(?:[._-][0-9a-z]+)*$", "", value)


def _composite_upstream_base(value: str) -> str:
    match = COMPOSITE_UPSTREAM_RE.match(value)
    return match.group(1) if match else ""


def _semver_major(value: str) -> int | None:
    match = SEMVER_RE.search(value)
    if not match:
        return None
    return int(match.group(1))
