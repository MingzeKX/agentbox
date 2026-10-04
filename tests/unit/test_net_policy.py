"""The network switch and its firewall.

The sandbox VM never gets a NIC: `net.fetch` / `net.http` run inside the AI service,
which is the same process that already talks to the LLM API.  Everything here is
tested with httpx.MockTransport, so no sockets are involved (the AI service is not
allowed to import `socket` at all).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agent.ai import net
from agent.config import Settings


@pytest.fixture
def net_settings(tmp_path: Path, monkeypatch):
    """A settings object with the switch on and an allowlist, isolated to tmp."""

    def build(**overrides):
        values = {
            "net_enabled": True,
            "net_allow_hosts": "example.com,*.trusted.test",
            "net_max_bytes": 1000,
            "net_timeout_s": 5.0,
            "net_max_redirects": 2,
        }
        values.update(overrides)
        settings = Settings(**values)
        monkeypatch.setattr(net, "settings", settings)
        return settings

    return build


# --------------------------------------------------------------------------- #
# allowlist parsing / matching
# --------------------------------------------------------------------------- #


def test_allowlist_parsing_accepts_urls_and_commas():
    assert net.parse_allowlist("api.example.com, https://Other.Example.com/path ; *.foo.test") == [
        "api.example.com",
        "other.example.com",
        "*.foo.test",
    ]


def test_exact_host_matching():
    allow = ["example.com"]
    assert net.host_allowed("example.com", allow)
    assert net.host_allowed("EXAMPLE.com", allow)
    assert not net.host_allowed("evil.com", allow)
    assert not net.host_allowed("example.com.evil.com", allow)


def test_wildcard_matches_subdomains_only():
    allow = ["*.trusted.test"]
    assert net.host_allowed("a.trusted.test", allow)
    assert net.host_allowed("a.b.trusted.test", allow)
    assert net.host_allowed("trusted.test", allow)
    # the classic suffix trick must not pass
    assert not net.host_allowed("nottrusted.test", allow)
    assert not net.host_allowed("trusted.test.evil.com", allow)


def test_star_allows_everything_but_must_be_explicit():
    assert net.host_allowed("anything.example", ["*"])
    assert not net.host_allowed("anything.example", [])


# --------------------------------------------------------------------------- #
# the switch
# --------------------------------------------------------------------------- #


def test_disabled_switch_refuses_before_any_network_access(net_settings):
    net_settings(net_enabled=False)
    with pytest.raises(net.NetPolicyError) as excinfo:
        net.check_url("https://example.com/x")
    assert excinfo.value.code == "net_disabled"


def test_enabled_but_empty_allowlist_denies(net_settings):
    net_settings(net_allow_hosts="")
    with pytest.raises(net.NetPolicyError) as excinfo:
        net.check_url("https://example.com/x")
    assert "AGENT_NET_ALLOW_HOSTS" in str(excinfo.value)


def test_non_allowlisted_host_is_denied(net_settings):
    net_settings()
    with pytest.raises(net.NetPolicyError) as excinfo:
        net.check_url("https://evil.example/x")
    assert excinfo.value.code == "net_denied"
    assert "not in AGENT_NET_ALLOW_HOSTS" in str(excinfo.value)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com"])
def test_only_http_schemes_are_allowed(net_settings, url):
    net_settings(net_allow_hosts="*")
    with pytest.raises(net.NetPolicyError) as excinfo:
        net.check_url(url)
    assert "not allowed" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# SSRF: internal addresses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8091/rpc",
        "http://10.0.2.2:8091/health",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:8090/health",
        "http://0.0.0.0/",
    ],
)
def test_private_and_metadata_addresses_are_refused(net_settings, url):
    net_settings(net_allow_hosts="*")
    with pytest.raises(net.NetPolicyError) as excinfo:
        net.check_url(url)
    assert "internal address" in str(excinfo.value)


def test_private_hosts_can_be_allowed_explicitly_for_testing(net_settings):
    net_settings(net_allow_hosts="*", net_allow_private_hosts=True)
    net.check_url("http://127.0.0.1:80/health")  # must not raise (port 80 is allowed)


def test_port_not_in_the_allowlist_is_refused(net_settings):
    net_settings(net_allow_hosts="example.com")
    net.check_url("https://example.com/x")  # 443 allowed by default
    net.check_url("http://example.com/x")  # 80 as well
    with pytest.raises(net.NetPolicyError) as excinfo:
        net.check_url("http://example.com:8080/admin")
    assert "port 8080" in str(excinfo.value)


def test_an_admin_port_on_an_allowed_host_is_refused(net_settings):
    net_settings(net_allow_hosts="example.com")
    for url in ("http://example.com:22/", "http://example.com:5432/", "http://example.com:8091/rpc"):
        with pytest.raises(net.NetPolicyError) as excinfo:
            net.check_url(url)
        assert "not allowed" in str(excinfo.value)


def test_custom_port_allowlist(net_settings):
    net_settings(net_allow_hosts="example.com", net_allow_ports="8443")
    net.check_url("https://example.com:8443/api")
    with pytest.raises(net.NetPolicyError):
        net.check_url("https://example.com/x")


def test_internal_addresses_are_reported_before_port_rules(net_settings):
    """The more severe reason wins: a private address is named as such."""
    net_settings(net_allow_hosts="*")
    with pytest.raises(net.NetPolicyError) as excinfo:
        net.check_url("http://127.0.0.1:9999/")
    assert "internal address" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# requests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fetch_saves_into_the_sandbox_and_reports_a_summary(net_settings, monkeypatch):
    net_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"hello": "world"})

    monkeypatch.setattr(net.httpx, "AsyncClient", _client_factory(handler))
    saved: list[dict] = []

    class FakeGateway:
        async def invoke_native(self, session_id, method, params, timeout_s=None):
            saved.append({"session": session_id, "method": method, "params": params})

            class Outcome:
                ok = True
                error = None

            return Outcome()

    class FakeCtx:
        session_id = "s1"
        gateway = FakeGateway()

    result = await net.net_fetch(FakeCtx(), {"url": "https://example.com/data.json", "dest": "/workspace/downloads/d.json"})
    assert result["ok"] is True
    assert result["status_code"] == 200
    assert result["saved"]["path"] == "/workspace/downloads/d.json"
    assert result["saved"]["bytes"] == len(b'{"hello":"world"}')  # httpx json= is compact
    assert saved[0]["method"] == "fs.write"
    assert saved[0]["params"]["path"] == "/workspace/downloads/d.json"


@pytest.mark.asyncio
async def test_response_body_is_capped(net_settings, monkeypatch):
    net_settings(net_max_bytes=10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 5000)

    monkeypatch.setattr(net.httpx, "AsyncClient", _client_factory(handler))
    result = await net.request("GET", "https://example.com/big")
    assert len(result.body) == 10
    assert result.truncated is True


def _client_factory(handler):
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    return factory


@pytest.mark.asyncio
async def test_redirect_to_a_non_allowlisted_host_is_refused(net_settings, monkeypatch):
    net_settings(net_allow_hosts="example.com")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        return httpx.Response(200, content=b"should never be fetched")

    monkeypatch.setattr(net.httpx, "AsyncClient", _client_factory(handler))
    with pytest.raises(net.NetPolicyError) as excinfo:
        await net.request("GET", "https://example.com/start")
    assert "evil.example" in str(excinfo.value)


@pytest.mark.asyncio
async def test_transport_errors_become_a_clear_error(net_settings, monkeypatch):
    net_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(net.httpx, "AsyncClient", _client_factory(handler))
    with pytest.raises(net.NetPolicyError) as excinfo:
        await net.request("GET", "https://example.com/x")
    assert excinfo.value.code == "net_unreachable"


# --------------------------------------------------------------------------- #
# audit trail
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_every_attempt_is_audited(net_settings, monkeypatch, caplog):
    net_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")

    monkeypatch.setattr(net.httpx, "AsyncClient", _client_factory(handler))

    class FakeGateway:
        async def invoke_native(self, *a, **k):
            class Outcome:
                ok = True
                error = None

            return Outcome()

    class FakeCtx:
        session_id = "s1"
        gateway = FakeGateway()

    with caplog.at_level("INFO", logger="agent.ai.net"):
        await net.net_fetch(FakeCtx(), {"url": "https://example.com/ok"})
        await net.net_fetch(FakeCtx(), {"url": "https://evil.example/nope"})

    # the audit trail is structured logging, not a file: the AI service may not write
    entries = [record.getMessage() for record in caplog.records if "net.audit" in record.getMessage()]
    assert any('"event": "fetch"' in line for line in entries)
    assert any('"event": "fetched"' in line for line in entries)
    refused = [line for line in entries if '"event": "refused"' in line]
    assert refused and '"code": "net_denied"' in refused[0]
