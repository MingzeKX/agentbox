"""Ask a live sandbox VM questions through the control plane.

Windows PowerShell mangles JSON on the command line (it strips the double quotes),
so the probes are built-ins selected by name instead of being passed as JSON:

    python var/diag_sandbox.py            # the default environment probes
    python var/diag_sandbox.py mounts
    python var/diag_sandbox.py passwd
    python var/diag_sandbox.py fs-write
    python var/diag_sandbox.py mem
    python var/diag_sandbox.py cgroup
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
SECRET = ""
for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
    if line.startswith("AGENT_CONTROL_SECRET="):
        SECRET = line.split("=", 1)[1].strip()

BASE = "http://127.0.0.1:8091"
SESSION = "diag"

CASES: dict[str, list[tuple[str, dict]]] = {
    "all": [
        ("exec.run", {"argv": ["cat", "/proc/mounts"]}),
        ("exec.run", {"argv": ["cat", "/etc/passwd"]}),
        ("exec.run", {"argv": ["sh", "-c", "ls -la /workspace; df -h /workspace"]}),
        ("exec.run", {"argv": ["sh", "-c", "echo hi > /workspace/probe.txt && cat /workspace/probe.txt"]}),
        ("sandbox.info", {}),
    ],
    "mounts": [("exec.run", {"argv": ["cat", "/proc/mounts"]})],
    "passwd": [
        ("exec.run", {"argv": ["cat", "/etc/passwd"]}),
        ("exec.run", {"argv": ["id"]}),
        ("exec.run", {"argv": ["getent", "passwd", "sandbox"]}),
    ],
    "fs-write": [
        ("fs.write", {"path": "/workspace/hello.txt", "text": "hi from fs.write\n"}),
        ("fs.read", {"path": "/workspace/hello.txt"}),
        ("fs.stat", {"path": "/workspace/hello.txt", "with_sha256": True}),
    ],
    "mem": [("exec.run", {"argv": ["python3", "-c", "b=bytearray(600*1024*1024);print(len(b))"], "timeout_s": 30})],
    "cgroup": [
        (
            "exec.run",
            {
                "argv": [
                    "sh",
                    "-c",
                    "cat /proc/self/cgroup; p=$(awk -F: '{print $3}' /proc/self/cgroup); "
                    "echo cg=$p; cat /sys/fs/cgroup$p/memory.max 2>&1; "
                    "echo peak=$(cat /sys/fs/cgroup$p/memory.peak 2>&1)",
                ]
            },
        ),
    ],
    "exec": [("exec.run", {"argv": ["sh", "-c", "echo out; echo err >&2; exit 3"]})],
    # does a file created by fs.write (executor = root) stay writable for uid 1000?
    "ownership": [
        ("fs.write", {"path": "/workspace/owner-test.txt", "text": "created by fs.write\n"}),
        ("exec.run", {"argv": ["sh", "-c", "ls -l /workspace/owner-test.txt"]}),
        ("exec.run", {"argv": ["sh", "-c", "echo appended >> /workspace/owner-test.txt; echo rc=$?"]}),
        ("exec.run", {"argv": ["sh", "-c", "cat /workspace/owner-test.txt"]}),
    ],
    "reset": [("sandbox.reset", {"keep_tools": True})],
}


def rpc(method: str, params: dict) -> dict:
    response = httpx.post(
        f"{BASE}/rpc",
        json={"method": method, "params": params},
        headers={"Content-Type": "application/json", "X-Agent-Token": SECRET},
        timeout=180.0,
    )
    return response.json()


def invoke(name: str, params: dict) -> dict:
    body = rpc("sandbox.invoke", {"session_id": SESSION, "kind": "native", "method": name, "params": params})
    if not body.get("ok"):
        return {"ok": False, "error": body.get("error")}
    return body["result"]


def main() -> int:
    case = sys.argv[1] if len(sys.argv) > 1 else "all"
    pairs = CASES.get(case)
    if pairs is None:
        print(f"unknown case {case!r}; pick one of: {', '.join(CASES)}")
        return 2
    for name, params in pairs:
        result = invoke(name, params)
        print(f"--- {name} {json.dumps(params, ensure_ascii=False)[:160]}")
        if result.get("ok"):
            payload = result.get("result")
            if isinstance(payload, dict):
                printed = False
                for key in ("stdout", "stderr"):
                    value = payload.get(key)
                    if isinstance(value, str) and value.strip():
                        print(value.rstrip())
                        printed = True
                for key in ("exit_code", "timed_out", "oom", "killed_by_limit", "bytes_written", "sha256"):
                    if key in payload and payload[key] not in (None, "", False):
                        print(f"  {key}={payload[key]}")
                        printed = True
                if not printed:
                    print(json.dumps(payload, ensure_ascii=False)[:1500])
            else:
                print(payload)
        else:
            print(f"  FAILED: {result.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
