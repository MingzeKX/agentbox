# agentbox 部署手册（DEPLOY）

**读者**：① 在一台全新的 Windows 机器上把它装起来的人；② 必须知道"什么跑在哪台机器上"的维护者。

**定位**：这是**权威部署文档**。技术原理看 [`README.md`](README.md)，日常操作看 [`USAGE.md`](USAGE.md)，
v1 范围与已知限制看 [`RELEASE-v1.md`](RELEASE-v1.md)——本文不整段复制它们（见 §9）。

> 约定：本文里凡是"实测/脚本里能读到"的值都附了文件路径；不确定的一律写成 `（以脚本为准：<path>）`，不猜。

---

## 1. 三台机器一张图

actor 是 **Windows 宿主**，它同时是**控制平面**；它用 QEMU 养两台完全不同的来宾 VM。

```text
┌──────────────────────────────── Windows 宿主（控制平面 · 唯一拥有 QEMU 的机器）──────────────┐
│                                                                                             │
│  agent.cli serve control  :8091        进程由 start-agent.ps1 拉起（deploy/windows/start-agent.ps1）│
│  QEMU 可执行文件 qemu\      owns      →  spawn 全部 VM（src/agent/control/vm.py）             │
│  宿主文件系统               owns      ←  只有控制平面/CLI 能碰；AI 服务碰不到（§8）              │
│                                                                                             │
│   ┌── hostfwd（仅宿主回环 127.0.0.1）────────┐   ┌── 无 hostfwd、无共享目录 ──────────┐        │
│   │ 8090→VM:8090  AI 服务                  │   │ （串口 virtio-serial 是唯一通道）  │        │
│   │ 8099→VM:8099  镜像分发 http.server      │   │                                    │        │
│   │ 2222→VM:22    SSH                      │   │                                    │        │
│   │ 8091→VM:8091  预留（见 §3 警告）        │   │                                    │        │
│   │ +AGENT_PLATFORM_PORTS 额外转发          │   │                                    │        │
│   └────────────────────────────────────────┘   └────────────────────────────────────┘        │
│            ↓ ssh -i var\vm_key -p 2222                     ↓ agent sandbox invoke/console      │
│  ┌──────────▼──────────────────────┐              ┌────────▼─────────────────────────────┐   │
│  │ 平台 VM（Debian 13 / cloud-init）│              │ 沙箱 VM（每会话一个，池化）           │   │
│  │  systemd: agentbox-ai  :8090    │              │  自研 PID 1 = agent-init              │   │
│  │  PostgreSQL 17+pgvector :5432   │              │  rootfs 只读（rootflags=ro 再 remount）│   │
│  │  模型在 /opt/agentbox/models    │              │  非 root：工具以 uid 1000 sandbox 运行 │   │
│  │  python -m http.server 8099     │              │  无网卡（net_mode=off）→ 无网络入口    │   │
│  │  sshd :22                       │              │  唯一可写：/workspace（vdb 独立盘）    │   │
│  └─────────────────────────────────┘              └───────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

三条必须记牢的所有权边界（每条都有守卫测试钉死）：

| 问题 | 答案 | 依据 |
|---|---|---|
| **谁拥有 QEMU？** | **宿主 ✓**。全仓库只有控制平面 `src/agent/control/vm.py` 出现 `create_subprocess_exec`；守卫测试断言其他包没有进程创建调用 | README.md:51、`tests/unit/test_isolation_guard.py` |
| **谁不能碰宿主文件？** | **平台 VM 里的 AI 服务 ✓（有隔离守卫测试 ✓）**。它的 systemd 单元把 `ProtectSystem=strict`、`ProtectHome=true`、`CapabilityBoundingSet=`（清空）全开上；不变量 **I1** 由 `tests/unit/test_isolation_guard.py` 用 AST 扫描 `ai/ registry/ toolsmith/ models/` 钉死：出现 `subprocess/socket/ctypes/shutil` 导入、`open()`、`Path.read_text()`、`agent.control.*` 导入即测试失败 | `deploy/systemd/agentbox-ai.service`；README.md:50（I1）；`tests/unit/test_isolation_guard.py` |
| **沙箱 ↔ 宿主之间有共享目录 / hostfwd 吗？** | **都没有 ✓（这是隔离设计，不是漏配）**。沙箱那台 `-nic` 上从来不挂 `hostfwd`；`stop-agent.ps1` 靠"命令行里有没有 `hostfwd`"来区分平台 VM 与沙箱 VM | `deploy/windows/stop-agent.ps1:97,101,146`；沙箱侧 `src/agent/control/vm.py:207-213` 只按 `net_mode` 决定是否给网卡 |

> 沙箱拿不到宿主文件、也没有网络入口，所以"模型现写的代码"既发不出去也进不来：想取文件只能走受控通道（§3.5）。

---

## 2. 各组件清单（进程名 / 端口 / 机器 / 日志 / 重启）

### 2.1 Windows 宿主 / 控制平面

| 组件 | 进程与命令 | 监听 | 日志 | 怎么重启 |
|---|---|---|---|---|
| 控制平面（FastAPI） | `agent.cli serve control`，由 `.\.venv\Scripts\python.exe -m agent.cli serve control` 启动 | `127.0.0.1:8091`（端口来自 `AGENT_CONTROL_PORT`，`src/agent/config.py:81`） | `var\logs\start-*.log`（`start-agent.ps1` 每步耗时表写在里面，README.md:314） | `.\stop-agent.cmd` → `.\start-agent.cmd`；只重启这一项：`.\deploy\windows\stop-agent.ps1` 再单跑 `agent.cli serve control`（README.md:345） |
| 启动 | `start-agent.ps1`（7 步）+ 根目录 `start-agent.cmd` 包装 | — | `var\logs\start-<stamp>.log`（同目录已有大量 `start-*.log` 实例） | 直接重跑 `.\start-agent.cmd` |
| 停止 | `stop-agent.ps1` + 包装 `stop-agent.cmd` | — | `var\logs\stop-*.log` | — |
| QEMU（宿主进程） | `qemu-system-x86_64.exe`，由控制平面 spawn | 平台 VM 的 hostfwd 绑 `127.0.0.1` | 平台 VM：`var\platform\console.log`；沙箱 VM：`var\sandbox\console\vm-*.log` | 停控制平面会回收沙箱；平台 VM 用 `stop-agent.ps1` / `run-platform-vm.ps1` |

包装脚本都只是 `powershell -NoProfile -ExecutionPolicy Bypass -File ...`（见 `start-agent.cmd`、`stop-agent.cmd`），
所以从 `cmd.exe`、PowerShell、资源管理器双击都能用。

### 2.2 平台 VM（Debian）

| 组件 | 进程 | 监听 | 日志 | 怎么重启 |
|---|---|---|---|---|
| AI 服务 | `agentbox-ai`（systemd，`ExecStart=.../python -m agent.cli serve ai`，`User=agent`） | 来宾 `0.0.0.0:8090` → 宿主 `127.0.0.1:8090`（经 hostfwd） | `journalctl -u agentbox-ai`；来宾内 `/opt/agentbox/app/var/...` | 来宾内 `sudo systemctl restart agentbox-ai`；`push-repo-to-vm.ps1 -RestartAi` 顺带重启（README.md:325） |
| PostgreSQL 17 + pgvector | `postgresql`（systemd；AI 服务 `Requires=postgresql.service`） | **来宾内 `127.0.0.1:5432`，不对外 ✗**（DSN 就是 `@127.0.0.1:5432`，`.env.example`） | `journalctl -u postgresql` | `sudo systemctl restart postgresql` |
| 镜像分发 | `python3 -m http.server 8099 --bind 0.0.0.0` | 来宾 `:8099` → 宿主 `127.0.0.1:8099`（hostfwd） | `/tmp/seed-http.log`（`deploy/windows/fetch-sandbox-image.ps1:12,66`） | `start-agent.ps1` 第 4 步会在缺镜像时把它拉起来（`deploy/windows/start-agent.ps1:318-328`） |
| sshd | `sshd`（cloud-init 里 `ssh_pwauth: true` + 装 `var\vm_key.pub`） | 来宾 `:22` → 宿主 `127.0.0.1:2222` | `journalctl -u ssh` | `sudo systemctl restart ssh` |
| 控制平面副本（可选） | `deploy/systemd/agentbox-control.service`（`User=root`，为 cgroup v2） | — | — | **本仓库的实际部署里控制平面跑在 Windows 宿主**，这个单元是给"控制平面也放进 Linux"的可选形态（§9） |

### 2.3 沙箱 VM

| 项 | 事实 |
|---|---|
| 网络入口 | **没有**。`net_mode=off`（默认）时**根本不挂网卡**；`full` 也只给一张 slirp 用户态网卡，**只出网、不进来**（`.env.example` 的 `AGENT_SANDBOX_NET_MODE` 段、`src/agent/control/vm.py:207-213`） |
| 唯一入口 | virtio-serial 上的 JSON-RPC（自研 PID 1 `agent-init`）。没有 getty、没有 sshd（README.md:359） |
| 串口日志 | `var\sandbox\console\vm-<id>.log`（来宾串口）；同目录 `vm-<id>.qemu.log` 是 QEMU 自己的 stderr（README.md:673） |
| 会话盘 | `var\sandbox\sessions\<session>\*.qcow2`（每个会话一份 overlay；`workspace_key` → `<session>/workspace.qcow2`，`src/agent/control/vm.py:158-162`；`manager.py:158` 用同一个路径判断会话是否已有盘） |
| 基线镜像 | `var\sandbox\` 下 4 件套：`vmlinuz`、`initrd.img`、`rootfs.img`、`workspace-blank.qcow2`（`deploy/windows/start-agent.ps1:156` 校验这 4 个名字） |
| 规格 | `AGENT_SANDBOX_VM_MEMORY_MB` / `AGENT_SANDBOX_VM_CPUS` / `AGENT_SANDBOX_POOL_SIZE` / `AGENT_SANDBOX_MAX_VMS`（`src/agent/config.py:160-163`） |

---

## 3. 怎么连（逐条可复制）

先 `cd` 到仓库根（下面所有相对路径都相对仓库根）：

```powershell
cd C:\Users\86133\Desktop\Agent
```

### 3.1 宿主 → 平台 VM（SSH）

```powershell
ssh -i var\vm_key -p 2222 agent@127.0.0.1
```

- 密钥由 provisioning 生成在 `var\vm_key`（`deploy/windows/provision-cloud-vm.ps1:105` 调 `ssh-keygen -t ed25519`），公钥被写进来宾的 `ssh_authorized_keys`（`deploy/windows/cloud-init/user-data.tpl:24-25`）。
- 图形/救援登录：`agent` / `agentbox`（默认口令，`deploy/windows/provision-cloud-vm.ps1:33`；`-Password` 可改）——**这是来宾口令，不是 API key**。
- hostfwd 是 `127.0.0.1:2222-:22`（`deploy/windows/run-platform-vm.ps1:115`），所以只能从本机连。

### 3.2 平台 VM 控制台（救砖：sshd 不响应时）

```powershell
.\deploy\windows\vm-console.cmd            # 根目录还有 vm-console.cmd 包装
```

- 走 TCP 串口，默认 **COM 端口 8906**（`deploy/windows/vm-console.ps1:24-25`）。
- 前提：平台 VM 是带串口启动的：
  ```powershell
  .\deploy\windows\run-platform-vm.ps1 -Headless -SerialTcp 8906
  ```
  （`deploy/windows/vm-console.ps1:8-9`）
- 串口会回显输入，**别用"回显即成功"判断登录**；脚本已用 printf 输出不同字符串来探测真实 shell（README.md:685）。
- 非交互日志：`var\platform\console.log`（`run-platform-vm.ps1:155`）。

### 3.3 宿主 → 控制平面 API

```powershell
curl.exe http://127.0.0.1:8091/health
```

受保护接口带 `X-Agent-Token`；值就是 `.env` 里的 `AGENT_CONTROL_SECRET`（不要把它打印到终端历史里）：

```powershell
$s = (Select-String -Path .env -Pattern '^AGENT_CONTROL_SECRET=(.+)$').Matches[0].Groups[1].Value
curl.exe -H "X-Agent-Token: $s" http://127.0.0.1:8091/sandbox/status
```

- 接口清单：`GET /health`、`GET /sandbox/status`、`POST /sessions/{id}/release`、`POST /rpc`（`src/agent/control/app.py:76-99`）。
- `AGENT_CONTROL_SECRET` 由 `install.ps1` 生成随机值（README.md:237），`start-agent.ps1:151-152` 会读它、为空就直接失败。
- 客户端（CLI/控制台）直接从仓库根 `.env` 读同一个值（`src/agent/config.py:66` 的 `env_file`），所以一般不用手工导出。
- `start-agent.ps1:169-177` 会检查 8091 是否被 QEMU 的 hostfwd 抢走（见 3.6）。

### 3.4 宿主 → AI 服务（经 hostfwd 到平台 VM ✓）

```powershell
curl.exe http://127.0.0.1:8090/health
```

- hostfwd 三条固定转发：`8090`、`8099`、`$SshPort`（`deploy/windows/run-platform-vm.ps1:113-115`）。
- **注意方向**：AI 服务要访问宿主时走 slirp 的 `10.0.2.2:8091`（`AGENT_CONTROL_URL`，`.env.example`），**不需要**任何 hostfwd。

### 3.5 宿主 → 沙箱（**没有 SSH，只有受控通道**）

```powershell
# 控制台里的命令（AI 生成的文件/图片取回本机 var\pulled\）
/get /workspace/plot.png
/get /workspace/out.csv report/a.csv

# CLI（读写控制平面的池/VM）
.\.venv\Scripts\python.exe -m agent.cli sandbox status
.\.venv\Scripts\python.exe -m agent.cli sandbox invoke fs.read --session default --params "{\"path\":\"/workspace/plot.png\"}"
.\.venv\Scripts\python.exe -m agent.cli sandbox console <vm-id>
```

- `sandbox` 的子命令只有 `status | reset | stop | start | metrics | console <vm> | invoke <method>`（`src/agent/cli/main.py:898-910`）。
- `/get` 只允许 `/workspace` 下的文件，单文件上限 8 MB（`AGENT_FS_PULL_MAX_BYTES`），落点只能是 `var\pulled\` 下的相对路径，同名拒绝、`--overwrite` 才覆盖（USAGE.md:138-146）。
- `sandbox status` 里看 `warm=` 那个字段判断有没有热 VM（`src/agent/control/manager.py:328,353`）。
- 沙箱里没有 shell 可登；要看内部只能 `sandbox console <vm-id>` 或读 `var\sandbox\console\vm-<id>.log`。

### 3.6 端口转发（FTP / SMB / Web 之类）

```dotenv
# 仓库根 .env
AGENT_PLATFORM_PORTS=2121,30000-30010,8080,8445
```

```powershell
# 改完 .env 必须重启平台 VM 才生效
.\stop-agent.cmd
.\start-agent.cmd
```

- 只接受"逗号分隔的单个端口或闭区间（从左到右升序）"，`1-65535`（`deploy/windows/run-platform-vm.ps1:48-91`）。
- **只绑 `127.0.0.1` ✓**：每一条都生成为 `hostfwd=tcp:127.0.0.1:<port>-:<port>`（`run-platform-vm.ps1:101`）；写绑定地址（`0.0.0.0`/局域网 IP）会被拒（`:61-62`）。
- **445 / 139 会被拒 ✓**（Windows 宿主自己占着 SMB）；报错信息会直接建议换 `8445`（`run-platform-vm.ps1:84-85`；测试 `tests/unit/test_platform_ports.py:60-62` 覆盖 `445`、`139`、以及"只碰到 445 的区间"）。
- 这只影响平台 VM。**沙箱 VM 的隔离没有任何变化**（USAGE.md:106）。

---

## 4. 全新机器部署

### 4.1 前置条件

| 项 | 要求 | 依据 |
|---|---|---|
| 系统 | Windows x64（脚本用 Windows PowerShell 5.1，不要用 `pwsh`） | README.md:665、`deploy/windows/*.ps1` |
| Python | **版本必须与 Wheelhouse 匹配**。本仓库现有 `packaging\wheelhouse\` 是在 **Python 3.14 / win_amd64** 上下载的（wheel 标签 `cp314`）→ 目标机装 **3.14 x64**，否则这批 wheel 装不上 | `packaging/README.md` 的"wheelhouse 和 Python 版本的耦合"一节；实测 `packaging\wheelhouse\` 里 `asyncpg-0.31.0-cp314-cp314-win_amd64.whl` 等 10 个 `cp314` wheel |
| 解释器路径 | 安装时勾 *py launcher*，验证 `py -3.14 -V`。**PATH 上的 `python.exe` 可能是 Microsoft Store 别名**（执行返回 9009） | `packaging/README.md`；`packaging/install.ps1:113` |
| 虚拟化 | **WHPX 需先启用并重启** | `deploy/windows/probe-whpx.ps1`；`.env.example` 里 `AGENT_SANDBOX_ACCEL=auto` 的注释（WHPX 下 guest 不能 `host/max`，否则 `Unexpected VP exit code 4`） |
| 磁盘 | **≈20 GB 起**（平台 VM + 沙箱镜像 + wheelhouse；`-Fat` 包则是 ~13 GB 的包） | `packaging/README.md` 的"每块东西到底多大"表 |
| 网络 | 安装期**能联网**（首次装 QEMU、下 Debian 云镜像、在 VM 内构建沙箱镜像、下模型） | `packaging/README.md` 的"离线覆盖不到什么"表；`packaging/setup-agentbox.ps1` 的联网预检 |
| SSH 客户端 | `ssh.exe`（`start-agent.ps1:85` 找 `%WINDIR%\System32\OpenSSH\ssh.exe`） | `deploy/windows/start-agent.ps1:85` |
| QEMU | **必须自己装**：`-Slim` 包里**不含** `qemu\`（GPLv2 第三方程序，不再随包分发）。官方下载页 <https://www.qemu.org/download/#windows>，安装器默认装到 `C:\Program Files\qemu`；再用 `.env` 的 `AGENT_QEMU_DIR` 或系统 PATH 指给脚本 | `packaging/README.md` 的"QEMU 从哪来"；`packaging/setup-agentbox.ps1` 的 `Resolve-Qemu` |

#### QEMU 装到哪、脚本怎么找到它

1. **自己装 QEMU**（`-Slim` 包不带，也不再随包分发）：官方下载页
   <https://www.qemu.org/download/#windows>，安装器默认装到 `C:\Program Files\qemu`。
2. **让脚本找到它，二选一**（两种都实测过）：
   * **推荐：不改 PATH** —— 在仓库根 `.env` 里写一行
     `AGENT_QEMU_DIR=C:\Program Files\qemu`（脚本优先用它，不污染系统）；
   * 或者把 `C:\Program Files\qemu` 加进**系统 PATH**：之后**新开的**终端里
     `qemu-system-x86_64.exe --version` 能出版本号即可。
3. **怎么自测**（照抄一条就能判断配好没有）：

   ```powershell
   qemu-system-x86_64.exe --version                          # PATH 方式
   & "$env:AGENT_QEMU_DIR\qemu-system-x86_64.exe" --version  # 或直接指定目录
   ```

4. **常见坑**：
   * 装好了但**没进 PATH** → 预检/`start-agent` 报"找不到 QEMU" → 用 `AGENT_QEMU_DIR` 一行解决；
   * `.env` 里的路径**不要加引号**（写 `AGENT_QEMU_DIR=C:\Program Files\qemu`，别写 `"C:\Program Files\qemu"`）；
   * 装到别处（如 `D:\qemu`）同理，填那个目录即可；
   * `qemu-img.exe` 必须和 `qemu-system-x86_64.exe` 在**同一个目录**（官方安装包自带）。
5. 我们**开发机**上的 `<仓库>\qemu\` 目录是历史遗留（`.gitignore` 排除，与 `-Slim` 包无关）。
   查找顺序三处一致（`setup-agentbox.ps1` / `install.ps1` / `src/agent/config.py:341`）：
   `AGENT_QEMU_DIR` → `<仓库>\qemu\` → PATH。一个都没有时脚本**只打印安装指引**（含下载页和
   `AGENT_QEMU_DIR` 选项），不会抛栈。

### 4.2 解压 → 一键安装

```powershell
# ① 源机器（能上网）打包：.\packaging\make-bundle.ps1 -Slim      （README: packaging/README.md）
# ② 把 dist\agentbox-<版本>-<stamp>-slim.zip 拷到目标机，解压：
Expand-Archive .\agentbox-0.1.0-<stamp>-slim.zip -DestinationPath C:\agentbox
cd C:\agentbox

# ③ 目标机先装 QEMU，再在 .env 里写 AGENT_QEMU_DIR=C:\Program Files\qemu（见 4.1）

# ④ 先体检（不改任何东西），再一键装
powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Check
powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Yes
```

第一次跑（不带 `-Check`）会**在平台 VM 里现建沙箱镜像**（`-Slim` 包里没有）：
`push-repo-to-vm.ps1` → VM 内 `deploy/sandbox/build-sandbox-image.sh` → VM 内 8099 静态服务 →
`fetch-sandbox-image.ps1` 拉回 `var\sandbox\`。**需要联网，约 5-10 分钟**（debootstrap + apt + pip）。

#### 允许哪个脚本跑（Windows 执行策略与防火墙）

1. **不需要改系统设置（推荐）**：用仓库里的 **`.cmd` 包装** —— `start-agent.cmd`、`stop-agent.cmd`、
   `fetch-sandbox-image.cmd`、`push-repo-to-vm.cmd`、`vm-console.cmd`、`run-platform-vm.cmd`
   （它们只在**本次进程**里加 `-ExecutionPolicy Bypass`）。直接敲名字或双击都行。
2. 或者**显式调用**：`powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\xxx.ps1`。
3. 或者**一次性放行**（当前用户，不需要管理员，也不是必须）：
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。
   注意：从压缩包解出来的文件可能带"来自网络"标记，先 `Get-ChildItem -Recurse | Unblock-File`。
4. **防火墙**：首次启动会弹"是否允许 `qemu-system-x86_64.exe` 访问网络" → **允许**
   （沙箱/WHPX 需要）；拒绝只影响沙箱出网，不影响控制平面。
5. **别用 `-?` 去验证脚本**：有的脚本会把 `-?` 当参数执行而不是打帮助（我们踩过）。
   看用法请直接打开 `.ps1` 开头的注释块（`.SYNOPSIS` / `.EXAMPLE`），或不带参数直接跑。

`packaging\setup-agentbox.ps1` 的 8 步（每步幂等，可反复跑）：

| 步 | 做什么 |
|---|---|
| 1 | **预检**：Windows / PowerShell 5.1 / Python / 磁盘 / **QEMU** / **联网** / `var\sandbox\*` / `ssh.exe` / `wheelhouse`，每项给 `✓`/`✗` + 中文修复建议（`-SkipChecks` 可跳） |
| 2 | **`.venv` + 依赖**：委托 `packaging\install.ps1`；有 `wheelhouse\` 就 `pip install --no-index --find-links wheelhouse -e .`（**全离线**） |
| 3 | **`.env`**：生成随机 `AGENT_CONTROL_SECRET`、留 `AGENT_LLM_API_KEY=` 占位，并**强制写入 `AGENT_SANDBOX_CPU=Nehalem`**（不写的话 WHPX 下 guest 用老 CPU 型号，`numpy` 等 x86-64-v2 wheel 会拒绝加载） |
| 4 | **平台 VM**：已有 qcow2 + `var\vm_key` 就跳过；否则 `fetch-platform-image.ps1` →（有 ISO 且 `-UseIso` 时 `new-platform-vm.ps1`，否则 `provision-cloud-vm.ps1`）→ `run-platform-vm.ps1 -Headless` → 轮询来宾内 `/opt/agentbox/PROVISIONED`；失败会打印 `/var/log/agentbox-install.log` 最后 40 行 |
| 5 | **模型**：来宾内下 `BAAI/bge-m3` + faster-whisper（`HF_HOME=/opt/agentbox/models`、`HF_ENDPOINT=https://hf-mirror.com`），再重启 `agentbox-ai`（`-SkipModels` 可跳） |
| 6 | **沙箱镜像**：`var\sandbox\` 没有就**自动在平台 VM 里现建**（联网 5-10 分钟，用构建脚本结尾那套 HTTP 发布 + 宿主拉取），有 4 个文件就直接用 |
| 7 | **启动 + 自检**：`start-agent.ps1 -NoChat` → `:8091/health`、`:8090/health` → `agent.cli sandbox status` 看 `warm=` |
| 8 | **结论**：成功/失败清单 + `已就绪` / `未就绪`（未就绪时退出码 1） |

只想要 Windows 侧（不碰 VM、不下模型）：`setup-agentbox.ps1 -SkipVm -SkipModels -Yes`。
其它开关：`-SkipChecks`、`-UseIso`、`-VmTimeoutMinutes`、`-ModelTimeoutMinutes`、`-StartTimeoutMinutes`。
（以上 8 步与开关都以 `packaging\setup-agentbox.ps1` 与 `packaging\README.md` 为准。）

### 4.3 填 API key → 启动

```powershell
# ④ 唯一必须手工做的一步：把 DeepSeek key 填进仓库根 .env
notepad .env        # AGENT_LLM_API_KEY=sk-...      （绝不提交、绝不贴出来）

# ⑤ 启动（`-Slim` 包里没有沙箱镜像：`setup-agentbox.ps1` 第 6 步已在平台 VM 里建好并拉回 var\sandbox\）
.\start-agent.cmd
```

- `.\start-agent.cmd` 就是 `start-agent.ps1` 的包装（见该 `.cmd` 内容）。
- 沙箱镜像 `-Slim` 包里**不带**（里面是 Debian 组件，见 `THIRD-PARTY.md`）：**第一次**跑
  `setup-agentbox.ps1`（不带 `-Check`）会自动在平台 VM 里用
  `sudo /opt/agentbox/app/deploy/sandbox/build-sandbox-image.sh` 现建（产出
  `vmlinuz / initrd.img / rootfs.img / workspace-blank.qcow2`），再用
  `deploy\windows\fetch-sandbox-image.ps1` 拉回宿主 `var\sandbox\`。
- 首次构建**需要联网，约 5-10 分钟**（debootstrap + apt + pip，走 Debian 镜像源）；
  构建脚本带 `timeout` + 三镜像回退（README.md:686）。手动重跑就是上面三条命令。
- 结论性成功标志：`/opt/agentbox/PROVISIONED` 存在、开始对话前 `agent sandbox status` 里 `warm=` 有值。

---

## 5. 必须知道的环境变量

`agent.config.Settings` 从**仓库根 `.env`**（或同名环境变量）读取，前缀 `AGENT_`（`src/agent/config.py:61-67`）。

| 变量 | 默认值 | 作用 | 依据 |
|---|---|---|---|
| `AGENT_LLM_API_KEY` | `sk-replace-me`（占位，**必须改**） | LLM 网关密钥。**唯一必须手工填的一项** | `.env.example`；`packaging/README.md` |
| `AGENT_LLM_MODEL` | `deepseek-chat` | 主模型名 | `.env.example` |
| `AGENT_QEMU_DIR` | 空（= 自动找） | QEMU 安装目录。**`-Slim` 包不带 QEMU**，推荐在这里写安装目录（如 `C:\Program Files\qemu`，**不要加引号**）；查找顺序 `AGENT_QEMU_DIR` → `<仓库>\qemu\` → PATH | `packaging/setup-agentbox.ps1` 的 `Resolve-Qemu`；`src/agent/config.py:326-353`；本文 4.1 的"QEMU 装到哪" |
| `AGENT_SANDBOX_CPU` | 空 = 自动（whpx→`qemu64`） | CPU 型号。**`setup-agentbox.ps1` 会强制写成 `Nehalem`**：WHPX 下不能用 `host/max`，且 guest 里的 numpy 等 wheel 需要 SSE4.2 | `packaging/setup-agentbox.ps1` 的 `Invoke-EnvStep`；`.env.example` |
| `AGENT_SANDBOX_NET_MODE` | `off` | `off` = 沙箱**不加网卡**；`full` = 加一张 slirp 网卡，**只出网、不进来** | `.env.example`；`src/agent/control/vm.py:207-213` |
| `AGENT_SANDBOX_WORKSPACE_MB` | 代码默认 `4096`；**构建期脚本默认 `20480`（=20G）** | `/workspace` 盘大小。**开机时自动扩容到磁盘实际大小 ✓**（`agent-init` 里 best-effort 调 `resize2fs`；失败绝不影响启动） | 代码默认 `src/agent/config.py:166`；构建默认 `deploy/sandbox/build-sandbox-image.sh:34`（`WORKSPACE_SIZE_DEFAULT="20480M"`，`.env` 里有值就优先用 `.env`，裸数字按 MB 解释）；扩容 `src/agent/sandbox/init.py:188-216` |
| `AGENT_TOOL_IMPORT_PROFILE` | `strict` | 自写工具能 import 什么：`strict`（默认）→ `extended`（常用脚本集）→ `unrestricted`（任意模块）。控制台 `/config tool_import_profile` 可改 | `src/agent/config.py:191`；`src/agent/sandbox/checker.py:166-167` |
| `AGENT_PLATFORM_PORTS` | 空（全关） | 平台 VM 的额外 hostfwd，**只绑宿主回环**；拒绝显式绑定地址与 `445/139`；**重启平台 VM 生效** | `.env.example`；`deploy/windows/run-platform-vm.ps1:48-101` |
| `AGENT_OPEN_PULLED_IMAGES` | 开（非 `0` 即开） | `/get` 取回图片后是否自动用本机看图程序打开；`=0` 只落盘不弹窗 | `src/agent/cli/console.py:159-160,249`；`tests/unit/test_console_commands.py:1048-1118` |
| `AGENT_CONTROL_SECRET` | 空（`install.ps1` 生成随机值） | AI 服务 ↔ 控制平面的共享密钥，作为 `X-Agent-Token` 做常量时间比较；**两端必须一致**，为空时控制平面会告警"接受未认证 RPC" | `src/agent/config.py:86`；`src/agent/control/app.py:37-50`；`packaging/install.ps1` |

其它常用（同样以 `.env.example` 为准）：`AGENT_SANDBOX_ACCEL`（`kvm|whpx|tcg|auto`）、`AGENT_SANDBOX_POOL_SIZE=2`、
`AGENT_SANDBOX_MAX_VMS=4`、`AGENT_SANDBOX_VM_MEMORY_MB`、`AGENT_SANDBOX_VM_CPUS`、`AGENT_PERMISSION_TIER=safe`、
`AGENT_NET_ENABLED=0`、`AGENT_ASR_MODEL=base`。

---

## 6. 验证清单（逐条命令 + 期望输出）

```powershell
# 1) 控制平面活着 → 期望 HTTP 200
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8091/health

# 2) AI 服务活着（经 hostfwd 到平台 VM）→ 期望 HTTP 200
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8090/health

# 3) 沙箱池：期望看到 warm= 有热 VM（≥1）
.\.venv\Scripts\python.exe -m agent.cli sandbox status

# 4) 工具四道门：对自己的工具清单跑一遍（期望通过，不报白名单错误）
.\.venv\Scripts\python.exe -m agent.cli tools check <manifest.json>

# 5) 取回一张图（先在对话里让 AI 生成 /workspace/plot.png）→ 期望图片出现在 var\pulled\ 并自动弹开
#    在 agent 控制台里输入：  /get /workspace/plot.png

# 6) 工作盘大小：期望 /workspace ≈ 20G（构建期默认 20480M；开机自动扩容）
#    在控制台里让 agent 跑：  df -h /workspace
```

补充判定：

- `8090=000` 说明 hostfwd 没起来或 AI 服务没活（见 §7）。
- `agent tools check <file>` 的子命令签名就是"对某个 manifest JSON 跑 authoring gates"（`src/agent/cli/main.py:881-882`）。
- 拿一个"`import requests` 的工具"过 `check` 是这份清单里最关键的**功能**验证：`requests` 属于 `extended` 档，
  `strict` 下会被拒并给出 `/config tool_import_profile extended` 这类自带修复命令（`src/agent/sandbox/checker.py:166-167`）。
- `/get` 会把文件落在 `var\pulled\`（USAGE.md:138）。
- `agent doctor` 里的 postgresql / llm 两项**只有在平台 VM 里跑才有意义**，宿主上会显示 `n/a`（README.md:687）。

---

## 7. 排错表

| 现象 | 原因 | 怎么办 | 依据 |
|---|---|---|---|
| 控制平面起不来 / `8091` 被占 | QEMU hostfwd 抢走了宿主的 8091（Windows 上更具体的绑定优先）；`netstat` 里 8091 可能有**两个**监听 | 从 `run-platform-vm.ps1` 的转发里去掉 8091，重启平台 VM；guest 访问宿主走 slirp 的 `10.0.2.2`，**不需要**转发 | `deploy/windows/start-agent.ps1:169-177,210`；README.md:679 |
| VM 没起来 | 平台 VM 没跑 / 加速器不对 | `agent doctor` 看 accelerator（`tcg` 比 `whpx`/`kvm` 慢 5-20 倍）；`probe-whpx.ps1` 复现 `WHPX: Unexpected VP exit code 4` | README.md:669,674 |
| `:8090` 返回 `000` | hostfwd 没起来，或 AI 服务没活 | 来宾内 `systemctl status agentbox-ai` → `sudo systemctl restart agentbox-ai`；宿主上查监听：`netstat -ano` 里找 `8090` | `deploy/systemd/agentbox-ai.service` |
| 沙箱镜像过期 / 起 VM 报 EIO | 池里的 VM 还跑着旧镜像，或 overlay 与 base 指纹不匹配 | **换镜像后必须重启控制平面**；overlay 会按 base 指纹自动作废，实在要手工清就删 `var\sandbox\sessions`（先停控制平面，镜像文件可能被占用） | README.md:688-689；`deploy/windows/fetch-sandbox-image.ps1:27` |
| 沙箱里 `apt-get install` 不可用 | **根文件系统只读**（`rootflags=ro` + init 重新 remount），只有 `/workspace` 可写；而且默认没有网卡 | 这是设计：要装包得用 `net.fetch` 把 wheel 落到 `/workspace`，再 `pip install --no-index --find-links=/workspace/wheels` | README.md:54,440-441 |
| 语音说没麦克风 | `sounddevice` 缺失或设备没选 | 控制台 `/voice-devices` 列出麦克风 + `sounddevice`/朗读可用性，序号填进 `AGENT_VOICE_INPUT_DEVICE` | USAGE.md:133 |
| 模型没下（检索退化成关键词、ASR 不可用） | `AGENT_ASR_LOCAL_ONLY=true` 拒绝联网取权重；bge-m3 权重 ~2.3 GB 未下 | 在平台 VM 内下模型（`HF_ENDPOINT=https://hf-mirror.com`，`-SkipModels` 跳过的就补跑 `setup-agentbox.ps1` 该步）；`agent doctor` 看 embedder | `.env.example`；README.md:677；`packaging/README.md` 第 5 步 |
| `executor_uid=0` | **既有行为，不是漏洞**：guest 内 root ≠ 宿主 root。executor 是 VM 里的 PID 1/root（它要挂 cgroup、起进程），**所有不可信代码都以 uid 1000 `sandbox` 运行**——每次执行才降权 | 不用改 | README.md:633；README.md:148 的实测 `uid=1000(sandbox)`；`src/agent/sandbox/init.py:42-43,66` |
| `Login incorrect`（串口） | 串口回显输入，视觉上像成功 | 用 `vm-console.cmd`（它用 printf 探测真实 shell），别靠回显判断 | README.md:685 |
| 改了代码但 VM 里行为没变 | `/opt/agentbox/app` 是 provisioning 时的快照 | `.\deploy\windows\push-repo-to-vm.ps1`（`-RestartAi` 顺带重启）；改了 `src/agent/sandbox/*` 还要重建沙箱镜像 | README.md:684 |
| 401 | 两端 `AGENT_CONTROL_SECRET` 不一致 | 对齐宿主 `.env` 与来宾 `/opt/agentbox/app/.env` | README.md:676 |
| `Permission denied: '/home/agent/.postgresql/postgresql.key'` | systemd 的 `ProtectHome=true` 让 asyncpg 探测客户端证书失败 | DSN 里 `?ssl=disable`（仓库默认已加） | README.md:680；`.env.example` |

---

## 8. 安全边界

### 8.1 什么在包里、什么**不在**

| 不在包里（永远） | 依据 |
|---|---|
| `.env`（含 `AGENT_LLM_API_KEY`、`AGENT_CONTROL_SECRET`） | `.gitignore:18`；`packaging/README.md` 的"安全规则"节 |
| `var\vm_key`（SSH 私钥，以及 `.pub`） | `.gitignore:11`（整个 `var/`）；`make-bundle.ps1` 的"永不进包"清单 |
| **QEMU**（`qemu\`，GPLv2 的第三方程序） | **`-Slim` 包强制排除**（打包时逐文件扫描，出现就中止）；`-Lean`/`-Fat` 才带（内部用）。见 `THIRD-PARTY.md` |
| 沙箱镜像 `var\sandbox\`（`*.img`、`*.qcow2`、`vmlinuz`、`initrd.img`） | `.gitignore:11`（`var/`）；**`-Slim` 强制排除**（镜像内是 Debian 组件）；`-Lean`/`-Fat` 才带 |
| 平台 VM 磁盘 `var\platform\platform.qcow2`（约 10.64 GB） | 只在 `-Fat` 包里可选；默认 `.gitignore:11` + `-Slim`/`-Lean` 不含 |
| Debian 安装 ISO（`*.iso`） | `.gitignore:23`；只在 `-Fat` 包里可选；`-Slim` 强制排除 |
| 模型权重（来宾 `/opt/agentbox/models`，~4.8 GB） | 在 qcow2 内部，随平台磁盘走；单独不进包（安装时在 VM 内下载） |

`make-bundle.ps1` 还有两层检查：① `-Slim` 时 payload 里出现 `qemu\`、`qemu*.exe`/`*.dll`、
`*.img`/`*.qcow2`/`*.iso`/`*.vmdk`/`*.raw` 就**中止打包**（`-ListPayload` 复核也跑同一套规则）；
② 文件内容里出现真实密钥赋值（`AGENT_LLM_API_KEY=sk-…` 之类；占位符放过）会被**跳过并打印文件名**，
打完再核一遍 `MANIFEST.sha256` 里不该出现 `.env`/`vm_key`（`packaging/README.md` 的"安全规则"节）。

### 8.2 沙箱隔离的三条不变量

1. **没有网卡**：`net_mode=off`（默认）时沙箱 VM 根本没有 NIC；即使 `full`，也只是"只出不进"的 slirp 网卡。
   所以模型现写的代码**无法**把 `/workspace` 里的东西发出去（README.md:403、`.env.example`）。
2. **根只读、唯一可写点**：内核 `rootflags=ro` + init 重新 remount，只有 `/workspace` 是独立 virtio 盘；
   守卫测试 `test_guest_reports_hardened_environment` / `test_root_is_read_only` 钉死（README.md:54）。
3. **非 root + 限额 + 无共享**：工具代码以 uid 1000、无密码、不可登录的 `sandbox` 用户运行；cgroup + RLIMIT 限额、超时、`killpg`；
   **宿主与沙箱之间没有共享目录、没有 hostfwd**，唯一通道是 virtio-serial 上的 JSON-RPC（README.md:359；`deploy/windows/stop-agent.ps1:97`）。

### 8.3 提示词不能覆盖安全段

安全约束由**服务端策略**执行，不是靠提示词"请遵守"：

- **权限分级** `AGENT_PERMISSION_TIER`（`safe`/`trusted`/`unrestricted`）是 AI 服务强制的策略上限（`src/agent/config.py:211-216`）；
- `host.exec` 默认关，且必须**逐字**打出 `AGENT_HOST_EXEC_PHRASE` 才可能执行，并且只能落在 `AGENT_HOST_WORKDIR` 里（`.env.example`）；
- 从沙箱取文件是**另一条独立开关**（`host_pull_enabled` / `host_pull_phrase`，默认开着，但落点锁死在 `var/pulled`、单文件 8 MB、不覆盖同名），不走 `host.exec`（`src/agent/config.py:224-231`）；
- 工具源码有 sha256 完整性：注册中心存一份，控制平面与 guest 各校验一次（README.md:56，`test_tool_source_hash_is_verified`）。

也就是说：**会话内容/提示词改不了上面这些开关的判定**——它们读的是 `.env`/运行时配置，在服务端生效。

> **操作员想加自己的提示词，请写文件，不要手改 `.py`**（手改 `src/agent/ai/*.py` 出错会让 AI 服务起不来）：
> 在 VM 里 `sudo tee /opt/agentbox/custom-prompt.md`（仓库外，`push-repo-to-vm.ps1` 不会覆盖），并在 `.env` 里加
> `AGENT_CUSTOM_PROMPT_FILE=/opt/agentbox/custom-prompt.md`。**每次请求重新读**（改完下一轮生效，不用重启），
> 位置在 persona 与内置规则**之后**（`base → persona → operating → custom → runtime`），但同样改不了上面这些服务端开关；
> 文件缺失/空/非 UTF-8/过大只会被忽略并记一条日志，服务不受影响（细节见 [`USAGE.md`](USAGE.md) §4 末）。

---

## 9. 与其它文档的关系

| 文档 | 管什么 | 本文不重复的部分 |
|---|---|---|
| [`README.md`](README.md) | **技术概览**：架构、工具库/四道门、会话与历史护栏、联网开关、权限分级、控制台、隔离不变量表（I1-I7）、运维与排错表 | 原理与实测数据都在那里；本文只做"部署/连接/验证"这一面的汇总与指针 |
| [`USAGE.md`](USAGE.md) | **日常操作**：控制台全部斜杠命令、`/get`、语音与朗读、persona、端口转发怎么改 | 命令语义与示例以 USAGE 为准；本文 §3 只给"怎么连"的可复制入口 |
| [`RELEASE-v1.md`](RELEASE-v1.md) | **v1 范围与已知限制**：来源 commit、包含什么、实测到什么程度、**未做/未在真机验证**的项 | 本文不承诺 v1 没承诺过的东西 |
| [`packaging/README.md`](packaging/README.md) | **打包/离线安装**：`make-bundle.ps1` 三种预设、`setup-agentbox.ps1` 的 8 步细节、wheelhouse 与 Python 版本耦合、安全规则、验证记录 | §4 只保留"目标机器上要做什么"，打包侧的取舍与实测数字看那里 |

> 有冲突时以**脚本文本**为准：`packaging\setup-agentbox.ps1`、`deploy\windows\*.ps1`、`deploy\sandbox\build-sandbox-image.sh`、
> `src\agent\config.py`（`.env` 默认值的唯一权威）。本文中凡是标了 `（以脚本为准：<path>）` 的地方都照这个规则读。
