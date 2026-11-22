"""Local relay setup preserves vendor configuration and strips toolkit credentials from agent helpers."""

from slop.agents import engines

import test_relay as RT


def test_local_setup_does_not_register_mcp_or_modify_engine_config(tmp_path, monkeypatch):
    env = RT.setup(tmp_path, monkeypatch, logged=("claude", "codex"))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "synthetic-key")
    codex_config = env["home"] / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True, exist_ok=True)
    previous_config = 'model = "local-model"\n'
    codex_config.write_text(previous_config)
    assert RT.K.run("relay", "setup", "--engine", "codex")[0] == 0
    assert codex_config.read_text() == previous_config
    assert not RT.K.calls(env["state"])
    assert not (env["wd"] / ".agents").exists()
    assert not (env["wd"] / ".claude").exists()
    assert "synthetic-key" not in (env["rd"] / "config.json").read_text()


def test_toolkit_keys_are_stripped_from_the_agent_environment(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "synthetic-key")
    assert "ELEVENLABS_API_KEY" in engines.TOOLKIT_SECRETS
    assert "ELEVENLABS_API_KEY" not in engines.base_env("claude")
