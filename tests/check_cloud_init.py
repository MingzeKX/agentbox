"""Render and validate the cloud-init seed the way provision-cloud-vm.ps1 does.

A YAML or schema mistake in user-data makes cloud-init fall back to
DataSourceNone and silently configure *nothing*, so this check is worth having in
the suite:

  * every @PLACEHOLDER@ is substituted
  * the rendered user-data parses as YAML
  * it starts with #cloud-config (no BOM, no leading blank line)
  * the pieces we depend on are present (bootcmd removing the stock apt source,
    our deb822 mirror file, pip config, packages, the tar/install runcmd)
  * meta-data carries instance-id + local-hostname
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CLOUD_INIT = REPO / "deploy" / "windows" / "cloud-init"
TEMPLATE = CLOUD_INIT / "user-data.tpl"
META_TEMPLATE = CLOUD_INIT / "meta-data.tpl"

VALUES = {
    "@HOSTNAME@": "agentbox-platform",
    "@MIRROR_URI@": "http://mirrors.tuna.tsinghua.edu.cn/debian",
    "@SECURITY_URI@": "http://mirrors.tuna.tsinghua.edu.cn/debian-security",
    "@PIP_INDEX@": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "@PIP_HOST@": "pypi.tuna.tsinghua.edu.cn",
    "@INSTANCE_ID@": "agentbox-test",
    "@PASSWORD@": "agentbox",
    "@SERVE_URL@": "http://10.0.2.2:8901/",
    "@SSH_KEY@": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEYFORTHECHECKER agentbox",
}

problems: list[str] = []


def render(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append(f"{path.name}: template starts with a UTF-8 BOM")
        raw = raw[3:]
    text = raw.decode("utf-8")
    for key, value in VALUES.items():
        text = text.replace(key, value)
    return text


user_data = render(TEMPLATE)
meta_data = render(META_TEMPLATE)

# 1. all placeholders substituted
leftover = sorted(set(re.findall(r"@[A-Z_]+@", user_data + meta_data)))
if leftover:
    problems.append(f"unsubstituted placeholders: {leftover}")

# 2. header / BOM
if not user_data.startswith("#cloud-config"):
    problems.append(f"user-data must start with '#cloud-config', got {user_data[:24]!r}")

# 3. YAML parses
try:
    config = yaml.safe_load(user_data)
except yaml.YAMLError as exc:
    problems.append(f"user-data is not valid YAML: {exc}")
    config = {}
try:
    meta = yaml.safe_load(meta_data)
except yaml.YAMLError as exc:
    problems.append(f"meta-data is not valid YAML: {exc}")
    meta = {}

if isinstance(config, dict):
    # 4. required content
    if "bootcmd" not in config:
        problems.append("user-data has no bootcmd (needed to drop the stock apt source)")
    else:
        bootcmd = str(config["bootcmd"])
        if "debian.sources" not in bootcmd:
            problems.append("bootcmd does not remove /etc/apt/sources.list.d/debian.sources")

    files = {entry.get("path"): entry for entry in config.get("write_files", []) if isinstance(entry, dict)}
    apt_source = files.get("/etc/apt/sources.list.d/agentbox.sources")
    if not apt_source:
        problems.append("no deb822 apt source written to /etc/apt/sources.list.d/agentbox.sources")
    else:
        content = str(apt_source.get("content", ""))
        if VALUES["@MIRROR_URI@"] not in content:
            problems.append("the apt source does not point at the configured mirror")
        # the written file must itself be valid deb822 with the required fields
        for field in ("Types:", "URIs:", "Suites:", "Components:", "Signed-By:"):
            if field not in content:
                problems.append(f"apt source is missing {field}")
    if "/etc/pip.conf" not in files:
        problems.append("no /etc/pip.conf written (pip would hit the slow default index)")

    packages = config.get("packages") or []
    for needed in ("sudo", "curl", "ca-certificates"):
        if needed not in packages:
            problems.append(f"user-data does not install {needed}")
    # Regression guard: installing postgresql during cloud-init's config stage
    # trips its debconf postinst ("pg_lsclusters: not found") and fails
    # cloud-config.service, which silently skips the rest of the config stage.
    for forbidden in ("postgresql", "postgresql-17-pgvector", "python3-venv"):
        if forbidden in packages:
            problems.append(
                f"{forbidden} must NOT be installed from cloud-init's packages list; "
                "deploy/platform/install-platform.sh installs it in runcmd instead"
            )

    runcmd = str(config.get("runcmd", []))
    if "agentbox.tar.gz" not in runcmd:
        problems.append("runcmd does not fetch the repository tarball from the host")
    if "install-platform.sh" not in runcmd:
        problems.append("runcmd does not run install-platform.sh")
    if "/dev/ttyS0" not in runcmd:
        problems.append("runcmd does not report the provisioning result on the serial console")
    # Regression guard: tarballs created on Windows have no executable bit, so
    # the scripts must be chmod'ed (or invoked through bash) after extraction.
    if "chmod 0755" not in runcmd:
        problems.append("runcmd does not chmod the extracted *.sh files (Windows tarballs lose +x)")

    installer = REPO / "deploy" / "platform" / "install-platform.sh"
    installer_text = installer.read_text(encoding="utf-8") if installer.is_file() else ""
    for needed in ("postgresql-17-pgvector", "postgresql", "python3-venv", "PIP_INDEX_URL"):
        if needed not in installer_text:
            problems.append(f"install-platform.sh does not mention {needed}")

    users = str(config.get("users", []))
    if "ssh_authorized_keys" not in users:
        problems.append("the agent user has no ssh_authorized_keys (host could not operate the VM)")

    chpasswd = str(config.get("chpasswd", {}))
    if "agent" not in chpasswd:
        problems.append("chpasswd does not set a password for the agent user")
else:
    problems.append("user-data did not parse into a mapping")

if isinstance(meta, dict):
    if not meta.get("instance-id"):
        problems.append("meta-data has no instance-id")
    if not meta.get("local-hostname"):
        problems.append("meta-data has no local-hostname")
else:
    problems.append("meta-data did not parse into a mapping")

# 5. report
print(f"user-data : {len(user_data)} bytes, {user_data.count(chr(10)) + 1} lines")
print(f"meta-data : {len(meta_data)} bytes")
if problems:
    for problem in problems:
        print(f"FAIL {problem}")
    sys.exit(1)
print("cloud-init seed is valid: placeholders, header, YAML and required content all check out")
