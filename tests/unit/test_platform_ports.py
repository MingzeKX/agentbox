"""``AGENT_PLATFORM_PORTS``: extra platform-VM port forwards, loopback only.

The launcher is ``deploy/windows/run-platform-vm.ps1`` and the operator edits ``.env``,
so the test executes the very functions that script calls: it is dot-sourced, and the
``$MyInvocation.InvocationName`` guard makes it define its helpers and return instead of
checking for a disk.  Same trick as ``test_sandbox_pylibs.py`` running the shell snippet
it pins.  No VM, no network, no service is touched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "windows" / "run-platform-vm.ps1"

FORWARDS = "hostfwd=tcp:127.0.0.1:8090-:8090,hostfwd=tcp:127.0.0.1:8099-:8099,hostfwd=tcp:127.0.0.1:2222-:22"
NIC_BASE = f"user,model=virtio-net-pci,{FORWARDS}"


def _powershell(harness_body: str, tmp_path: Path) -> str:
    """Run ``harness_body`` after dot-sourcing the real launcher; return stdout."""
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        f". '{SCRIPT}'\n" + harness_body + "\n",
        encoding="utf-8",
        newline="\n",
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter, test-owned harness
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_the_spec_parses_single_ports_ranges_whitespace_and_duplicates(tmp_path):
    out = _powershell(
        "Write-Output ((Get-PlatformForwardArgs -Spec ' 2121 , 30000 - 30002 ,8080,2121 ,')"
        " -join ';')\n"
        "Write-Output \"empty=[$(@(Get-PlatformForwardArgs -Spec '').Count)]\"",
        tmp_path,
    )
    lines = out.strip().splitlines()
    assert lines[0] == ";".join(
        f"hostfwd=tcp:127.0.0.1:{p}-:{p}" for p in (2121, 30000, 30001, 30002, 8080)
    ), "whitespace is tolerated, ranges expand, a repeated port is forwarded once"
    assert lines[1] == "empty=[0]", "an empty setting must add no forward at all"


def test_the_spec_refuses_smb_bind_addresses_and_bad_tokens(tmp_path):
    cases = [
        ("445", "8445"),  # the host owns SMB; the message has to name a usable port
        ("139", "8445"),
        ("2121,440-446", "8445"),  # a range that only reaches 445 is refused too
        ("127.0.0.1:2121", "127.0.0.1"),  # an explicit bind address is never accepted
        ("0.0.0.0:8080", "127.0.0.1"),
        ("192.168.1.9:8080", "127.0.0.1"),
        ("0", "1-65535"),
        ("70000", "1-65535"),
        ("30010-30000", "1-65535"),
        ("abc", "2121"),
    ]
    specs = "|".join(spec for spec, _ in cases)
    out = _powershell(
        f"foreach ($spec in '{specs}'.Split('|')) {{"
        " try { $null = Get-PlatformForwardArgs -Spec $spec; Write-Output \"$spec => ACCEPTED\" }"
        " catch { Write-Output \"$spec => REFUSED $($_.Exception.Message)\" } }",
        tmp_path,
    )
    reported = dict(line.split(" => ", 1) for line in out.strip().splitlines())
    assert sorted(reported) == sorted(spec for spec, _ in cases), out
    for spec, expected in cases:
        message = reported[spec]
        assert message.startswith("REFUSED "), f"{spec} must be refused: {message}"
        assert expected in message, f"{spec}: the fix ({expected}) must be in the message: {message}"
        assert spec.split(",")[-1] in message, f"{spec}: the offending token must be named: {message}"


def test_the_nic_argument_appends_exactly_the_loopback_forwards(tmp_path):
    out = _powershell(
        "Write-Output (Get-PlatformNicArg -Spec '2121,30000-30001' -SshPort 2222)\n"
        "Write-Output (Get-PlatformNicArg -Spec '' -SshPort 2222)",
        tmp_path,
    )
    nic, plain = out.strip().splitlines()
    assert nic == (
        f"{NIC_BASE},hostfwd=tcp:127.0.0.1:2121-:2121,"
        "hostfwd=tcp:127.0.0.1:30000-:30000,hostfwd=tcp:127.0.0.1:30001-:30001"
    )
    assert plain == NIC_BASE, "no setting -> the -nic argument is unchanged"
    assert "0.0.0.0" not in nic
    # the launcher really builds its -nic from these helpers, not from a copy that could drift
    source = SCRIPT.read_text(encoding="utf-8")
    assert "'-nic', $nicArg," in source
    assert "AGENT_PLATFORM_PORTS" in source
