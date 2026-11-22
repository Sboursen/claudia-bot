"""The legacy wake configuration is local and non-disclosing."""

import json

import test_relay as RT


def test_wake_status_and_off_never_use_a_legacy_destination(tmp_path, monkeypatch):
    env = RT.setup(tmp_path, monkeypatch)
    assert RT.K.run("relay", "setup")[0] == 0
    legacy_url = "https://legacy.example.invalid/routine"
    state = env["rd"] / "wake.json"
    state.write_text(json.dumps({"url": legacy_url}))
    for command in (["test"], [legacy_url]):
        rc, error = RT.K.run("relay", "wake", *command)
        assert rc == 1 and "authenticated broker" in error["error"]
        assert legacy_url not in error["error"]
    rc, status = RT.K.run("relay", "wake")
    assert rc == 0 and not status["on"] and status["legacy_configured"]
    assert legacy_url not in json.dumps(status)
    assert RT.K.run("relay", "wake", "off")[1]["legacy_configured"] is False
    assert "url" not in json.loads(state.read_text())
