from __future__ import annotations

from unittest import mock

from wudup import web_demo_fixtures


def test_static_demo_fixture_generation_matches_read_only_contract() -> None:
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=AssertionError("static demo fixture generation must not use network"),
    ):
        data = web_demo_fixtures.generate_static_demo_fixtures()

    assert data["auth"]["session"]["dev_auth_bypass"] is False
    assert data["auth"]["session"]["mutations_enabled"] is False
    assert data["planCases"] == []
    assert "removalCases" not in data
    assert data["retagCases"] == []
    assert any(
        item["service_key"] == "media/wudup"
        and item["retag_available"] is True
        and item["runtime_state"] == "not-running"
        for item in data["retagTargets"]["items"]
    )
    assert data["runs"]["rollbackPlans"]
    assert set(data["runs"]["rollbackPlans"]) == {
        str(run["id"]) for run in data["runs"]["summaries"]
    }
    assert data["pending"]["count"] == 8
    runner = next(
        item
        for item in data["pending"]["items"]
        if item["image"] == web_demo_fixtures.DEMO_RUNNER_CURRENT_IMAGE
    )
    assert runner["tag_stream"] == {
        "current_stream": "distroless",
        "reported_stream": "default",
    }
    assert data["pending"]["snoozed_candidates"] == [
        web_demo_fixtures.DEMO_PENDING_SNOOZED_CANDIDATE
    ]
    security_assessments = {
        item["image_repo"]: item["security"]
        for item in data["releaseNotes"]["items"]
    }
    assert security_assessments["home-assistant/core"]["outcome"] == (
        "verified_critical_high"
    )
    assert security_assessments["home-assistant/core"]["severity"] == "critical"
    assert security_assessments["magrhino/wudup"]["outcome"] == "needs_review"
    assert security_assessments["linuxserver/docker-radarr"]["outcome"] == "ordinary"

    doctor_checks = {check["code"]: check for check in data["doctor"]["checks"]}
    assert doctor_checks["webui-authentication"]["status"] == "PASS"
    assert doctor_checks["webui-authentication"]["detail"] == (
        "development auth bypass is disabled"
    )
    assert doctor_checks["webui-mutation-gate"]["status"] == "PASS"
    assert doctor_checks["webui-mutation-gate"]["detail"] == (
        "browser mutations are disabled"
    )

    diagnostics_checks = {
        check["code"]: check
        for check in data["diagnostics"]["doctor_result"]["checks"]
    }
    assert diagnostics_checks["webui-mutation-gate"]["detail"] == (
        "browser mutations are disabled"
    )

    apply_checks = {
        check["code"]: check
        for check in data["selfUpdatePlan"]["plan"]["apply_preflight"]["checks"]
    }
    assert apply_checks["mutations-enabled"]["status"] == "FAIL"
    assert apply_checks["mutations-enabled"]["detail"] == (
        web_demo_fixtures.STATIC_DEMO_READ_ONLY_MESSAGE
    )

    assert (
        web_demo_fixtures.GENERATED_FIXTURE_PATH.read_text(encoding="utf-8")
        == web_demo_fixtures.render_static_demo_fixtures_ts(data)
    )
