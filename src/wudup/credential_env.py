"""Secret environment variables that can also be read from a file.

Framework-free so the CLI doctor can use it without loading the WebUI stack.
"""

from __future__ import annotations

WUD_API_AUTH_BEARER_TOKEN_ENV = "WUD_API_AUTH_BEARER_TOKEN"
WUD_API_AUTH_BEARER_TOKEN_FILE_ENV = "WUD_API_AUTH_BEARER_TOKEN_FILE"
WUD_API_AUTH_BASIC_PASSWORD_ENV = "WUD_API_AUTH_BASIC_PASSWORD"
WUD_API_AUTH_BASIC_PASSWORD_FILE_ENV = "WUD_API_AUTH_BASIC_PASSWORD_FILE"

# Each direct secret paired with the variable that names a file holding it.
SECRET_FILE_ENV_PAIRS: tuple[tuple[str, str], ...] = (
    (WUD_API_AUTH_BEARER_TOKEN_ENV, WUD_API_AUTH_BEARER_TOKEN_FILE_ENV),
    (WUD_API_AUTH_BASIC_PASSWORD_ENV, WUD_API_AUTH_BASIC_PASSWORD_FILE_ENV),
)
