"""Command line interface for WUDup Python entrypoints."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .banner import print_startup_banner
from .doctor import run_doctor_from_namespace


class WudArgumentParser(argparse.ArgumentParser):
    def format_help(self) -> str:
        return _capitalized_usage(super().format_help())

    def format_usage(self) -> str:
        return _capitalized_usage(super().format_usage())


def _capitalized_usage(value: str) -> str:
    if value.startswith("usage:"):
        return "Usage:" + value[len("usage:") :]
    return value


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base", metavar="PATH")
    parser.add_argument("--file", metavar="PATH")
    parser.add_argument("--mode", choices=("pause", "stop", "live"))
    parser.add_argument("--max-wait", metavar="SECONDS")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-y", "--yes", "--no-confirm", action="store_true")
    parser.add_argument("--allow-tag-updates", action="store_true")


def _add_update_from_wud_options(parser: argparse.ArgumentParser) -> None:
    _add_common_options(parser)
    parser.add_argument("--log-dir", metavar="PATH")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--only-lines", metavar="SPEC")
    parser.add_argument("--remove-lines-before-run", metavar="SPEC")
    parser.add_argument("--tag-override", metavar="LINE=TAG", action="append")
    parser.add_argument("--exclude-tag-lines", metavar="SPEC")
    parser.add_argument("--recreate-excluded-services", action="store_true")


def _add_doctor_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base", metavar="PATH")
    parser.add_argument("--file", metavar="PATH")
    parser.add_argument("--log-dir", metavar="PATH")
    parser.add_argument("--no-color", action="store_true")


def _add_web_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("web_command", nargs="?", choices=("reset-admin",))
    parser.add_argument("--base", metavar="PATH")
    parser.add_argument("--file", metavar="PATH")
    parser.add_argument("--log-dir", metavar="PATH")
    parser.add_argument("--db-path", metavar="PATH")
    parser.add_argument("--host", metavar="HOST")
    parser.add_argument("--port", metavar="PORT")
    parser.add_argument("--static-dir", metavar="PATH")
    parser.add_argument("--user", metavar="USERNAME")


def _add_init_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", choices=("webui", "helper", "hardened"))
    parser.add_argument("--config-file", metavar="PATH")
    parser.add_argument("--compose-override", metavar="PATH")
    parser.add_argument("--no-compose-override", action="store_true")
    parser.add_argument("--stack-root", metavar="PATH")
    parser.add_argument("--log-dir", metavar="PATH")
    parser.add_argument("--db-path", metavar="PATH")
    parser.add_argument("--uid", metavar="ID")
    parser.add_argument("--gid", metavar="ID")
    parser.add_argument(
        "--web-exposure",
        choices=("loopback", "lan", "reverse-proxy"),
    )
    parser.add_argument("--web-bind", metavar="HOST")
    parser.add_argument("--web-port", metavar="PORT")
    parser.add_argument("--public-origin", metavar="ORIGIN")
    parser.add_argument("--allowed-hosts", metavar="HOSTS")
    parser.add_argument("--trusted-proxies", metavar="CIDRS")
    parser.add_argument("--enable-web-mutations", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--backup-existing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-doctor", action="store_true")
    parser.add_argument("--no-color", action="store_true")


def _run_update_from_wud(args: argparse.Namespace) -> int:
    from .updater import run_update_from_wud
    from .updater_cli import options_from_namespace
    from .updater_models import UpdaterError

    print_startup_banner(no_color=bool(getattr(args, "no_color", False)))
    try:
        options = options_from_namespace(args)
    except UpdaterError as exc:
        print(exc, file=sys.stderr)
        return 1
    return run_update_from_wud(options)


def _run_doctor(args: argparse.Namespace) -> int:
    return run_doctor_from_namespace(
        args,
        repo_root=Path(__file__).resolve().parents[2],
    )


def _run_web(args: argparse.Namespace) -> int:
    from .web import run_web_from_namespace

    return run_web_from_namespace(args)


def _run_init(args: argparse.Namespace) -> int:
    from .init_config import run_init_from_namespace

    return run_init_from_namespace(args)


def build_parser() -> argparse.ArgumentParser:
    parser = WudArgumentParser(
        prog="wudup",
        description="WUDup command line tools.",
    )
    subcommands = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=WudArgumentParser,
    )

    update_from_wud = subcommands.add_parser(
        "update-from-wud",
        help="run the Python docker-update-from-wud implementation",
    )
    _add_update_from_wud_options(update_from_wud)
    update_from_wud.set_defaults(handler=_run_update_from_wud)

    doctor = subcommands.add_parser(
        "doctor",
        help="check WUDup container setup and Docker access",
    )
    _add_doctor_options(doctor)
    doctor.set_defaults(handler=_run_doctor)

    web = subcommands.add_parser(
        "web",
        help="run the read-only WebUI API server",
    )
    _add_web_options(web)
    web.set_defaults(handler=_run_web)

    init = subcommands.add_parser(
        "init",
        help="generate first-run configuration files",
    )
    _add_init_options(init)
    init.set_defaults(handler=_run_init)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
