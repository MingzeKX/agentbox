"""The licence boundary of the ``-Slim`` bundle, pinned as a contract.

Why this file exists: ``-Slim`` is the preset we hand to somebody else, and its whole point is
that the payload carries **no third-party binaries** -- no QEMU (GPLv2) and no VM image (those
images are assembled from Debian components).  The rule cannot live only in prose.

Both tests are offline and never enter a deployment flow:

* the payload test asks the bundler for its *listing* (``-ListPayload``: read-only, no staging
  directory, no zip, no network) and independently exercises the rule function itself, with
  synthetic entries, by dot-sourcing ``make-bundle.ps1``;
* the QEMU test dot-sources ``setup-agentbox.ps1`` -- both scripts return early when dot-sourced
  -- and calls only ``Resolve-Qemu``/``Show-QemuMissing``.  No ``-Check``, no VM, no service,
  no installer is invoked.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BUNDLER = REPO / "packaging" / "make-bundle.ps1"
SETUP = REPO / "packaging" / "setup-agentbox.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")

# The same rule the bundler enforces (see $PayloadForbiddenRules in make-bundle.ps1).  It is
# deliberately narrow: tests/smoke_qemu_argv.py is *our* source and has to stay allowed.
FORBIDDEN = re.compile(r"(?i)(^|/)qemu(/|$)|(^|/)qemu[^/]*\.(exe|dll)$|\.(img|qcow2|iso|vmdk|raw)$")

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="these contract tests run the repo's .ps1 logic")


def _pwsh(script: Path, args: list[str], *, env: dict[str, str] | None = None, timeout: int = 240):
    """Run a repo script with Windows PowerShell 5.1; return the CompletedProcess."""
    return subprocess.run(  # noqa: S603 - fixed interpreter, repo-owned scripts
        [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=env,
    )


def _harness(tmp_path: Path, body: str) -> str:
    """Run PowerShell in a temp dir: no QEMU on PATH, no AGENT_QEMU_DIR, UTF-8 output."""
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n" + body + "\n",
        encoding="utf-8",
        newline="\n",
    )
    env = dict(os.environ)
    env.pop("AGENT_QEMU_DIR", None)
    empty = tmp_path / "no-qemu-on-path"
    empty.mkdir(exist_ok=True)
    env["PATH"] = os.pathsep.join([str(empty), str(Path(env.get("WINDIR", r"C:\Windows")) / "System32")])
    completed = subprocess.run(  # noqa: S603 - fixed interpreter, test-owned harness
        [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout

def _entries(stdout: str) -> list[str]:
    return [line.strip() for line in stdout.splitlines() if line.strip() and not line.startswith("#")]


def test_the_slim_payload_has_no_qemu_binaries_and_no_vm_images(tmp_path: Path):
    listed = _pwsh(BUNDLER, ["-Slim", "-ListPayload"])
    assert listed.returncode == 0, listed.stdout + listed.stderr
    entries = _entries(listed.stdout)
    assert entries, "the payload listing printed no entries"
    for expected in ("pyproject.toml", "packaging/setup-agentbox.ps1", "THIRD-PARTY.md"):
        assert expected in entries, f"{expected} must be part of the -Slim payload"
    offenders = [e for e in entries if FORBIDDEN.search(e)]
    assert not offenders, f"-Slim must not carry QEMU or VM images; payload has {offenders}"
    assert "# OK: -Slim payload" in listed.stdout, "the bundler has to state the check it passed"

    # ...and the rule is not vacuous, on synthetic entries fed straight to the function.
    quoted_bundler = str(BUNDLER).replace("'", "''")
    out = _harness(
        tmp_path,
        ". '" + quoted_bundler + "'\n"
        "$bad = @('qemu/qemu-system-x86_64.exe', 'qemu/share/bios-256k.bin', 'qemu/x.dll',\n"
        "         'var/sandbox/rootfs.img', 'var/sandbox/workspace-blank.qcow2', 'debian-13-amd64.iso',\n"
        "         'var/platform/platform.qcow2', 'var/platform/disk.vmdk')\n"
        "$ok = @('tests/smoke_qemu_argv.py', 'src/agent/control/vm.py', 'deploy/sandbox/build-sandbox-image.sh')\n"
        "Write-Output ('caught=' + (@(Get-ForbiddenPayloadEntry -rels $bad).Count) + '/' + $bad.Count)\n"
        "Write-Output ('spared=' + @(Get-ForbiddenPayloadEntry -rels $ok).Count)",
    )
    assert "caught=8/8" in out, out
    assert "spared=0" in out, "our own qemu-named source files must never be treated as QEMU binaries"

    # Combining -Slim with -IncludeQemu must refuse instead of quietly shipping a GPLv2 binary.
    refused = _pwsh(BUNDLER, ["-Slim", "-IncludeQemu", "-ListPayload"])
    assert refused.returncode != 0, "combining -Slim with -IncludeQemu must fail"
    assert "-IncludeQemu" in refused.stdout + refused.stderr, "the refusal has to name the offending switch"


def test_a_recipient_without_qemu_is_told_where_to_get_it(tmp_path: Path):
    """Dot-source the script's pure functions: a machine with no QEMU gets instructions."""
    # The dot-source above is only safe because the script returns early in that case
    # (same idiom as deploy/windows/run-platform-vm.ps1): no preflight, no VM, no installer.
    assert "if ($MyInvocation.InvocationName -eq '.') { return }" in SETUP.read_text(encoding="utf-8")
    repo = tmp_path / "recipient"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "agentbox"\nversion = "0"\n', encoding="utf-8")
    message = tmp_path / "qemu-missing.txt"
    quoted_setup = str(SETUP).replace("'", "''")
    quoted_repo = str(repo).replace("'", "''")
    quoted_message = str(message).replace("'", "''")
    out = _harness(
        tmp_path,
        ". '" + quoted_setup + "' -RepoRoot '" + quoted_repo + "'\n"
        "Write-Output ('resolved=[' + (Resolve-Qemu) + ']')\n"
        "$cap = (& { Show-QemuMissing } 6>&1 | Out-String)\n"
        "[System.IO.File]::WriteAllText('" + quoted_message + "', $cap,"
        " (New-Object System.Text.UTF8Encoding($false)))\n"
        "Write-Output ('fail=' + $script:Fail.Count)",
    )
    assert "resolved=[]" in out, f"a machine without QEMU must resolve to nothing:\n{out}"
    assert "fail=1" in out, f"the missing QEMU has to be reported as one problem:\n{out}"

    text = message.read_text(encoding="utf-8")
    assert "https://www.qemu.org/download/#windows" in text, "the message must name the official download page"
    assert "AGENT_QEMU_DIR" in text, "the message must offer the env-var option"
    assert "qemu-system-x86_64.exe --version" in text, "the recipient needs a self-check command"
    assert "GPLv2" in text, "the reason it is not in the bundle belongs in the message"

    # The preflight/step text (static: we never run the deploy flow) must promise the one-off
    # online sandbox-image build with its cost, because that is what a fresh recipient hits.
    text_all = SETUP.read_text(encoding="utf-8")
    assert "build-sandbox-image.sh" in text_all, "the image has to be built with the repo's own script"
    assert "5-10 分钟" in text_all and "需要联网" in text_all, "the online build must be costed in the hints"
    assert "fetch-sandbox-image.ps1" in text_all, "the built image has to be pulled back to var\\sandbox"
