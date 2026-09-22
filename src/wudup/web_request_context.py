"""Request settings and existing trusted-proxy context interpretation.

Authentication, authorization, actor credential checks, CSRF and cookie policy
remain in web_auth. This module reads the already configured request context.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import Request

from .web_models import WebSettings


def request_settings(request: Request) -> WebSettings:
    return request.app.state.web_settings


def effective_origin(request: Request, settings: WebSettings) -> str:
    if settings.public_origin:
        return settings.public_origin
    forwarded = trusted_forwarded_origin(request, settings)
    if forwarded:
        return forwarded
    host = request.headers.get("host", "")
    return normalize_origin(f"{request.url.scheme}://{host}")


def trusted_forwarded_origin(request: Request, settings: WebSettings) -> str:
    if not client_is_trusted_proxy(request, settings):
        return ""
    forwarded_origin = origin_from_forwarded_header(
        request.headers.get("forwarded", "")
    )
    if forwarded_origin:
        return forwarded_origin
    proto = last_forwarded_header_value(
        request.headers.get("x-forwarded-proto", "")
    )
    host = last_forwarded_header_value(
        request.headers.get("x-forwarded-host", "")
    )
    if proto and host:
        return normalize_origin(f"{proto}://{host}")
    return ""


def request_client_address(request: Request, settings: WebSettings) -> str:
    forwarded = trusted_forwarded_client_address(request, settings)
    if forwarded:
        return forwarded
    if request.client is None:
        return ""
    return request.client.host


def trusted_forwarded_client_address(
    request: Request,
    settings: WebSettings,
) -> str:
    if not client_is_trusted_proxy(request, settings):
        return ""
    forwarded = client_address_from_forwarded_header(
        request.headers.get("forwarded", "")
    )
    if forwarded:
        return forwarded
    forwarded_for = last_forwarded_header_value(
        request.headers.get("x-forwarded-for", "")
    )
    return normalize_forwarded_client_address(forwarded_for)


def last_forwarded_header_value(value: str) -> str:
    for item in reversed(value.split(",")):
        stripped = item.strip()
        if stripped:
            return stripped
    return ""


def client_address_from_forwarded_header(value: str) -> str:
    if not value:
        return ""
    hop = last_forwarded_header_value(value)
    for segment in hop.split(";"):
        key, separator, raw = segment.strip().partition("=")
        if separator and key.lower() == "for":
            return normalize_forwarded_client_address(raw)
    return ""


def normalize_forwarded_client_address(value: str) -> str:
    raw = value.strip().strip('"')
    if not raw or raw.lower() == "unknown":
        return ""
    if raw.startswith("["):
        host, separator, _port = raw[1:].partition("]")
        return host if separator else raw
    host, separator, port = raw.rpartition(":")
    if separator and port.isdigit():
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return raw
        return host
    return raw


def origin_from_forwarded_header(value: str) -> str:
    if not value:
        return ""
    hop = last_forwarded_header_value(value)
    parts: dict[str, str] = {}
    for segment in hop.split(";"):
        key, separator, raw = segment.strip().partition("=")
        if separator:
            parts[key.lower()] = raw.strip().strip('"')
    proto = parts.get("proto", "")
    host = parts.get("host", "")
    if proto and host:
        return normalize_origin(f"{proto}://{host}")
    return ""


def client_is_trusted_proxy(request: Request, settings: WebSettings) -> bool:
    if request.client is None:
        return False
    try:
        address = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    return any(address in network for network in settings.trusted_proxies)


def raw_client_is_loopback(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def normalize_origin(value: str) -> str:
    raw = value.strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        return ""
    host = parsed.hostname
    if not host:
        return ""
    netloc = parsed.netloc.lower()
    return f"{parsed.scheme.lower()}://{netloc}"


def host_from_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    return normalize_host(parsed.hostname or "")


def normalize_host(value: str) -> str:
    raw = value.strip().lower().rstrip(".")
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        return raw[1:end] if end != -1 else ""
    if raw.count(":") == 1:
        host, _, port = raw.partition(":")
        if port.isdigit():
            return host
    return raw
