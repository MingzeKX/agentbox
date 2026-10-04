"""Permission tiers and the host.exec escape hatch.

Checked here:
  * the tier -> tool mapping and the refusal message
  * search hides what the ceiling forbids, dispatch refuses it before any RPC
  * host.exec itself: tier + phrase + workdir confinement + timeout + env scrubbing
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent.ai import tiers
from agent.config import Settings
from agent.control.host import exec as host_exec

# --------------------------------------------------------------------------- tiers


def test_tool_to_tier_mapping():
    assert tiers.required_tier("exec.run") == "safe"
    assert tiers.required_tier("fs.read") == "safe"
    assert tiers.required_tier("toolsmith.create") == "safe"
    assert tiers.required_tier("net.fetch") == "trusted"
    assert tiers.required_tier("mcp.server.tool") == "trusted"
    assert tiers.required_tier("host.exec") == "unrestricted"


@pytest.mark.parametrize(
    ("tier", "tool", "ok"),
    [
        ("safe", "exec.run", True),
        ("safe", "net.fetch", False),
        ("safe", "host.exec", False),
        ("trusted", "net.fetch", True),
        ("trusted", "mcp.a.b", True),
        ("trusted", "host.exec", False),
        ("unrestricted", "host.exec", True),
        ("unrestricted", "net.fetch", True),
    ],
)
def test_allowed_matrix(tier, tool, ok):
    assert tiers.allowed(tool, tier) is ok


def test_denial_explains_how_to_raise_the_tier(monkeypatch):
    monkeypatch.setattr(tiers.settings, "permission_tier", "safe")
    with pytest.raises(tiers.TierDenied) as excinfo:
        tiers.check("host.exec")
    message = str(excinfo.value)
    assert "/perm unrestricted" in message
    assert "AGENT_HOST_EXEC_PHRASE" in message

    with pytest.raises(tiers.TierDenied) as excinfo:
        tiers.check("net.fetch")
    assert "/perm trusted" in str(excinfo.value)


def test_describe_reports_what_is_available(monkeypatch):
    monkeypatch.setattr(tiers.settings, "permission_tier", "trusted")
    described = tiers.describe()
    assert described["tier"] == "trusted"
    assert described["tools"]["net.*"]["available"] is True
    assert described["tools"]["host.exec"]["available"] is False


# ------------------------------------------------------------------ dispatch gate


@pytest.mark.asyncio
async def test_dispatch_refuses_above_the_tier_before_any_rpc(monkeypatch, fake_gateway, fake_sessionmaker):
    from agent.ai.metacalls import MetaTools
    from agent.models.tool import ToolRecord

    monkeypatch.setattr(tiers.settings, "permission_tier", "safe")
    record = ToolRecord(
        id="11111111-1111-1111-1111-111111111111",
        name="host.exec",
        version=1,
        status="active",
        tier="core",
        executor="control_native",
        description="d",
        when_to_use="w",
        tags=["host"],
        params_schema={
            "type": "object",
            "properties": {"argv": {"type": "array", "items": {"type": "string"}}, "confirm": {"type": "string"}},
            "required": ["argv", "confirm"],
            "additionalProperties": False,
        },
        permissions=[],
        timeout_s=30.0,
        entrypoint="run",
        examples=[],
        source="",
        source_sha256="a" * 64,
        embedding_model="",
        created_at="2026-01-01T00:00:00",
    )

    async def resolve(session, name, version=None, statuses=("active",)):  # noqa: ANN001, ARG001
        return record

    async def note(session, **kwargs):  # noqa: ANN001, ARG002
        return "active"

    from agent.registry import service as registry_service

    monkeypatch.setattr(registry_service, "resolve_tool", resolve)
    monkeypatch.setattr(registry_service, "note_call", note)

    meta = MetaTools(session_id="s1", gateway=fake_gateway, sessionmaker=fake_sessionmaker)
    outcome = await meta.invoke("host.exec", {"argv": ["echo", "hi"], "confirm": "x"})
    assert outcome.ok is False
    assert outcome.error_code == "tier_denied"
    assert fake_gateway.calls == [], "a forbidden tool must never reach the host"


# --------------------------------------------------------------------- host.exec


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "permission_tier": "unrestricted",
        "host_workdir": str(tmp_path),
        "host_exec_timeout_s": 10.0,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_host_exec_requires_unrestricted_tier(tmp_path: Path):
    settings = make_settings(tmp_path, permission_tier="trusted")
    with pytest.raises(host_exec.HostExecError, match="disabled"):
        await host_exec.run(settings, argv=["echo", "hi"], confirm=settings.host_exec_phrase)


@pytest.mark.asyncio
async def test_host_exec_requires_the_confirmation_phrase(tmp_path: Path):
    settings = make_settings(tmp_path)
    with pytest.raises(host_exec.HostExecError, match="confirmation phrase"):
        await host_exec.run(settings, argv=["echo", "hi"], confirm="wrong")
    with pytest.raises(host_exec.HostExecError, match="confirmation phrase"):
        await host_exec.run(settings, argv=["echo", "hi"])


@pytest.mark.asyncio
async def test_host_exec_refuses_to_leave_the_workdir(tmp_path: Path):
    settings = make_settings(tmp_path)
    outside = tmp_path.parent
    with pytest.raises(host_exec.HostExecError, match="must stay inside"):
        await host_exec.run(settings, argv=["echo", "hi"], cwd=str(outside), confirm=settings.host_exec_phrase)


@pytest.mark.asyncio
async def test_host_exec_refuses_a_missing_cwd(tmp_path: Path):
    settings = make_settings(tmp_path)
    with pytest.raises(host_exec.HostExecError, match="does not exist"):
        await host_exec.run(settings, argv=["echo", "hi"], cwd="nope", confirm=settings.host_exec_phrase)


@pytest.mark.asyncio
async def test_host_exec_runs_a_real_command(tmp_path: Path):
    settings = make_settings(tmp_path)
    result = await host_exec.run(
        settings,
        argv=[sys.executable, "-c", "print('from the host')"],
        confirm=settings.host_exec_phrase,
    )
    assert result["exit_code"] == 0
    assert "from the host" in result["stdout"]
    assert result["cwd"] == str(tmp_path.resolve())  # noqa: ASYNC240 - test-only path check
    assert result["timed_out"] is False


@pytest.mark.asyncio
async def test_host_exec_enforces_the_timeout(tmp_path: Path):
    settings = make_settings(tmp_path, host_exec_timeout_s=1.0)
    result = await host_exec.run(
        settings,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        confirm=settings.host_exec_phrase,
        timeout_s=1.0,
    )
    assert result["timed_out"] is True
    assert result["exit_code"] is None


@pytest.mark.asyncio
async def test_host_exec_scrubs_secret_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_TEST_SECRET_TOKEN", "super-secret")
    monkeypatch.setenv("MY_API_KEY", "also-secret")
    monkeypatch.setenv("HARMLESS_SETTING", "visible")
    settings = make_settings(tmp_path)
    result = await host_exec.run(
        settings,
        argv=[sys.executable, "-c", "import os; print(os.environ.get('MY_API_KEY'), os.environ.get('HARMLESS_SETTING'))"],
        confirm=settings.host_exec_phrase,
    )
    assert "also-secret" not in result["stdout"]
    assert "visible" in result["stdout"]
    assert "None" in result["stdout"].split()[0]


@pytest.mark.asyncio
async def test_host_exec_caps_output(tmp_path: Path):
    settings = make_settings(tmp_path)
    result = await host_exec.run(
        settings,
        argv=[sys.executable, "-c", "print('x' * 5000)"],
        confirm=settings.host_exec_phrase,
        max_output_bytes=100,
    )
    assert result["truncated"] is True
    assert len(result["stdout"]) == 100


def test_workdir_defaults_to_the_repo_root():
    from agent.config import project_root

    settings = Settings()
    assert host_exec.workdir(settings) == project_root().resolve()
