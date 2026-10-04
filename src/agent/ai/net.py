"""Network access for the agent -- behind a switch and a firewall.

Design
------
The sandbox VM keeps **no network interface at all** (that invariant is what stops
model-authored code from exfiltrating the workspace).  Network access is instead a
*capability of the AI service*, which already talks to the LLM API, exposed to the
model as two core tools:

* ``net.fetch`` -- GET a URL, save the body into the sandbox workspace
* ``net.http``  -- any method/headers/body, optionally save the response

The firewall is enforced here, in the AI service, on every request and on every
redirect hop:

* the global switch ``AGENT_NET_ENABLED`` (default **off**) -- when off the tools are
  hidden from ``search_tools`` and calling them is refused
* an allowlist ``AGENT_NET_ALLOW_HOSTS`` (comma separated, ``*.example.com`` wildcards,
  a bare ``*`` to allow everything -- documented as unsafe)
* only ``http``/``https``
* private, loopback, link-local and metadata addresses are refused, both when the URL
  contains an IP literal and after the connection is made (the resolved peer address
  is checked too)
* a per-request byte cap and a timeout, with redirects re-validated
* every attempt is appended to a JSONL audit log
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from agent.config import settings
from agent.models.protocol import ErrorCode, RpcError

log = logging.getLogger(__name__)

TEXT_PREVIEW_CHARS = 4000
WRITE_CHUNK_BYTES = 1_500_000  # base64 inflates 4/3, protocol frames cap at 8 MiB
ALLOWED_SCHEMES = ("http", "https")


class NetPolicyError(Exception):
    """A request refused by the firewall (not a transport failure)."""

    def __init__(self, message: str, code: str = "net_denied") -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# policy helpers
# --------------------------------------------------------------------------- #


def parse_allowlist(raw: str) -> list[str]:
    entries = []
    for item in (raw or "").replace(";", ",").split(","):
        host = item.strip().lower()
        if not host:
            continue
        if host.startswith("https://") or host.startswith("http://"):
            host = urlparse(host).hostname or ""
        entries.append(host.rstrip("."))
    return entries


def allowed_port_set() -> set[int]:
    """Ports the firewall lets through (defaults to 80/443)."""
    ports: set[int] = set()
    for item in (settings.net_allow_ports or "").replace(";", ",").split(","):
        item = item.strip()
        if item.isdigit():
            ports.add(int(item))
    return ports or {80, 443}


def host_allowed(host: str, allowlist: list[str]) -> bool:
    """Exact match, ``*.suffix`` wildcard, or a bare ``*``."""
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    for entry in allowlist:
        if entry == "*":
            return True
        if entry.startswith("*."):
            suffix = entry[2:]
            # example.com does not match *.example.com, evil-example.com never does
            if host.endswith("." + suffix) or host == suffix:
                return True
        elif host == entry:
            return True
    return False


def _address_is_blocked(host: str) -> str | None:
    """Reason string when ``host`` is an IP literal pointing somewhere internal."""
    candidate = host.strip("[]")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return f"{address} is a private/internal address"
    return None


def _peer_is_blocked(server_addr: Any) -> str | None:
    """Check the address we actually connected to (covers DNS rebinding)."""
    if not isinstance(server_addr, (tuple, list)) or not server_addr:
        return None
    host = str(server_addr[0]).strip("[]")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
        return f"resolved to internal address {address}"
    return None


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #


def audit(event: dict[str, Any]) -> None:
    """One structured line per network attempt (allowed or refused).

    Deliberately *not* a file: the AI service must never touch the filesystem
    (tests/unit/test_isolation_guard.py enforces that).  systemd captures stdout
    into the journal, so the trail is:

        journalctl -u agentbox-ai -g net.audit
    """
    log.info("net.audit %s", json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()), **event}, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# the request itself
# --------------------------------------------------------------------------- #


def check_url(url: str) -> tuple[str, str, str]:
    """Validate scheme + host against the firewall.  Returns (scheme, host, port-ish)."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise NetPolicyError(f"scheme {scheme or '(none)'!r} is not allowed; only http/https", "net_denied")
    host = (parsed.hostname or "").lower()
    if not host:
        raise NetPolicyError(f"{url!r} has no host", "invalid_args")
    if not settings.net_enabled:
        raise NetPolicyError("network access is disabled (set AGENT_NET_ENABLED=1)", "net_disabled")
    allowlist = settings.net_allowlist()
    if allowlist and not host_allowed(host, allowlist):
        raise NetPolicyError(
            f"host {host!r} is not in AGENT_NET_ALLOW_HOSTS ({', '.join(allowlist[:8]) or 'empty'})",
            "net_denied",
        )
    if not allowlist:
        raise NetPolicyError(
            "no host is allowed: AGENT_NET_ALLOW_HOSTS is empty "
            "(set it to a comma separated list, or to * to allow everything)",
            "net_denied",
        )
    # The more severe reason wins: an internal address is reported as such even if the
    # port would also have been refused.
    if not settings.net_allow_private_hosts:
        blocked = _address_is_blocked(host)
        if blocked:
            raise NetPolicyError(f"refusing {host!r}: {blocked}", "net_denied")
    allowed_ports = allowed_port_set()
    port = parsed.port or (443 if scheme == "https" else 80)
    if port not in allowed_ports:
        raise NetPolicyError(
            f"port {port} is not allowed (AGENT_NET_ALLOW_PORTS={sorted(allowed_ports)})",
            "net_denied",
        )
    return scheme, host, parsed.path


async def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    max_bytes: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchResult:
    """Perform one firewalled HTTP request, following redirects manually."""
    cap = int(max_bytes or settings.net_max_bytes)
    request_headers = {
        "User-Agent": settings.net_user_agent,
        "Accept": "*/*",
        **(headers or {}),
    }
    redirects: list[str] = []
    current = url
    timeout = httpx.Timeout(settings.net_timeout_s, connect=min(10.0, settings.net_timeout_s))

    async with httpx.AsyncClient(
        follow_redirects=False, timeout=timeout, transport=transport, headers=request_headers
    ) as client:
        for hop in range(settings.net_max_redirects + 1):
            check_url(current)
            try:
                async with client.stream(method, current, content=body) as response:
                    if response.is_redirect and response.headers.get("location"):
                        if hop >= settings.net_max_redirects:
                            raise NetPolicyError(
                                f"more than {settings.net_max_redirects} redirects", "net_denied"
                            )
                        location = str(response.headers["location"])
                        nxt = httpx.URL(current).join(location)
                        redirects.append(str(nxt))
                        audit({"event": "redirect", "from": current, "to": str(nxt)})
                        current = str(nxt)
                        continue

                    if not settings.net_allow_private_hosts:
                        stream = response.extensions.get("network_stream")
                        if stream is not None:
                            with_peer = stream.get_extra_info("server_addr")
                            blocked = _peer_is_blocked(with_peer)
                            if blocked:
                                raise NetPolicyError(f"refusing {current!r}: {blocked}", "net_denied")

                    chunks: list[bytes] = []
                    size = 0
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        if size + len(chunk) > cap:
                            chunks.append(chunk[: cap - size])
                            size = cap
                            truncated = True
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                    return FetchResult(
                        url=url,
                        final_url=current,
                        status_code=response.status_code,
                        headers={k.lower(): v for k, v in response.headers.items()},
                        body=b"".join(chunks),
                        truncated=truncated,
                        redirects=redirects,
                    )
            except httpx.HTTPError as exc:
                raise NetPolicyError(f"request failed: {type(exc).__name__}: {exc}", "net_unreachable") from exc
    raise NetPolicyError("redirect loop", "net_denied")  # pragma: no cover


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #


def _guess_name(url: str, content_type: str) -> str:
    tail = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "download"
    tail = "".join(ch for ch in tail if ch.isalnum() or ch in "._-")[:80] or "download"
    if "." not in tail:
        if "json" in content_type:
            tail += ".json"
        elif "html" in content_type:
            tail += ".html"
        elif "text/" in content_type:
            tail += ".txt"
    return tail


async def _save_to_sandbox(ctx: Any, dest: str, payload: bytes) -> dict[str, Any]:
    """Write bytes into the sandbox workspace in protocol-sized chunks."""
    written = 0
    first = True
    for offset in range(0, len(payload), WRITE_CHUNK_BYTES):
        chunk = payload[offset : offset + WRITE_CHUNK_BYTES]
        params = {
            "path": dest,
            "data_b64": base64.b64encode(chunk).decode("ascii"),
            "append": not first,
            "create_dirs": True,
        }
        outcome = await ctx.gateway.invoke_native(ctx.session_id, "fs.write", params, timeout_s=60.0)
        if not outcome.ok:
            raise RpcError(
                ErrorCode.SANDBOX_ERROR,
                f"cannot save to {dest}: {outcome.error}",
            )
        written += len(chunk)
        first = False
    return {"path": dest, "bytes": written}


async def net_fetch(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url") or "").strip()
    dest = args.get("dest")
    max_bytes = args.get("max_bytes")
    audit({"event": "fetch", "url": url, "enabled": settings.net_enabled})
    try:
        result = await request("GET", url, max_bytes=int(max_bytes) if max_bytes else None)
    except NetPolicyError as exc:
        audit({"event": "refused", "url": url, "reason": str(exc), "code": exc.code})
        return {"ok": False, "error": str(exc), "error_code": exc.code}

    payload: dict[str, Any] = {
        "ok": True,
        "url": url,
        "final_url": result.final_url,
        "status_code": result.status_code,
        "content_type": result.content_type,
        "bytes": len(result.body),
        "sha256": result.sha256,
        "truncated": result.truncated,
        "redirects": result.redirects,
    }
    if dest:
        target = str(dest) if str(dest).startswith("/workspace") else f"/workspace/downloads/{_guess_name(url, result.content_type)}"
        saved = await _save_to_sandbox(ctx, target, result.body)
        payload["saved"] = saved
        payload["hint"] = f"read it with fs.read('{saved['path']}') or process it with a sandbox command"
    if "text/" in result.content_type or "json" in result.content_type or "xml" in result.content_type:
        text = result.body.decode("utf-8", errors="replace")
        payload["text"] = text[:TEXT_PREVIEW_CHARS]
        payload["text_chars"] = len(text)
        if len(text) > TEXT_PREVIEW_CHARS:
            payload["text_truncated"] = True
    audit(
        {
            "event": "fetched",
            "url": url,
            "status": result.status_code,
            "bytes": len(result.body),
        }
    )
    return payload


async def net_http(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    method = str(args.get("method") or "GET").upper()
    url = str(args.get("url") or "").strip()
    headers = args.get("headers") or {}
    body_text = args.get("body")
    dest = args.get("dest")
    audit({"event": "http", "method": method, "url": url, "enabled": settings.net_enabled})
    if not isinstance(headers, dict):
        return {"ok": False, "error": "headers must be an object", "error_code": "invalid_args"}
    try:
        result = await request(
            method,
            url,
            headers={str(k): str(v) for k, v in headers.items()},
            body=body_text.encode("utf-8") if isinstance(body_text, str) else None,
        )
    except NetPolicyError as exc:
        audit({"event": "refused", "url": url, "reason": str(exc), "code": exc.code})
        return {"ok": False, "error": str(exc), "error_code": exc.code}

    payload: dict[str, Any] = {
        "ok": True,
        "method": method,
        "url": url,
        "final_url": result.final_url,
        "status_code": result.status_code,
        "content_type": result.content_type,
        "bytes": len(result.body),
        "sha256": result.sha256,
        "truncated": result.truncated,
    }
    if dest:
        target = str(dest) if str(dest).startswith("/workspace") else f"/workspace/downloads/{_guess_name(url, result.content_type)}"
        payload["saved"] = await _save_to_sandbox(ctx, target, result.body)
    if "text/" in result.content_type or "json" in result.content_type:
        payload["text"] = result.body.decode("utf-8", errors="replace")[:TEXT_PREVIEW_CHARS]
    audit({"event": "done", "method": method, "url": url, "status": result.status_code, "bytes": len(result.body)})
    return payload


async def net_ping(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """TCP reachability probe, run by the AI service.

    The sandbox has no network device at all, so "can this host be reached" has to be
    answered from the service side.  It goes through the same allowlist, port allowlist
    and private-address checks as net.fetch -- a probe is exactly as sensitive as a
    fetch, so it must not become a port scanner for the operator's own network.
    """
    host = str(args.get("host") or "").strip()
    try:
        port = int(args.get("port") or 443)
    except (TypeError, ValueError):
        return {"ok": False, "error": "port must be an integer", "error_code": "invalid_args"}
    timeout_s = float(args.get("timeout_s") or 5.0)
    audit({"event": "ping", "host": host, "port": port, "enabled": settings.net_enabled})

    if not settings.net_enabled:
        return {"ok": False, "error": "the network switch is off", "error_code": "net_disabled"}
    allowlist = settings.net_allowlist()
    if not host_allowed(host, allowlist):
        audit({"event": "refused", "host": host, "reason": "host not allowlisted", "code": "host_not_allowed"})
        return {
            "ok": False,
            "error": f"{host!r} is not in the allowlist ({len(allowlist)} entries); add it with /net allow {host}",
            "error_code": "host_not_allowed",
            "allowlist": allowlist[:20],
        }
    if port not in allowed_port_set() and not settings.net_allow_private_hosts:
        return {
            "ok": False,
            "error": f"port {port} is not allowed (allowed: {sorted(allowed_port_set())})",
            "error_code": "port_not_allowed",
        }
    if not settings.net_allow_private_hosts:
        blocked = _address_is_blocked(host)
        if blocked:
            return {"ok": False, "error": f"refused to probe {blocked}", "error_code": "private_address"}

    try:
        infos = await asyncio.wait_for(asyncio.getaddrinfo(host, port), timeout=timeout_s)
    except TimeoutError:
        return {"ok": False, "error": f"DNS lookup for {host} timed out", "error_code": "dns_timeout"}
    except OSError as exc:
        return {"ok": False, "error": f"DNS lookup for {host} failed: {exc}", "error_code": "dns_failed"}
    resolved = sorted({str(info[4][0]) for info in infos})
    if not settings.net_allow_private_hosts:
        for address in resolved:
            blocked = _address_is_blocked(address)
            if blocked:
                audit({"event": "refused", "host": host, "reason": blocked, "code": "private_address"})
                return {
                    "ok": False,
                    "error": f"{host} resolves to {address}: {blocked}",
                    "error_code": "private_address",
                    "resolved": resolved,
                }

    started = time.monotonic()
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_s)
        del reader
        latency_ms = int((time.monotonic() - started) * 1000)
        peer = writer.get_extra_info("peername")
        address = str(peer[0]) if isinstance(peer, tuple) and peer else None
        if address and not settings.net_allow_private_hosts:
            blocked = _address_is_blocked(address)
            if blocked:  # defence in depth: the address we actually connected to
                audit({"event": "refused", "host": host, "reason": blocked, "code": "private_peer"})
                return {"ok": False, "error": f"connected to {address}: {blocked}", "error_code": "private_peer"}
        audit({"event": "ping_done", "host": host, "port": port, "latency_ms": latency_ms, "address": address})
        return {
            "ok": True,
            "host": host,
            "port": port,
            "resolved": resolved,
            "address": address,
            "latency_ms": latency_ms,
            "reachable": True,
        }
    except TimeoutError:
        return {
            "ok": False,
            "host": host,
            "port": port,
            "resolved": resolved,
            "reachable": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": f"no TCP answer from {host}:{port} within {timeout_s:g}s",
            "error_code": "timeout",
        }
    except OSError as exc:
        return {
            "ok": False,
            "host": host,
            "port": port,
            "resolved": resolved,
            "reachable": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
            "error_code": "unreachable",
        }
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - closing best effort
                pass


NetHandler = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]

NET_HANDLERS: dict[str, NetHandler] = {
    "net.fetch": net_fetch,
    "net.http": net_http,
    "net.ping": net_ping,
}

NET_TOOL_NAMES = tuple(NET_HANDLERS)


def net_tool_enabled(name: str) -> bool:
    return name in NET_HANDLERS and settings.net_enabled
