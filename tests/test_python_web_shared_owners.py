"""Contracts for shared redaction, request context, and transactions."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from wudup import web_auth as auth
from wudup import web_database as database
from wudup import web_redaction as redaction


@pytest.mark.parametrize("outcome", ["success", "body", "begin", "commit"])
def test_immediate_transaction_preserves_order_and_failure_identity(outcome):
    conn = MagicMock()
    failure = RuntimeError("example transaction failure")
    if outcome == "begin":
        conn.execute.side_effect = failure
    elif outcome == "commit":
        conn.commit.side_effect = failure

    def transact():
        with database.immediate_transaction(conn):
            conn.execute("example write")
            if outcome == "body":
                raise failure

    if outcome == "success":
        transact()
    else:
        with pytest.raises(RuntimeError) as caught:
            transact()
        assert caught.value is failure
    expected = ["execute"]
    if outcome != "begin":
        expected.extend(["execute", "rollback" if outcome == "body" else "commit"])
    assert [call[0] for call in conn.mock_calls] == expected
    assert conn.execute.call_args_list[0].args == ("BEGIN IMMEDIATE",)


def test_recursive_redaction_preserves_input_and_exact_replacements(tmp_path: Path):
    settings = SimpleNamespace(
        auth_token="example-token-value",
        wud_api_client=SimpleNamespace(secret_values=("example-password",)),
        command_env={"DISCORD_WEBHOOK": "https://discord.test/secret-webhook-fragment"},
        config=SimpleNamespace(
            docker_base=tmp_path,
            wud_out_file=tmp_path / "images.todo",
            log_dir=tmp_path / "logs",
            db_path=tmp_path / "state.db",
        ),
        host_docker_base=None,
    )
    value = {
        "details": [
            f"example-token-value {tmp_path / 'images.todo'}",
            "example-password secret-webhook-fragment /unrelated/private/path",
            {"count": 2, "enabled": False, "missing": None},
        ],
    }
    original = deepcopy(value)

    result = redaction.sanitize_support_bundle_value(settings, value)

    assert result == {"details": [
        "<redacted> <WUD_OUT_FILE>",
        "<redacted> <redacted> [REDACTED_PATH]",
        {"count": 2, "enabled": False, "missing": None},
    ]}
    assert value == original
    assert result is not value
    assert redaction.safe_exception_detail(
        settings, "could not read example", RuntimeError("example-password /private/example"),
    ) == "could not read example: <redacted> [REDACTED_PATH]"


@pytest.mark.parametrize(("dev", "authorization", "cookie", "actor"), [
    (True, "Bearer wrong", "", "dev"),
    (False, "Bearer example-token-value", "session-value", "bearer"),
    (False, "Bearer wrong", "session-value", "session"),
    (False, "Bearer wrong", "", "unknown"),
])
def test_audit_actor_classification_keeps_auth_policy_precedence(dev, authorization, cookie, actor):
    settings = SimpleNamespace(dev_no_auth=dev, auth_token="example-token-value")
    request = SimpleNamespace(
        headers={"authorization": authorization},
        cookies={auth.SESSION_COOKIE: cookie},
    )
    assert auth.request_actor_type(settings, request) == actor
