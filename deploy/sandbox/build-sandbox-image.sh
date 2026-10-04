#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build the Debian-minimal QEMU sandbox image.
#
# Run this **inside the Debian platform VM** as root (it uses debootstrap, no
# Docker anywhere).  It produces, in $OUT:
#
#   vmlinuz                  guest kernel          (booted with -kernel)
#   initrd.img               guest initramfs       (mounts the virtio root)
#   rootfs.img               ext4, no journal, mounted read-only by the guest
#   workspace-blank.qcow2    512 MiB ext4 backing file for per-session overlays
#
# The guest has no bootloader, no systemd and no network stack: PID 1 is
# /sbin/agent-init (our own python init), and the only writable path is
# /workspace on the second virtio disk.
# ---------------------------------------------------------------------------
set -euo pipefail

# 任何未捕获的失败都打印行号：静默退出排查起来非常费劲（真实踩过）。
trap 'rc=$?; echo "!!! build failed at line ${LINENO} (exit ${rc})" >&2' ERR

SUITE="${SUITE:-trixie}"
# deb.debian.org measured 0 KiB/s (timeout) on the reference network while TUNA
# measured ~19 MiB/s; override with MIRROR=... if you have a faster one.
MIRROR="${MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/debian}"
ARCH="${ARCH:-amd64}"
OUT="${OUT:-/var/lib/agentbox/sandbox}"
ROOTFS_SIZE="${ROOTFS_SIZE:-1400M}"
# 4 GiB by default: builds, package installs and datasets need room.
WORKSPACE_SIZE="${WORKSPACE_SIZE:-4096M}"
SANDBOX_UID="${SANDBOX_UID:-1000}"
SANDBOX_GID="${SANDBOX_GID:-1000}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
GUEST_SRC="${REPO_DIR}/src/agent/sandbox"

if [[ "${EUID}" -ne 0 ]]; then
  echo "must run as root (debootstrap + chroot); try: sudo $0" >&2
  exit 1
fi
if [[ ! -d "${GUEST_SRC}" ]]; then
  echo "cannot find the guest sources at ${GUEST_SRC}" >&2
  exit 1
fi

# One build at a time, and clean up chroots left behind by killed builds.
exec 9>/var/lock/agentbox-build.lock 2>/dev/null || true
if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
  echo "another build is already running (lock /var/lock/agentbox-build.lock)" >&2
  exit 1
fi
for stale in /var/tmp/agentbox-build.*; do
  [[ -d "${stale}" ]] || continue
  mountpoint -q "${stale}/rootfs/proc" 2>/dev/null && continue
  echo "==> removing stale build directory ${stale}"
  rm -rf "${stale}"
done

BUILD="$(mktemp -d /var/tmp/agentbox-build.XXXXXX)"
ROOTFS="${BUILD}/rootfs"
cleanup() {
  for mnt in "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/dev/pts" "${ROOTFS}/dev"; do
    mountpoint -q "${mnt}" 2>/dev/null && umount -l "${mnt}" 2>/dev/null || true
  done
  rm -rf "${BUILD}"
}
trap cleanup EXIT

echo "==> installing build tooling"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends debootstrap e2fsprogs qemu-utils ca-certificates >/dev/null

echo "==> debootstrap ${SUITE}/${ARCH} into ${ROOTFS}"
# python3-minimal has a stripped stdlib (no ctypes/shutil/threading), which made
# PID 1 die with "No module named ctypes" and panic the kernel: use the full package.
INCLUDE="python3,passwd,kmod,initramfs-tools,linux-image-${ARCH},util-linux,coreutils,bash,\
procps,findutils,grep,sed,gawk,tar,gzip,xz-utils,file,diffutils,patch,less,nftables,iproute2,\
iputils-ping,ca-certificates"
# debootstrap has no download timeout: a single stalled connection hangs the
# whole build forever (seen in practice).  Bound each attempt and fall back.
DEBOOTSTRAP_TIMEOUT="${DEBOOTSTRAP_TIMEOUT:-900}"
MIRROR_CANDIDATES=("${MIRROR}" "http://mirrors.tuna.tsinghua.edu.cn/debian" \
  "http://mirrors.aliyun.com/debian" "http://deb.debian.org/debian")

# 只保留真的能应答的镜像：回退到一个不可达的镜像只会白等好几分钟。
MIRROR_LIVE=()
for candidate in "${MIRROR_CANDIDATES[@]}"; do
  if curl -fsS --max-time 8 -o /dev/null "${candidate}/dists/${SUITE}/Release"; then
    echo "==> mirror reachable: ${candidate}"
    MIRROR_LIVE+=("${candidate}")
  else
    echo "==> mirror unreachable, skipping: ${candidate}"
  fi
done
if [[ "${#MIRROR_LIVE[@]}" -eq 0 ]]; then
  echo "!!! no Debian mirror answered; check the network (tried: ${MIRROR_CANDIDATES[*]})" >&2
  exit 1
fi

bootstrapped=0
for candidate in "${MIRROR_LIVE[@]}"; do
  # 两次机会：实测的失败是"连接卡住"，重试同一个镜像通常就好了。
  for attempt in 1 2; do
    echo "==> debootstrap ${SUITE}/${ARCH} from ${candidate} (attempt ${attempt}, timeout ${DEBOOTSTRAP_TIMEOUT}s)"
    rm -rf "${ROOTFS}"
    if timeout "${DEBOOTSTRAP_TIMEOUT}" debootstrap --arch="${ARCH}" --variant=minbase \
         --include="${INCLUDE}" "${SUITE}" "${ROOTFS}" "${candidate}"; then
      bootstrapped=1
      break 2
    fi
    echo "!!! attempt ${attempt} from ${candidate} failed or timed out" >&2
  done
done
if [[ "${bootstrapped}" -ne 1 ]]; then
  echo "every mirror failed; check connectivity and re-run (the build is resumable from scratch)" >&2
  exit 1
fi

echo "==> mounting pseudo filesystems for chroot post-install"
mount -t proc proc "${ROOTFS}/proc"
mount -t sysfs sys "${ROOTFS}/sys"
mount --bind /dev "${ROOTFS}/dev"
cp -f /etc/resolv.conf "${ROOTFS}/etc/resolv.conf" 2>/dev/null || true

echo "==> creating the sandbox group and user (uid/gid ${SANDBOX_UID})"
# 必须先建组：useradd -g <gid> 要求该 GID 已存在，否则会失败（以前被 `|| true` 吞掉，
# 结果 /etc/passwd 里根本没有 sandbox 用户）。
chroot "${ROOTFS}" /usr/sbin/groupadd -g "${SANDBOX_GID}" sandbox 2>/dev/null || true
if ! chroot "${ROOTFS}" /usr/sbin/useradd -u "${SANDBOX_UID}" -g "${SANDBOX_GID}" \
       -d /workspace -s /bin/bash -M sandbox; then
  echo "!!! could not create the sandbox user; commands would run as a nameless uid" >&2
  exit 1
fi
if ! chroot "${ROOTFS}" getent passwd sandbox >/dev/null; then
  echo "!!! sandbox user is absent from /etc/passwd" >&2
  exit 1
fi
chroot "${ROOTFS}" id sandbox
install -d -o "${SANDBOX_UID}" -g "${SANDBOX_GID}" -m 0755 "${ROOTFS}/workspace"

echo "==> installing the guest agent code"
install -d "${ROOTFS}/usr/lib/agent/sandbox"
install -m 0644 "${GUEST_SRC}"/*.py "${ROOTFS}/usr/lib/agent/sandbox/"
cat > "${ROOTFS}/sbin/agent-init" <<'SHIM'
#!/bin/sh
# PID 1 for the sandbox: a small audited python init instead of systemd.
export PYTHONPATH=/usr/lib/agent
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 /usr/lib/agent/sandbox/init.py
SHIM
chmod 0755 "${ROOTFS}/sbin/agent-init"

echo "==> kernel modules for the virtio devices we boot with"
install -d "${ROOTFS}/etc/initramfs-tools"
cat > "${ROOTFS}/etc/initramfs-tools/modules" <<'MODULES'
# needed before the root filesystem is available / for the RPC channel
virtio_pci
virtio_ring
virtio_blk
virtio_console
virtio_rng
ext4
MODULES
sed -i 's/^MODULES=.*/MODULES=most/' "${ROOTFS}/etc/initramfs-tools/initramfs.conf" 2>/dev/null || true
cat > "${ROOTFS}/etc/initramfs-tools/conf.d/agentbox.conf" <<'CONF'
MODULES=most
COMPRESS=gzip
RESUME=none
CONF

echo "==> rebuilding the initramfs inside the chroot"
timeout 600 chroot "${ROOTFS}" update-initramfs -c -k all >/dev/null

echo "==> hardening guest configuration"
install -d "${ROOTFS}/etc/agent"
cat > "${ROOTFS}/etc/agent/nftables.conf" <<'NFT'
#!/usr/sbin/nft -f
# The VM has no NIC at all (-nic none); this ruleset exists so that even a
# hypothetical interface comes up closed.
flush ruleset
table inet agentbox {
  chain input { type filter hook input priority 0; policy drop; iif lo accept }
  chain output { type filter hook output priority 0; policy drop; oif lo accept }
  chain forward { type filter hook forward priority 0; policy drop; }
}
NFT
chmod 0644 "${ROOTFS}/etc/agent/nftables.conf"
cat > "${ROOTFS}/etc/agent/nftables-net.conf" <<'NFTNET'
#!/usr/sbin/nft -f
# Profile for AGENT_SANDBOX_NET_MODE=full: the operator asked this sandbox to have
# internet access, so egress is allowed -- but nothing may reach *in*: no host service,
# no other guest, not even the slirp gateway's own ports.  Replies to our own
# connections are still let through by the established/related rule.
flush ruleset
table inet agentbox {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
  }
  chain output { type filter hook output priority 0; policy accept; }
  chain forward { type filter hook forward priority 0; policy drop; }
}
NFTNET
chmod 0644 "${ROOTFS}/etc/agent/nftables-net.conf"
# /etc is part of the read-only root, so the resolver config lives on the /run tmpfs and
# /etc/resolv.conf is only a symlink to it (the guest fills it in when a NIC is present).
install -d "${ROOTFS}/run/agent"
ln -sfn /run/agent/resolv.conf "${ROOTFS}/etc/resolv.conf"
# a usable apt source: the operator can `apt-get update && apt-get install ...` when the
# sandbox network switch is on
cat > "${ROOTFS}/etc/apt/sources.list" <<'SOURCES'
# everything through one mirror: security.debian.org answers over https with a redirect
# that plain http apt cannot follow ("does not have a Release file")
deb http://mirrors.tuna.tsinghua.edu.cn/debian trixie main
deb http://mirrors.tuna.tsinghua.edu.cn/debian trixie-updates main
deb http://mirrors.tuna.tsinghua.edu.cn/debian-security trixie-security main
SOURCES
cat > "${ROOTFS}/etc/hostname" <<'HOST'
sandbox
HOST
# Read-only root: everything writable lives on tmpfs or /workspace.
cat > "${ROOTFS}/etc/fstab" <<'FSTAB'
# / is mounted read-only by the kernel (rootflags=ro) and re-asserted by agent-init.
/dev/vdb  /workspace  ext4  rw,nosuid,nodev  0 0
tmpfs     /tmp        tmpfs rw,nosuid,nodev,size=128m,mode=1777 0 0
tmpfs     /var/tmp    tmpfs rw,nosuid,nodev,size=64m,mode=1777  0 0
tmpfs     /var/log    tmpfs rw,nosuid,nodev,size=16m,mode=0755  0 0
FSTAB
# Keep python from trying to write bytecode on a read-only root.
install -d "${ROOTFS}/etc/profile.d"
echo 'export PYTHONDONTWRITEBYTECODE=1' > "${ROOTFS}/etc/profile.d/agentbox.sh"

echo "==> smoke-testing the guest python (stdlib + our modules)"
# 这些检查能在镜像被启动之前抓出"python 太精简"这类错误（曾经导致 PID 1 直接 panic）。
if ! chroot "${ROOTFS}" /usr/bin/python3 - <<'PY'
import sys
missing = []
for name in ("ctypes", "shutil", "threading", "concurrent.futures", "socket", "select",
             "subprocess", "resource", "pwd", "grp", "hashlib", "hmac", "base64", "ast",
             "signal", "uuid", "platform", "stat", "errno", "glob", "importlib.util",
             "dataclasses", "typing", "json", "re", "os", "sys", "time", "tempfile"):
    try:
        __import__(name)
    except Exception as exc:
        missing.append(f"{name}: {exc}")
if missing:
    print("MISSING STDLIB MODULES:")
    for item in missing:
        print("  ", item)
    sys.exit(1)
print("  stdlib ok:", sys.version.split()[0])
PY
then
  echo "!!! guest python is missing stdlib modules (install the full 'python3' package)" >&2
  exit 1
fi

# 用与真实运行完全相同的方式导入：guest 里是 `python3 /usr/lib/agent/sandbox/xxx.py`
# （脚本目录自动进 sys.path），所以这里导入同级模块，而不是 sandbox.* 包路径。
if ! chroot "${ROOTFS}" /usr/bin/python3 - <<'PY'
import sys
sys.path.insert(0, "/usr/lib/agent/sandbox")
import checker, limits, policy, handlers, executor, runner, init
print("  guest modules import cleanly")
PY
then
  echo "!!! guest modules failed to import -- the image would not boot (PID 1 would die)" >&2
  exit 1
fi

if ! chroot "${ROOTFS}" getent passwd sandbox >/dev/null; then
  echo "!!! the sandbox user is missing -- commands would run as a nameless uid" >&2
  exit 1
fi
echo "  sandbox user: $(chroot "${ROOTFS}" id sandbox)"

echo "==> unmounting pseudo filesystems"
umount -l "${ROOTFS}/dev" 2>/dev/null || true
umount -l "${ROOTFS}/sys" 2>/dev/null || true
umount -l "${ROOTFS}/proc" 2>/dev/null || true
# the runtime resolver is a symlink into /run (see above)

KERNEL="$(ls -1 "${ROOTFS}"/boot/vmlinuz-* | sort -V | tail -n1)"
INITRD="$(ls -1 "${ROOTFS}"/boot/initrd.img-* | sort -V | tail -n1)"
[[ -n "${KERNEL}" && -n "${INITRD}" ]] || { echo "kernel or initramfs missing in the chroot" >&2; exit 1; }

echo "==> building the read-only root filesystem (ext4, journalless)"
install -d "${OUT}"
rm -f "${OUT}/rootfs.img"
# No journal: a journalled ext4 cannot be mounted read-only while dirty.
mkfs.ext4 -q -O ^has_journal -L agentbox-root -d "${ROOTFS}" -F "${OUT}/rootfs.img" "${ROOTFS_SIZE}"

echo "==> building the blank workspace filesystem"
WS_DIR="${BUILD}/workspace"
install -d -o "${SANDBOX_UID}" -g "${SANDBOX_GID}" -m 0755 "${WS_DIR}"
install -d -o "${SANDBOX_UID}" -g "${SANDBOX_GID}" -m 0755 "${WS_DIR}/.tools"
rm -f "${BUILD}/workspace.raw" "${OUT}/workspace-blank.qcow2"
mkfs.ext4 -q -O ^has_journal -L agentbox-ws -d "${WS_DIR}" -F "${BUILD}/workspace.raw" "${WORKSPACE_SIZE}"
qemu-img convert -f raw -O qcow2 "${BUILD}/workspace.raw" "${OUT}/workspace-blank.qcow2"

echo "==> copying kernel and initramfs"
cp -f "${KERNEL}" "${OUT}/vmlinuz"
cp -f "${INITRD}" "${OUT}/initrd.img"

echo "==> verifying the produced image"
python3 - "$OUT" <<'PY'
import sys
from pathlib import Path
out = Path(sys.argv[1])
required = ["vmlinuz", "initrd.img", "rootfs.img", "workspace-blank.qcow2"]
missing = [name for name in required if not (out / name).is_file()]
for name in required:
    path = out / name
    if path.is_file():
        print(f"  {name:24s} {path.stat().st_size / 1048576:8.1f} MiB")
if missing:
    print(f"missing: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)
PY

cat <<EOF

sandbox image ready in ${OUT}
  kernel    : $(basename "${KERNEL}")
  initramfs : $(basename "${INITRD}")

next (the control plane runs on the Windows host and needs these four files):
  1. serve them from this VM (the host reaches the VM through port forwarding):
       cd ${OUT} && nohup python3 -m http.server 8099 --bind 0.0.0.0 >/tmp/seed-http.log 2>&1 &
  2. on the Windows host, pull them into the repository:
       powershell -ExecutionPolicy Bypass -File .\\deploy\\windows\\fetch-sandbox-image.ps1
  3. python -m agent.cli image verify                     # on the Windows host
  4. python -m agent.cli serve control                    # on the Windows host
  5. python -m agent.cli sandbox status                   # should show a warm VM
EOF
