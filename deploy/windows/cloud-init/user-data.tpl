#cloud-config
# ---------------------------------------------------------------------------
# agentbox platform provisioning (Debian 13 cloud image, NoCloud datasource)
#
# Rendered from user-data.tpl by deploy/windows/provision-cloud-vm.ps1:
#   @HOSTNAME@ @MIRROR_URI@ @SECURITY_URI@ @PIP_INDEX@ @INSTANCE_ID@ @PASSWORD@ @SERVE_URL@
# ---------------------------------------------------------------------------

hostname: @HOSTNAME@
fqdn: @HOSTNAME@.local
manage_etc_hosts: true
locale: C.UTF-8
timezone: UTC

users:
  - name: agent
    gecos: agentbox platform administrator
    groups: [sudo, adm]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: false
    # injected by provision-cloud-vm.ps1 from var/vm_key.pub: lets the host run
    # commands over ssh (hostfwd 2222 -> 22) instead of typing at a console.
    ssh_authorized_keys:
      - @SSH_KEY@

chpasswd:
  expire: false
  users:
    # Only `agent`: setting a password for the stock `debian` user fails with
    # "pam_chauthtok() failed" in this image and is not needed (agent has sudo).
    - {name: agent, password: "@PASSWORD@", type: text}

ssh_pwauth: true
disable_root: true

# Drop the stock mirror configuration before anything apt-related runs.  The
# trixie cloud image uses a deb822 file plus a mirror-list indirection
# (/etc/apt/sources.list.d/debian.sources -> mirror+file:/etc/apt/mirrors/...),
# which would otherwise keep pulling from deb.debian.org (measured 0 KiB/s from
# CN networks versus ~19 MiB/s for TUNA).
bootcmd:
  - [rm, -f, /etc/apt/sources.list]
  - [rm, -f, /etc/apt/sources.list.d/debian.sources]
  - [sh, -c, "rm -f /etc/apt/mirrors/*.list"]

apt:
  conf: |
    Acquire::Retries "3";
    Acquire::http::Timeout "20";

package_update: true
package_upgrade: false

# Keep this list minimal on purpose: installing postgresql during cloud-init's
# config stage trips its debconf postinst ("pg_lsclusters: not found") and takes
# cloud-config.service down with it.  deploy/platform/install-platform.sh
# installs PostgreSQL + pgvector + python properly a moment later, in runcmd.
packages:
  - sudo
  - curl
  - ca-certificates

write_files:
  - path: /etc/apt/sources.list.d/agentbox.sources
    permissions: "0644"
    content: |
      Types: deb
      URIs: @MIRROR_URI@
      Suites: trixie trixie-updates
      Components: main contrib non-free-firmware
      Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

      Types: deb
      URIs: @SECURITY_URI@
      Suites: trixie-security
      Components: main contrib non-free-firmware
      Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
  - path: /etc/agentbox/provision.env
    permissions: "0644"
    content: |
      AGENTBOX_HOSTNAME=@HOSTNAME@
      AGENTBOX_MIRROR=@MIRROR_URI@
      AGENTBOX_SECURITY_MIRROR=@SECURITY_URI@
      AGENTBOX_PIP_INDEX=@PIP_INDEX@
      AGENTBOX_INSTANCE=@INSTANCE_ID@
  - path: /etc/pip.conf
    permissions: "0644"
    content: |
      [global]
      index-url = @PIP_INDEX@
      trusted-host = @PIP_HOST@
      timeout = 60
      retries = 5

runcmd:
  # 1. fetch this repository from the Windows host and unpack it
  - [mkdir, -p, /opt/agentbox/app]
  - [curl, -fsS, --retry, "3", -o, /opt/agentbox/agentbox.tar.gz, "@SERVE_URL@agentbox.tar.gz"]
  - [tar, -xzf, /opt/agentbox/agentbox.tar.gz, -C, /opt/agentbox/app]
  # Windows 打的 tar 包没有可执行位，不 chmod 的话所有 ./xxx.sh 都是 Permission denied
  - [bash, -lc, "find /opt/agentbox/app -type f -name '*.sh' -exec chmod 0755 {} +"]
  # 2. provision PostgreSQL + pgvector + the virtualenv + systemd units.
  #    The whole log is echoed to the serial console on failure, because the
  #    host can read the serial console but not the guest's filesystem.
  - [bash, -lc, "cd /opt/agentbox/app && REPO_DIR=/opt/agentbox/app PIP_INDEX_URL=@PIP_INDEX@ bash ./deploy/platform/install-platform.sh > /var/log/agentbox-install.log 2>&1; rc=$?; echo \"install-platform.sh exit=$rc\" > /dev/ttyS0; if [ $rc -ne 0 ]; then echo '=== install-platform.sh FAILED, last 80 lines ===' > /dev/ttyS0; tail -n 80 /var/log/agentbox-install.log > /dev/ttyS0; else touch /opt/agentbox/PROVISIONED; echo 'agentbox provisioning OK' > /dev/ttyS0; fi"]
  # 3. reveal the result on the serial console either way
  - [bash, -lc, "test -f /opt/agentbox/PROVISIONED && echo 'AGENTBOX_READY' > /dev/ttyS0 || echo 'AGENTBOX_NOT_READY' > /dev/ttyS0"]

final_message: "agentbox platform ready after $UPTIME seconds"
