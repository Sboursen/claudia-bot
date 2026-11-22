"""Local effort preference without querying or starting an engine."""
from __future__ import annotations

from slop import relay

import test_relay as RT  # noqa: E402  (its fake engine and helpers)
K = RT.K


def test_the_effort_slider(tmp_path, monkeypatch):
    RT.setup(tmp_path, monkeypatch)
    monkeypatch.delenv("SLOP_CLAUDE_EFFORT", raising=False)
    monkeypatch.delenv("SLOP_CODEX_EFFORT", raising=False)
    assert K.run("relay", "setup")[0] == 0
    rc, out = K.run("relay", "effort")
    assert rc == 0 and out["engine"] == "claude" and out["effort"] == "xhigh" and out["default"] == "xhigh", out
    assert out["levels"] == ["low", "medium", "high", "xhigh", "max"] and out["engines"]["codex"]["effort"] == "xhigh"
    rc, out = K.run("relay", "effort", "max")
    assert rc == 0 and out["changed"] == "max" and out["effort"] == "max" and "saved local effort preference" in out["text"]
    rc, out = K.run("relay", "effort", "high", "--engine", "codex")
    assert rc == 0 and out["engine"] == "codex" and out["engines"] == {
        "claude": {"effort": "max", "source": "slop relay effort"},
        "codex": {"effort": "high", "source": "slop relay effort"}}
    rc, out = K.run("relay", "effort", "very hard")
    assert rc == 2 or (rc == 1 and "one word" in out["error"]), out
    rc, out = K.run("relay", "effort", "auto")
    assert rc == 0 and out["effort"] == "auto"
    rc, out = K.run("relay", "effort", "default")
    assert rc == 0 and out["effort"] == "xhigh"
    monkeypatch.setenv("SLOP_CLAUDE_EFFORT", "low")
    rc, out = K.run("relay", "effort")
    assert out["effort"] == "low" and "SLOP_CLAUDE_EFFORT" in out["text"]
    assert "effort: low" in K.run("relay", "status")[1]["text"]


def test_local_levels_do_not_probe_the_engine(monkeypatch):
    from slop.agents import engines
    def unexpected(*args, **kwargs):
        raise AssertionError("effort levels reached an engine")
    monkeypatch.setattr(engines, "run_cli", unexpected)
    assert relay.effort_levels("claude")[0] == ["low", "medium", "high", "xhigh", "max"]
