"""Guided first-run configuration generation for WUDup."""

from __future__ import annotations

import argparse
import os
import shlex
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse

from ruamel.yaml import YAML

from .config import DEFAULT_TIMEZONE
from .naming import CONFIG_DIR_NAME, DB_FILENAME, DISPLAY_NAME, TECHNICAL_NAME

PROFILES = ("webui", "helper", "hardened")
WEB_EXPOSURES = ("loopback", "lan", "reverse-proxy")
DEFAULT_WEB_PORT = "7417"
DEFAULT_UID_GID = "1000"


class InitConfigError(ValueError):
    """Raised when init answers or output paths are unsafe."""


@dataclass(frozen=True)
class InitAnswers:
    profile: str
    config_file: Path
    compose_override: Path | None
    stack_root: Path
    log_dir: Path
    db_path: Path
    uid: str
    gid: str
    web_exposure: str
    web_bind: str
    web_port: str
    public_origin: str
    allowed_hosts: str
    trusted_proxies: str
    enable_web_mutations: bool
    backup_existing: bool = False
    dry_run: bool = False
    no_doctor: bool = False
    no_color: bool = False
    non_interactive: bool = False


@dataclass(frozen=True)
class _InitOutputPaths:
    config_file: Path
    compose_override: Path | None
    log_dir: Path
    db_path: Path


@dataclass(frozen=True)
class GeneratedFile:
    path: Path
    content: str
    mode: int


@dataclass(frozen=True)
class InitResult:
    answers: InitAnswers
    generated_files: tuple[GeneratedFile, ...]
    backups: tuple[Path, ...]
    doctor_status: int | None


class InitPrompter:
    def __init__(
        self,
        *,
        input_func: Callable[[str], str] | None = None,
        stream: object = sys.stdout,
    ) -> None:
        self.input_func = input if input_func is None else input_func
        self.stream = stream

    def choice(self, question: str, choices: Sequence[str], default: str) -> str:
        choice_list = ", ".join(
            f"{choice}{' (default)' if choice == default else ''}"
            for choice in choices
        )
        while True:
            answer = self.input_func(f"{question} [{choice_list}]: ").strip()
            if answer == "":
                return default
            if answer in choices:
                return answer
            print(f"Choose one of: {', '.join(choices)}", file=self.stream)

    def text(self, question: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        answer = self.input_func(f"{question}{suffix}: ").strip()
        return answer or default

    def yes_no(self, question: str, default: bool = False) -> bool:
        default_label = "Y/n" if default else "y/N"
        while True:
            answer = self.input_func(f"{question} [{default_label}]: ").strip().lower()
            if answer == "":
                return default
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            print("Answer yes or no.", file=self.stream)


def run_init_from_namespace(
    args: argparse.Namespace,
    *,
    repo_root: str | Path,
    environ: Mapping[str, str] | None = None,
    input_func: Callable[[str], str] | None = None,
) -> int:
    env = dict(os.environ if environ is None else environ)
    prompter = InitPrompter(input_func=input_func)
    try:
        answers = answers_from_namespace(args, environ=env, prompter=prompter)
        result = run_init(answers, repo_root=repo_root, environ=env)
    except InitConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    _print_result(result)
    return result.doctor_status if result.doctor_status is not None else 0


def answers_from_namespace(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    prompter: InitPrompter | None = None,
) -> InitAnswers:
    non_interactive = bool(getattr(args, "non_interactive", False))
    prompt = prompter or InitPrompter()
    home = Path(environ.get("HOME") or str(Path.home()))

    profile = _resolve_profile(args, non_interactive, prompt)
    stack_root = _resolve_stack_root(
        args,
        home,
        profile,
        non_interactive,
        prompt,
    )
    output_paths = _resolve_output_paths(
        args,
        home,
        profile,
        non_interactive,
        prompt,
    )
    uid, gid = _resolve_init_uid_gid(
        args,
        environ,
        profile,
        non_interactive,
        prompt,
    )
    web_exposure = _resolve_web_exposure(args, profile, non_interactive, prompt)
    web_bind = _resolve_web_bind(args, web_exposure)
    web_port = str(getattr(args, "web_port", "") or DEFAULT_WEB_PORT)
    public_origin = str(getattr(args, "public_origin", "") or "")
    allowed_hosts = str(getattr(args, "allowed_hosts", "") or "")
    trusted_proxies = str(getattr(args, "trusted_proxies", "") or "")

    if profile == "webui":
        web_bind, public_origin, allowed_hosts, trusted_proxies = _resolve_web_values(
            web_exposure=web_exposure,
            web_bind=web_bind,
            public_origin=public_origin,
            allowed_hosts=allowed_hosts,
            trusted_proxies=trusted_proxies,
            non_interactive=non_interactive,
            prompter=prompt,
        )

    _validate_port(web_port)
    _validate_uid_gid(uid, gid)

    return InitAnswers(
        profile=profile,
        config_file=output_paths.config_file,
        compose_override=output_paths.compose_override,
        stack_root=stack_root,
        log_dir=output_paths.log_dir,
        db_path=output_paths.db_path,
        uid=uid,
        gid=gid,
        web_exposure=web_exposure,
        web_bind=web_bind,
        web_port=web_port,
        public_origin=public_origin,
        allowed_hosts=allowed_hosts,
        trusted_proxies=trusted_proxies,
        enable_web_mutations=bool(getattr(args, "enable_web_mutations", False)),
        backup_existing=bool(getattr(args, "backup_existing", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        no_doctor=bool(getattr(args, "no_doctor", False)),
        no_color=bool(getattr(args, "no_color", False)),
        non_interactive=non_interactive,
    )


def _resolve_profile(
    args: argparse.Namespace,
    non_interactive: bool,
    prompter: InitPrompter,
) -> str:
    profile = str(getattr(args, "profile", "") or "")
    if not profile:
        if non_interactive:
            raise InitConfigError("--profile is required with --non-interactive")
        profile = prompter.choice("Deployment profile", PROFILES, "webui")
    _validate_choice("profile", profile, PROFILES)
    return profile


def _resolve_stack_root(
    args: argparse.Namespace,
    home: Path,
    profile: str,
    non_interactive: bool,
    prompter: InitPrompter,
) -> Path:
    raw_stack_root = str(getattr(args, "stack_root", "") or "")
    if not raw_stack_root:
        if non_interactive:
            raise InitConfigError("--stack-root is required with --non-interactive")
        raw_stack_root = prompter.text(
            "Compose stack root",
            _default_stack_root(home, profile),
        )
    return _absolute_path(raw_stack_root, "stack root")


def _resolve_output_paths(
    args: argparse.Namespace,
    home: Path,
    profile: str,
    non_interactive: bool,
    prompter: InitPrompter,
) -> _InitOutputPaths:
    config_file = _resolve_config_file(args, home, profile)
    compose_override = _resolve_compose_override(args, home, profile)
    if not non_interactive and not getattr(args, "config_file", None):
        config_file = Path(
            prompter.text("Config file", str(config_file))
        ).expanduser()
    if (
        not non_interactive
        and compose_override is not None
        and not getattr(args, "compose_override", None)
    ):
        if prompter.yes_no("Write a Compose override file", default=True):
            compose_override = Path(
                prompter.text("Compose override file", str(compose_override))
            ).expanduser()
        else:
            compose_override = None

    raw_log_dir = str(getattr(args, "log_dir", "") or "")
    if not raw_log_dir and not non_interactive:
        raw_log_dir = prompter.text(
            "Log/state directory",
            _default_log_dir(profile),
        )
    log_dir = Path(raw_log_dir or _default_log_dir(profile))

    raw_db_path = str(getattr(args, "db_path", "") or "")
    db_path = Path(raw_db_path or str(log_dir / DB_FILENAME))
    return _InitOutputPaths(
        config_file=config_file,
        compose_override=compose_override,
        log_dir=log_dir,
        db_path=db_path,
    )


def _resolve_init_uid_gid(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    profile: str,
    non_interactive: bool,
    prompter: InitPrompter,
) -> tuple[str, str]:
    uid, gid = _resolve_uid_gid_from_args_or_env(args, environ)
    if (
        not non_interactive
        and profile in {"webui", "helper", "hardened"}
        and not getattr(args, "uid", None)
        and not getattr(args, "gid", None)
    ):
        uid = prompter.text("Shared file UID", uid or DEFAULT_UID_GID)
        gid = prompter.text("Shared file GID", gid or uid or DEFAULT_UID_GID)
    return uid, gid


def run_init(
    answers: InitAnswers,
    *,
    repo_root: str | Path,
    environ: Mapping[str, str],
) -> InitResult:
    files = generate_files(answers)
    backups = _write_generated_files(files, answers)
    doctor_status = _run_doctor_if_requested(answers, repo_root=repo_root, environ=environ)
    return InitResult(
        answers=answers,
        generated_files=files,
        backups=tuple(backups),
        doctor_status=doctor_status,
    )


def generate_files(answers: InitAnswers) -> tuple[GeneratedFile, ...]:
    files = [
        GeneratedFile(
            path=answers.config_file,
            content=_env_content(answers),
            mode=0o600,
        )
    ]
    if answers.compose_override is not None:
        files.append(
            GeneratedFile(
                path=answers.compose_override,
                content=_compose_override_content(answers),
                mode=0o644,
            )
        )
    return tuple(files)


def _env_content(answers: InitAnswers) -> str:
    values = _container_env_values(answers)
    title = (
        f"{DISPLAY_NAME} {answers.profile} Compose environment "
        f"generated by {TECHNICAL_NAME} init."
    )
    return _dotenv_content(title, values)


def _container_env_values(answers: InitAnswers) -> list[tuple[str, str]]:
    values = [
        ("HOST_DOCKER_BASE", str(answers.stack_root)),
        ("WEBUI_LOG_DIR", str(answers.log_dir)),
        ("WUD_TIMEZONE", DEFAULT_TIMEZONE),
        ("OUT_UID", answers.uid or DEFAULT_UID_GID),
        ("OUT_GID", answers.gid or DEFAULT_UID_GID),
    ]
    if answers.profile in {"webui", "hardened"}:
        values.append(("WUD_API_BASE_URL", "http://wud:3000"))
        values.append(("WUD_API_STARTUP_WAIT_SECONDS", "5"))
        values.append(("WUD_PENDING_SOURCE", "api"))
        values.append(("WUDUP_LEGACY_SCRIPTS", "true"))
    if answers.profile == "webui":
        values.extend(
            (
                ("WEBUI_HTTP_BIND", answers.web_bind),
                ("WUD_WEB_PORT", answers.web_port),
                ("WUD_WEB_MUTATIONS_ENABLED", _bool_text(answers.enable_web_mutations)),
                ("WUD_WEB_PUBLIC_ORIGIN", answers.public_origin),
                ("WUD_WEB_ALLOWED_HOSTS", answers.allowed_hosts),
                ("WUD_WEB_TRUSTED_PROXIES", answers.trusted_proxies),
                ("WUD_WEB_SECURE_COOKIES", "auto"),
            )
        )
    return values


def _compose_override_content(answers: InitAnswers) -> str:
    yaml = YAML()
    yaml.default_flow_style = False
    data: dict[str, object] = {"services": {TECHNICAL_NAME: {}}}
    service = data["services"][TECHNICAL_NAME]  # type: ignore[index]
    if not isinstance(service, dict):
        raise InitConfigError("internal compose override error")

    service["environment"] = _compose_environment(answers)
    if answers.profile in {"webui", "hardened"}:
        _add_wud_health_dependency(service)
    service["volumes"] = _compose_volumes(answers)
    if answers.profile == "webui":
        service["ports"] = [
            "${WEBUI_HTTP_BIND:-127.0.0.1}:${WUD_WEB_PORT:-7417}:${WUD_WEB_PORT:-7417}"
        ]

    output = StringIO()
    output.write(f"# Generated by {TECHNICAL_NAME} init. Review before deploying.\n")
    yaml.dump(data, output)
    return output.getvalue()


def _add_wud_health_dependency(service: dict[str, object]) -> None:
    current_depends_on = service.get("depends_on")
    if current_depends_on is None:
        depends_on = {}
    elif isinstance(current_depends_on, Mapping):
        depends_on = dict(current_depends_on)
    elif isinstance(current_depends_on, Sequence) and not isinstance(
        current_depends_on, (str, bytes, bytearray)
    ):
        depends_on = {}
        for dependency in current_depends_on:
            if not isinstance(dependency, str):
                raise InitConfigError(
                    "compose service depends_on must be a mapping or a list of "
                    "service names"
                )
            depends_on[dependency] = {"condition": "service_started"}
    else:
        raise InitConfigError(
            "compose service depends_on must be a mapping or a list of service names"
        )
    depends_on["wud"] = {"condition": "service_healthy"}
    service["depends_on"] = depends_on


def _compose_environment(answers: InitAnswers) -> dict[str, str]:
    environment = {
        "DOCKER_BASE": "${HOST_DOCKER_BASE:-/srv/docker}",
        "WUD_OUT_FILE": "/out/images.todo",
        "OUT_UID": "${OUT_UID:-1000}",
        "OUT_GID": "${OUT_GID:-1000}",
    }
    if answers.profile == "hardened":
        environment["WUD_OUT_FILE"] = "${WUD_OUT_FILE:-/out/images.todo}"
    else:
        environment.update(
            {
                "WUD_LOG_DIR": "/logs",
                "WUD_DB_PATH": f"/logs/{DB_FILENAME}",
            }
        )
    if answers.profile == "webui":
        environment.update(
            {
                "WUD_WEB_PORT": "${WUD_WEB_PORT:-7417}",
                "WUD_WEB_MUTATIONS_ENABLED": "${WUD_WEB_MUTATIONS_ENABLED:-false}",
                "WUD_WEB_PUBLIC_ORIGIN": "${WUD_WEB_PUBLIC_ORIGIN:-}",
                "WUD_WEB_ALLOWED_HOSTS": "${WUD_WEB_ALLOWED_HOSTS:-}",
                "WUD_WEB_TRUSTED_PROXIES": "${WUD_WEB_TRUSTED_PROXIES:-}",
                "WUD_WEB_SECURE_COOKIES": "${WUD_WEB_SECURE_COOKIES:-auto}",
            }
        )
    if answers.profile in {"webui", "hardened"}:
        environment["WUD_API_BASE_URL"] = "${WUD_API_BASE_URL:-http://wud:3000}"
        environment["WUD_API_STARTUP_WAIT_SECONDS"] = (
            "${WUD_API_STARTUP_WAIT_SECONDS:-5}"
        )
        environment["WUD_PENDING_SOURCE"] = "${WUD_PENDING_SOURCE:-api}"
        environment["WUDUP_LEGACY_SCRIPTS"] = "${WUDUP_LEGACY_SCRIPTS:-true}"
    return environment


def _compose_volumes(answers: InitAnswers) -> list[str]:
    volumes = [
        "${HOST_DOCKER_BASE:-/srv/docker}:${HOST_DOCKER_BASE:-/srv/docker}",
        "${WEBUI_LOG_DIR:-./logs}:/logs",
        "wud-out:/out",
    ]
    return volumes


def _write_generated_files(
    files: Sequence[GeneratedFile],
    answers: InitAnswers,
) -> list[Path]:
    if answers.dry_run:
        return []
    _preflight_generated_files(files, answers)
    backups: list[Path] = []
    for file in files:
        file.path.parent.mkdir(parents=True, exist_ok=True)
        if file.path.exists():
            backup = _backup_path(file.path)
            file.path.replace(backup)
            backups.append(backup)
        file.path.write_text(file.content, encoding="utf-8")
        file.path.chmod(file.mode)
    return backups


def _preflight_generated_files(
    files: Sequence[GeneratedFile],
    answers: InitAnswers,
) -> None:
    seen: set[Path] = set()
    for file in files:
        if file.path in seen:
            raise InitConfigError(f"Generated file targets must be unique: {file.path}")
        seen.add(file.path)

        try:
            mode = file.path.lstat().st_mode
        except FileNotFoundError:
            continue

        if not stat.S_ISREG(mode):
            raise InitConfigError(f"Refusing to overwrite non-regular file: {file.path}")
        if not answers.backup_existing:
            raise InitConfigError(
                f"Refusing to overwrite existing file: {file.path}. "
                "Use --backup-existing to keep a timestamped backup."
            )


def _run_doctor_if_requested(
    answers: InitAnswers,
    *,
    repo_root: str | Path,
    environ: Mapping[str, str],
) -> int | None:
    if answers.no_doctor or answers.dry_run:
        return None
    if not answers.non_interactive:
        command = _container_doctor_command(answers)
        display = " ".join(shlex.quote(part) for part in command)
        prompter = InitPrompter()
        if prompter.yes_no(
            "Run container doctor now? This may create transient containers "
            "or volumes.",
            default=False,
        ):
            print(f"Running: {display}")
            result = subprocess.run(command, check=False)
            return result.returncode
    print(_container_doctor_guidance(answers))
    return None


def _container_doctor_guidance(answers: InitAnswers) -> str:
    command = _container_doctor_command(answers)
    display = " ".join(shlex.quote(part) for part in command)
    return "Container doctor was not run automatically. Run:\n  " + display


def _container_doctor_command(answers: InitAnswers) -> list[str]:
    base_file = {
        "webui": "docs/examples/docker-compose.webui.yml",
        "helper": "docs/examples/docker-compose.example.yml",
        "hardened": "docs/examples/docker-compose.hardened.yml",
    }[answers.profile]
    command = [
        "docker",
        "compose",
        "--env-file",
        str(answers.config_file),
        "-f",
        base_file,
    ]
    if answers.compose_override is not None:
        command.extend(("-f", str(answers.compose_override)))
    command.extend(("run", "--rm", TECHNICAL_NAME, "doctor"))
    return command


def _print_result(result: InitResult) -> None:
    action = "Would write" if result.answers.dry_run else "Wrote"
    for file in result.generated_files:
        print(f"{action} {file.path}")
    for backup in result.backups:
        print(f"Backed up existing file to {backup}")
    if result.answers.dry_run:
        print("Dry-run mode: no files were changed.")
    if result.doctor_status is not None:
        print(f"Doctor exit status: {result.doctor_status}")


def _resolve_config_file(
    args: argparse.Namespace,
    home: Path,
    profile: str,
) -> Path:
    value = str(getattr(args, "config_file", "") or "")
    if value:
        return Path(value).expanduser()
    name = f"{profile}.env"
    return home / ".config" / CONFIG_DIR_NAME / name


def _resolve_compose_override(
    args: argparse.Namespace,
    home: Path,
    profile: str,
) -> Path | None:
    if bool(getattr(args, "no_compose_override", False)):
        return None
    value = str(getattr(args, "compose_override", "") or "")
    if value:
        return Path(value).expanduser()
    if profile not in {"helper", "hardened"}:
        return None
    return home / ".config" / CONFIG_DIR_NAME / f"docker-compose.{profile}.override.yml"


def _resolve_uid_gid_from_args_or_env(
    args: argparse.Namespace,
    environ: Mapping[str, str],
) -> tuple[str, str]:
    uid = str(getattr(args, "uid", "") or "")
    gid = str(getattr(args, "gid", "") or "")
    if uid or gid:
        return uid, gid
    return environ.get("OUT_UID", ""), environ.get("OUT_GID") or environ.get("OUT_GUID", "")


def _resolve_web_exposure(
    args: argparse.Namespace,
    profile: str,
    non_interactive: bool,
    prompter: InitPrompter,
) -> str:
    value = str(getattr(args, "web_exposure", "") or "")
    if value:
        _validate_choice("web exposure", value, WEB_EXPOSURES)
        return value
    if profile != "webui":
        return "loopback"
    if non_interactive:
        return "loopback"
    return prompter.choice("WebUI exposure", WEB_EXPOSURES, "loopback")


def _resolve_web_bind(args: argparse.Namespace, web_exposure: str) -> str:
    value = str(getattr(args, "web_bind", "") or "")
    if value:
        return value
    if web_exposure == "lan":
        return "0.0.0.0"
    return "127.0.0.1"


def _resolve_web_values(
    *,
    web_exposure: str,
    web_bind: str,
    public_origin: str,
    allowed_hosts: str,
    trusted_proxies: str,
    non_interactive: bool,
    prompter: InitPrompter,
) -> tuple[str, str, str, str]:
    if web_exposure == "loopback":
        return web_bind, public_origin, allowed_hosts, trusted_proxies

    public_origin = public_origin.strip()
    while not public_origin:
        if non_interactive:
            raise InitConfigError(
                f"--public-origin is required for --web-exposure {web_exposure}"
            )
        public_origin = prompter.text("Browser-visible WebUI origin").strip()
        if not public_origin:
            print("Browser-visible WebUI origin is required.", file=prompter.stream)
    _validate_public_origin(public_origin)

    allowed_hosts = allowed_hosts.strip()
    if web_exposure == "reverse-proxy" and not trusted_proxies and not non_interactive:
        trusted_proxies = prompter.text(
            "Trusted proxy IP/CIDR/hostname list",
            "127.0.0.1/32",
        )
    return web_bind, public_origin, allowed_hosts, trusted_proxies


def _validate_public_origin(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise InitConfigError("WUD_WEB_PUBLIC_ORIGIN must be an http(s) origin")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise InitConfigError("WUD_WEB_PUBLIC_ORIGIN must not include a path or query")


def _absolute_path(value: str, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InitConfigError(f"{label} must be an absolute path")
    return path


def _validate_choice(name: str, value: str, choices: Sequence[str]) -> None:
    if value not in choices:
        raise InitConfigError(f"{name} must be one of: {', '.join(choices)}")


def _validate_port(value: str) -> None:
    if not value.isdigit() or not (1 <= int(value) <= 65535):
        raise InitConfigError("--web-port must be an integer from 1 to 65535")


def _validate_uid_gid(uid: str, gid: str) -> None:
    if bool(uid) != bool(gid):
        raise InitConfigError("--uid and --gid must be set together")
    for label, value in (("--uid", uid), ("--gid", gid)):
        if value and (not value.isdigit() or int(value) < 0):
            raise InitConfigError(f"{label} must be a numeric id")


def _default_stack_root(home: Path, profile: str) -> str:
    return "/srv/docker"


def _default_log_dir(profile: str) -> str:
    return "./logs"


def _dotenv_content(title: str, values: Sequence[tuple[str, str]]) -> str:
    lines = [f"# {title}"]
    lines.extend(f"{key}={_dotenv_quote(value)}" for key, value in values)
    return "\n".join(lines) + "\n"


def _dotenv_quote(value: str) -> str:
    if value == "":
        return ""
    if any(char.isspace() for char in value) or "#" in value:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{index}")
        index += 1
    return candidate
