# ---------------------------------------------------------------------------
# Debian 13 (trixie) preseed for the agentbox *platform* VM.
#
# This VM is the trusted side: it runs the AI service (FastAPI) and PostgreSQL +
# pgvector natively (no Docker).  The sandbox VMs it drives are built separately
# by deploy/sandbox/build-sandbox-image.sh.
#
# @MIRROR_HOST@ / @MIRROR_DIR@ / @SECURITY_HOST@ / @SECURITY_DIR@ are rendered by
# deploy/windows/new-platform-vm.ps1 (default: Tsinghua TUNA, because
# deb.debian.org is frequently unreachable from CN networks -- measured 0 KiB/s
# versus 19 MiB/s for TUNA on the same machine).
# ---------------------------------------------------------------------------

### localization
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
d-i console-setup/ask_detect boolean false

### network (QEMU user networking: DHCP gives 10.0.2.15, host is 10.0.2.2)
d-i netcfg/choose_interface select auto
d-i netcfg/get_hostname string agentbox-platform
d-i netcfg/get_domain string local

### mirror (rendered: fast local mirror, both archive and security)
d-i mirror/country string manual
d-i mirror/http/hostname string @MIRROR_HOST@
d-i mirror/http/directory string @MIRROR_DIR@
d-i mirror/http/proxy string
d-i apt-setup/security_host string @SECURITY_HOST@
d-i apt-setup/security_path string @SECURITY_DIR@
d-i apt-setup/use_mirror boolean true
d-i apt-setup/services-select multiselect security, updates

### clock / users
d-i time/zone string UTC
d-i clock-setup/utc boolean true
d-i clock-setup/ntp boolean true
d-i passwd/root-login boolean false
d-i passwd/user-fullname string agent
d-i passwd/username string agent
d-i passwd/user-password password agentbox
d-i passwd/user-password-again password agentbox
d-i user-setup/allow-password-weak boolean true
d-i user-setup/encrypt-home boolean false

### partitioning: one ext4 root on the single virtio disk, 2 GiB swap
d-i partman-auto/method string regular
d-i partman-auto/disk string /dev/vda
d-i partman-auto/choose_recipe select atomic
d-i partman-auto/expert_recipe string                         \
      agentbox ::                                             \
              2048 2048 2048 linux-swap                        \
                      $primary{ } method{ swap } format{ }      \
              .                                               \
              12000 12000 -1 ext4                              \
                      $primary{ } $bootable{ }                  \
                      method{ format } format{ }                \
                      use_filesystem{ } filesystem{ ext4 }      \
                      mountpoint{ / }                           \
              .
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true
d-i partman-md/confirm boolean true

### bootloader
d-i grub-installer/only_debian boolean true
d-i grub-installer/bootdev string /dev/vda

### packages: no desktop, we need ssh, python and postgresql
tasksel tasksel/first multiselect standard, ssh-server
d-i pkgsel/include string sudo curl ca-certificates python3 python3-venv python3-pip \
                          postgresql postgresql-contrib postgresql-17-pgvector git
d-i pkgsel/update-policy select none
popularity-contest popularity-contest/participate boolean false

### fetch the provisioning helper through the host-side HTTP server
d-i preseed/late_command string \
    in-target mkdir -p /opt/agentbox/app ; \
    in-target /bin/sh -c 'curl -fsS --retry 3 http://10.0.2.2:8899/agentbox.tar.gz -o /opt/agentbox/agentbox.tar.gz || true' ; \
    in-target /bin/sh -c 'tar -xzf /opt/agentbox/agentbox.tar.gz -C /opt/agentbox/app 2>/dev/null || true' ; \
    in-target /bin/sh -c 'find /opt/agentbox/app -type f -name "*.sh" -exec chmod 0755 {} + 2>/dev/null || true' ; \
    in-target /bin/sh -c 'test -x /opt/agentbox/app/deploy/platform/install-platform.sh && echo ok > /opt/agentbox/READY || true'

d-i finish-install/reboot_in_progress note
