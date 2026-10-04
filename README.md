# agentbox — 沙箱化 AI Agent

一个 Python AI Agent：**所有工具执行都发生在 QEMU + Debian minimal 虚拟机里**，
宿主（Windows）只负责管理 VM 生命周期，AI 服务（Debian 平台 VM 内）只负责对话与决策。

模型的上下文里永远只有 **3 个元工具**：`search_tools` / `get_tool_schema` / `call_tool`，
其余工具（含模型自己写的工具）按需检索注入。

```
┌──────────────┐  HTTP/SSE   ┌────────────────────────────┐
│ Rich CLI     │ ──────────► │ AI 服务 FastAPI :8090      │   Debian 平台 VM
│ (宿主/任一端)│ ◄────────── │ LLM 循环 / 元工具 / 工具流水线│   PostgreSQL 17 + pgvector
└──────────────┘             └───────┬──────────────┬─────┘
                    注册中心(只读/读写) │              │ HTTP JSON-RPC
                          ┌──────────▼───────┐      │
                          │ PostgreSQL 17    │◄─────┤
                          │ + pgvector(1024) │      │
                          └──────────────────┘      │
                                          ┌─────────▼──────────────────┐
                                          │ 控制平面 FastAPI :8091      │  Windows 宿主
                                          │ 池/生命周期/overlay/JobObject│  (或 Linux)
                                          └─────────┬──────────────────┘
                                                    │ virtio-serial (TCP loopback)
                                          ┌─────────▼──────────────────┐
                                          │ QEMU: Debian minimal 沙箱   │
                                          │ -nic none / 根只读 / 非 root│
                                          │ executor(JSON-RPC 服务端)   │
                                          │ /workspace 唯一可写(vdb)    │
                                          └────────────────────────────┘
```

## 目录

- [设计不变量](#设计不变量)
- [文件树](#文件树)
- [快速开始](#快速开始)
- [四个组件](#四个组件)
- [工具作者流水线](#工具作者流水线)
- [RPC 协议](#rpc-协议)
- [数据库](#数据库)
- [安全模型](#安全模型)
- [测试](#测试)
- [运维与排错](#运维与排错)
- [已知限制](#已知限制)

## 设计不变量

| # | 不变量 | 强制手段（可验证） |
|---|---|---|
| I1 | AI 服务**永不**碰宿主文件/命令 | `tests/unit/test_isolation_guard.py` 用 AST 扫描 `ai/ registry/ toolsmith/ models/`，出现 `subprocess/socket/ctypes/shutil` 导入、`open()`、`Path.read_text()`、`agent.control.*` 导入即失败 |
| I2 | 只有控制平面能创建进程 | 全仓库仅 `control/vm.py` 出现 `create_subprocess_exec`；守卫测试断言其他包没有进程创建调用 |
| I3 | 不可信代码只在 VM 内执行 | 静态检查 `py.check`、测试 `tool.test`、正式调用 `py.run` 三者都在 guest 内；宿主从不执行工具源码 |
| I4 | 沙箱无网络 | QEMU `-nic none`（根本没有网卡）+ guest 内 nftables 兜底；`test_no_network_device_is_ever_attached` 断言 argv |
| I5 | 根只读、唯一点可写 | 内核 `rootflags=ro` + init 重新 remount；只有 `/workspace` 是独立 virtio 磁盘；`test_guest_reports_hardened_environment` / `test_root_is_read_only` |
| I6 | 每条命令都有天花板 | 宿主：Windows Job Object / Linux cgroup v2；guest：每条命令独立 cgroup v2 + rlimit + 超时 + 输出截断 |
| I7 | 工具源码完整性 | sha256 由注册中心存放，控制平面与 guest 各校验一次（`test_tool_source_hash_is_verified`） |
| I8 | 会话之间互不可见 | 每会话独立 qcow2 overlay；`test_sessions_do_not_share_a_workspace` |
| I9 | 失败工具自动熔断 | 连续 3 次失败 → `quarantined`，检索默认不再召回 |

## 文件树

```
Agent/
├─ pyproject.toml                 # 依赖 + pytest/ruff 配置（Python >= 3.11）
├─ .env.example                   # 全部配置项（复制成 .env）
├─ README.md
├─ qemu/                          # 你已有的 Windows QEMU 11.1.0（不改动）
├─ debian-13.7.0-amd64-netinst.iso# 平台 VM 安装源
├─ deploy/
│  ├─ windows/
│  │  ├─ new-platform-vm.ps1      # 建盘 + preseed 全自动装 Debian 13（支持 -DryRun / -Follow / -Mode manual）
│  │  ├─ run-platform-vm.ps1      # 启动平台 VM（hostfwd 8090/8091，支持 -DryRun / -Headless）
│  │  ├─ iso_extract.py           # 纯 Python ISO9660 抽取器：取出安装器 vmlinuz + initrd.gz
│  │  ├─ probe-whpx.ps1           # 实测 WHPX 能配哪个 CPU 型号 / 几个 vCPU
│  │  └─ preseed.cfg              # 平台机 preseed（含 PG + pgvector + python）
│  ├─ sandbox/build-sandbox-image.sh  # debootstrap 构建沙箱镜像（内核+initrd+只读 rootfs+空白 workspace）
│  ├─ platform/install-platform.sh    # 平台机内：apt 装 PG17+pgvector、venv、建库、systemd
│  └─ systemd/agentbox-{ai,control}.service
├─ src/agent/
│  ├─ config.py                   # 全部设置（AGENT_ 前缀）
│  ├─ embeddings.py               # 本地 BGE-M3 / DashScope，固定 1024 维
│  ├─ models/
│  │  ├─ protocol.py              # JSON-RPC 信封 + NDJSON 分帧 + 错误码
│  │  └─ tool.py                  # 工具清单/记录/参数校验 + 全部 RPC 载荷模型
│  ├─ registry/                   # 工具注册中心
│  │  ├─ tables.py db.py          # SQLAlchemy 表（vector(1024) / HNSW / trgm）
│  │  ├─ ranking.py               # RRF 融合（纯函数，可单测）
│  │  ├─ repository.py            # CRUD、版本、统计、熔断、会话
│  │  ├─ search.py                # pgvector 余弦 + pg_trgm 关键词 混合检索
│  │  ├─ service.py               # 检索/取 schema/记账/种子
│  │  └─ seed.py                  # 13 个内置工具定义（fs/exec/sandbox/toolsmith）
│  ├─ ai/                         # AI 服务（FastAPI）
│  │  ├─ app.py                   # 路由：/chat /sessions /tools /sandbox /health
│  │  ├─ agent_loop.py            # 常驻工具循环 + 事件流
│  │  ├─ metacalls.py             # 3 个元工具实现 + 唯一分发路径 + qsonschema 校验
│  │  ├─ sandbox_gateway.py       # 到控制平面的 HTTP JSON-RPC（AI 服务唯一出口）
│  │  ├─ llm.py                   # OpenAI 兼容流式客户端（DeepSeek）
│  │  └─ prompts/system.md        # 系统提示（沙箱事实 + 造工具规范）
│  ├─ toolsmith/gates.py          # G0 清单 → G1 静态 → G2 沙箱测试 → G3 注册
│  ├─ control/                    # 控制平面（FastAPI，唯一能起进程的地方）
│  │  ├─ app.py                   # /rpc /sandbox/status /health
│  │  ├─ manager.py               # 预热池、会话绑定、LRU 淘汰、空闲回收
│  │  ├─ vm.py                    # QEMU 进程、overlay、握手、优雅关机
│  │  ├─ client.py                # 到 guest 的 NDJSON 客户端 + HMAC 双向认证
│  │  └─ host/{base,windows,linux}.py  # Job Object / cgroup v2 / 空实现
│  ├─ sandbox/                    # ★ guest 内运行的代码（仅标准库）
│  │  ├─ init.py                  # PID 1：挂载、只读根、fw_cfg 取 token、看守 executor
│  │  ├─ executor.py              # virtio-serial 上的 JSON-RPC 服务端
│  │  ├─ handlers.py              # fs/exec/sandbox/py.check/py.run/tool.test/install
│  │  ├─ runner.py                # 以 sandbox 用户加载工具、注入 fs/sh、写回结果
│  │  ├─ policy.py                # /workspace 路径策略（POSIX 语义）
│  │  ├─ limits.py                # cgroup v2 + rlimit + 超时 + 进程组清理
│  │  └─ checker.py               # AST 白名单静态检查器
│  └─ cli/main.py                 # Rich CLI（serve/chat/tools/sandbox/image/db/doctor）
└─ tests/
   ├─ conftest.py                 # FakeEmbedder / FakeGateway / settings_factory
   ├─ unit/                       # 283 个用例，无需 DB、无需 QEMU
   ├─ integration/test_registry_pg.py   # -m pg，真 PG + pgvector
   ├─ sandbox/test_sandbox_vm.py        # -m sandbox，真 QEMU VM 与隔离断言
   ├─ smoke_imports.py            # 全模块导入 + 两个 ASGI app 构建 + CLI 解析 + 13 个内置工具清单校验
   └─ smoke_qemu_argv.py          # 用本机 QEMU 校验 argv 与 overlay 创建（无需可引导镜像）
```

## 快速开始

> ### ✅ 当前状态：环境已经装好，三套测试全绿，端到端已跑通
>
> 这台机器上平台 VM、PostgreSQL、AI 服务、控制平面、沙箱镜像都已经就位，
> **现在只要一条命令就能对话**：
>
> ```powershell
> cd C:\Users\86133\Desktop\Agent
> powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1
> ```
>
> | 测试套件 | 命令 | 结果 |
> |---|---|---|
> | 单元测试 | `python -m pytest -m "not pg and not sandbox"` | **399 passed, 2 skipped** |
> | 沙箱集成（真实 QEMU/WHPX VM） | `python -m pytest -m sandbox` | **25 passed** |
> | PostgreSQL 集成（在平台 VM 内跑） | `AGENT_DB_URL=... python -m pytest -m pg` | **12 passed** |
>
> 实测通过的真实链路（原样粘贴自 `agent chat` 输出，5.7 秒完成）：
>
> ```
> → search_tools {"query": "run shell command in sandbox"}
> → get_tool_schema {"name": "exec.run"}
> → call_tool {"name": "exec.run", "arguments": {"argv": ["id"]}}
>   ok {"exit_code": 0, "stdout": "uid=1000(sandbox) gid=1000(sandbox) groups=1000(sandbox)\n"}
> → call_tool {"name": "exec.run", "arguments": {"argv": ["sh","-c","echo test > /etc/ro_test_file"]}}
>   ok {"exit_code": 2, "stderr": "sh: 1: cannot create /etc/ro_test_file: Read-only file system\n"}
> ```
>
> 即：LLM 决策 → 只注入 3 个常驻元工具 → 检索工具 → 取 schema → **命令在 QEMU 沙箱内执行**
> （非 root 的 `sandbox` 用户、根只读、`/workspace` 可写、无网卡、cgroup+rlimit 限额）。

> ### ⚠️ 先看清楚：每条命令在哪个终端、哪个目录
>
> 这套系统跨两个操作系统，**命令敲错地方是最常见的坑**：
>
> | 步骤 | 在哪个终端 | 工作目录 | 用哪个 shell |
> |---|---|---|---|
> | 1. 建平台 VM | 这台 **Windows** 的 PowerShell | `C:\Users\86133\Desktop\Agent`（仓库根） | `powershell`（5.1 就够，**不需要 pwsh**） |
> | 2. 装平台依赖 | **平台 VM 内部**（QEMU 窗口或 SSH） | 仓库在 VM 里的路径 | `bash` |
> | 3. 造沙箱镜像 | **平台 VM 内部** | 同上 | `bash`（`sudo`） |
> | 4. 起控制平面 | 这台 **Windows** 的 PowerShell | 仓库根 | `powershell` |
> | 5. 起 AI 服务 | **平台 VM 内部** | 同上 | `bash` |
> | 6. 对话 / 自检 | 两边都行 | — | 都行 |
>
> 本机只有 **Windows PowerShell 5.1**（没有 PowerShell 7 / `pwsh`），所以下面一律用
> `powershell -ExecutionPolicy Bypass -File ...`。所有 `.ps1` 路径都相对脚本自身解析，
> 只要**脚本路径写对**，工作目录不影响。
>
> 进 VM 有两个通道：`ssh -i var\vm_key -p 2222 agent@127.0.0.1`（正常），
> 或者 `.\deploy\windows\vm-console.ps1`（串口控制台，guest 被打满、sshd 没响应时用）。
>
> **第 2 步之前，先确认 1 成功了。** 任何脚本都可以先 `-DryRun` 预演（只打印要执行的命令，不改动任何东西）。

### 0. 前置

| 组件 | 在哪 | 说明 |
|---|---|---|
| QEMU 11.1 (`qemu/`) | Windows 宿主 | 已就绪，实测 `whpx` 可用、**无嵌套虚拟化** |
| Python 3.11+ | 两端 | 宿主跑控制平面，Debian 内跑 AI 服务 |
| Debian 13 平台 VM | Windows 宿主上的 QEMU | 用你给的 netinst ISO 安装 |
| PostgreSQL 17 + pgvector | 平台 VM 内 | `apt install postgresql-17-pgvector`，无 Docker |

实测结论（都会影响配置）：

* `qemu-system-x86_64 -accel whpx` 可用，但 **WHPX 不能配 `-cpu host` / `-cpu max`**：guest 会立刻
  以 `WHPX: Unexpected VP exit code 4` 死掉。**必须用 `-cpu qemu64`**（或 `Nehalem`），代码里的
  默认值已经这样设了，可用 `AGENT_SANDBOX_CPU` 覆盖。`deploy/windows/probe-whpx.ps1` 就是做这个矩阵实测的脚本。
* `-accel whpx,nested=on` 不存在 → **沙箱不能跑在 Debian VM 里面**（会退化成 TCG 软件模拟），
  因此采用 "Windows 当宿主 + Debian 平台 VM" 的拓扑。

### 1. 建平台 VM（Windows 宿主的 PowerShell，仓库根目录）

```powershell
cd C:\Users\86133\Desktop\Agent

# 想看它会干什么（不改动任何东西）
powershell -ExecutionPolicy Bypass -File .\deploy\windows\new-platform-vm.ps1 -DryRun

# 真正开始：全自动安装（5-15 分钟），-Follow 会另开窗口实时显示安装日志
powershell -ExecutionPolicy Bypass -File .\deploy\windows\new-platform-vm.ps1 -Follow
```

这一步做了四件事，全部在这台机器上实测通过：

1. 从 ISO 里用纯 Python 抽出安装器内核（`deploy/windows/iso_extract.py`，不需要管理员挂载 ISO、不需要 7z）；
2. 用 `-kernel/-initrd/-append` 直启安装器 —— **只有直启内核才能传内核参数**，`-cdrom` 启动时
   isolinux 掌管命令行，preseed 根本传不进去；
3. 在宿主上临时起 `http.server`，安装器通过 `preseed/url=http://10.0.2.2:8899/preseed.cfg` 拉取配置；
4. `-no-reboot`：装完 QEMU 直接退出（否则会又回到安装器）。

手动安装（想自己点鼠标）：`... new-platform-vm.ps1 -Mode manual`。

**耗时与实测情况**（我在你机器上真跑过一次）：下载安装器内核 → 自动分区（swap 2 GiB + ext4 根）→
装基础系统 → 装内核与 initramfs → 配置 apt → pkgsel 装 postgresql/pgvector/python。
**整个流程 15-40 分钟，瓶颈是经 QEMU slirp 下载软件包**（宿主带宽没问题，slirp 转发较慢）。
`-Follow` 会另开窗口显示安装日志；日志文件也在 `var/platform/install-console.log`。
`vmlinuz`/`initrd.gz` 抽一次就会复用，重复执行脚本不会重复抽取。
如果你的 WHPX 环境需要别的 CPU 型号，加 `-CpuModel Nehalem`（默认 `qemu64`）。

### 2. 启动平台 VM 并装依赖

```powershell
# Windows 宿主，仓库根目录
powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-platform-vm.ps1
```
```bash
# 平台 VM 内部（登录 agent / agentbox）
sudo /opt/agentbox/install-platform.sh      # preseed 已经把它放进去了
# 或者从仓库里执行： sudo deploy/platform/install-platform.sh
```

它会：apt 装 PostgreSQL 17 + `postgresql-17-pgvector` → 建库建角色 + `CREATE EXTENSION vector/pg_trgm`
→ 建 venv 安装本仓库 → 生成 `.env`（含随机 `AGENT_CONTROL_SECRET`）→ `agent db init` + `agent db seed`
→ 装 `agentbox-ai.service`。

然后编辑 `.env`：填 `AGENT_LLM_API_KEY`，把 `AGENT_CONTROL_SECRET` 复制到宿主侧。

### 3. 构建沙箱镜像（平台 VM 内部，bash + sudo）

```bash
cd /opt/agentbox/app
sudo deploy/sandbox/build-sandbox-image.sh
# 产出 var/sandbox/{vmlinuz,initrd.img,rootfs.img,workspace-blank.qcow2}
.venv/bin/python -m agent.cli image verify
```

### 4. 在 Windows 宿主上起控制平面

```powershell
cd C:\Users\86133\Desktop\Agent
.\qemu\..\  # （不需要，只是提醒：仓库根）
py -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"
$env:AGENT_CONTROL_SECRET = "<与平台 VM 相同>"
.\.venv\Scripts\python -m agent.cli serve control
```

### 5. 起 AI 服务并对话

```bash
# 平台 VM 内部
sudo systemctl start agentbox-ai     # 或 .venv/bin/python -m agent.cli serve ai
.venv/bin/python -m agent.cli doctor # 自检：PG / 控制平面 / QEMU / 镜像 / 模型 / 向量
```

```powershell
# Windows 宿主的 PowerShell（经 hostfwd 到平台 VM 的 8090）
cd C:\Users\86133\Desktop\Agent
.\.venv\Scripts\python -m agent.cli chat
you › 在沙箱里写一个 fizzbuzz.py 并运行它，把结果读回来给我
```

其它常用命令（目录同上，宿主侧用 `.\.venv\Scripts\python`，平台 VM 侧用 `.venv/bin/python`）：

```bash
python -m agent.cli sandbox status            # 池状态（accel/warm/active）
python -m agent.cli sandbox start smoke       # 立刻起一台 VM 端到端验证
python -m agent.cli sandbox console <vm-id>   # 看某台 VM 的串口日志
python -m agent.cli sandbox invoke sandbox.info --session s1
python -m agent.cli tools list -q "读取文件"
python -m agent.cli tools show fs.read --source
python -m agent.cli tools runs fs.read
python -m agent.cli image verify
python -m agent.cli db seed / db ping
python -m agent.cli doctor
```


## 启动顺序、账号密码、API 与模型

### 一键启动（推荐）

平台 VM、AI 服务、控制平面、沙箱预热池都装好之后，以后每次只要一条命令：

```powershell
cd C:\Users\86133\Desktop\Agent
powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1
```

它是幂等的，会打印每一步在做什么，然后进交互对话。常用变体：

```powershell
.\deploy\windows\start-agent.ps1 -NoChat                      # 只把环境拉起来
.\deploy\windows\start-agent.ps1 -Message "在沙箱里跑 uname -a"  # 跑一条就退出
.\deploy\windows\start-agent.ps1 -ForceRestartVm              # 平台 VM 卡住时强制重启
.\deploy\windows\stop-agent.ps1                               # 优雅停止（VM 干净关机，qcow2 不脏）
```

`start-agent.ps1` 的 7 步：前置检查 → 平台 VM（没跑就起，等 SSH）→ AI 服务（`systemctl` + `/health`）→
沙箱镜像（缺就从 VM 的 8099 拉）→ 控制平面（`/health`）→ 等预热 VM `ready` → `doctor` 摘要 → 对话。
任何一步失败都会打印该敲的下一条命令，不会静默停住。

| 脚本 | 用途 |
|---|---|
| `start-agent.ps1` | 一键启动（上面这条） |
| `stop-agent.ps1` | 优雅停止 |
| `provision-cloud-vm.ps1` | 首次：用云镜像 + cloud-init 配好平台 VM（几分钟） |
| `new-platform-vm.ps1` | 备选：用 netinst ISO 全自动安装（15-40 分钟） |
| `fetch-platform-image.ps1` | 只下载 Debian 云镜像（326 MiB，可断点续传 + SHA512 校验） |
| `fetch-sandbox-image.ps1` | 从平台 VM 拉沙箱镜像 4 件套到 `var\sandbox` |
| `push-repo-to-vm.ps1` | 改完代码把仓库推回平台 VM（`-RestartAi` 顺带重启服务） |
| `diag_sandbox.py` | 问一个**活着的沙箱 VM** 问题（走控制平面 RPC，不受 Windows 引号折磨）：`python deploy/diag_sandbox.py all` |
| `save-platform-image.ps1` | 把配好的平台 VM 导出成你自己的压缩镜像 |
| `run-platform-vm.ps1` | 只启动平台 VM（`-Headless` 无窗口，串口写 `var\platform\console.log`；`-SerialTcp 8906` 开可交互串口） |
| `vm-console.ps1` | 串口控制台客户端（`-Command 'uptime; free -m'` 跑一条，或交互登入）—— SSH 被 guest 重负载饿死时的救命通道 |
| `probe-whpx.ps1` | 诊断：实测 WHPX 能用哪个 CPU 型号 / vCPU 数 |
| `iso_extract.py` | 从 ISO 抽安装器内赅/initramfs（纯 Python，无需挂载 ISO） |

### 启动顺序（手动拆解，了解每一步在做什么）

### 完整启动顺序（照着从上往下走）

| # | 在哪执行 | 命令 | 预期 |
|---|---|---|---|
| 1 | Windows 仓库根 | `powershell -ExecutionPolicy Bypass -File .\deploy\windows\provision-cloud-vm.ps1 -Follow` | 几分钟后 VM 里出现 `AGENTBOX_READY`（备选：`new-platform-vm.ps1` 用安装器，15-40 分钟）|
| 2 | **平台 VM 内** | `sudo /opt/agentbox/app/deploy/platform/install-platform.sh` | 装 PG17+pgvector、建库、建 venv、装 systemd 单元、`db seed` |
| 3 | 平台 VM 内 | `sudo nano /opt/agentbox/app/.env` | 填 `AGENT_LLM_API_KEY`；记下 `AGENT_CONTROL_SECRET` |
| 4 | 平台 VM 内 | `sudo /opt/agentbox/app/deploy/sandbox/build-sandbox-image.sh` | 产出 `vmlinuz/initrd.img/rootfs.img/workspace-blank.qcow2` |
| 5 | 平台 VM 内 | `cd /var/lib/agentbox/sandbox && nohup python3 -m http.server 8099 --bind 0.0.0.0 &` | 把沙箱镜像发出来给宿主 |
| 6 | Windows 仓库根 | `powershell -ExecutionPolicy Bypass -File .\deploy\windows\fetch-sandbox-image.ps1` | 四个文件落到 `var\sandbox\`；`.\.venv\Scripts\python -m agent.cli image verify` 通过 |
| 7 | Windows 仓库根 | `$env:AGENT_CONTROL_SECRET='<同第3步>'; .\.venv\Scripts\python -m agent.cli serve control` | 控制平面起在 `:8091`，后台预热沙箱 VM |
| 8 | 平台 VM 内 | `sudo systemctl start agentbox-ai` | AI 服务起在 `:8090` |
| 9 | Windows 仓库根 | `.\.venv\Scripts\python -m agent.cli doctor` 然后 `.\.venv\Scripts\python -m agent.cli chat` | 全部 ok，可以对话 |

> 第 4-6 步是**必须的**：沙箱镜像只能用 debootstrap 在 Debian 里造，而控制平面跑在
> Windows 上（只有 Windows 的 QEMU 有 WHPX）。两边没有共享目录，所以走 HTTP 转发
> `127.0.0.1:8099 -> guest 8099`（两个启动脚本都已带上这个转发）。

### 账号密码

| 系统 | 用户 | 密码 | 说明 |
|---|---|---|---|
| 平台 VM（preseed 安装） | `agent` | `agentbox` | `root` 登录被禁用；`sudo` 可用 |
| 平台 VM（云镜像配置） | `agent` 与 `debian` | 均为 `agentbox` | `ssh_pwauth: true`，`disable_root: true` |
| **沙箱 VM** | **无** | **无** | 刻意如此：没有网卡、没有 getty、PID 1 是自写的 `agent-init`，唯一入口是 virtio-serial 上的 JSON-RPC。工具代码以 uid 1000 的 `sandbox` 用户运行，该用户没有密码、不可登录。想看沙箱内部只能读串口日志：`agent sandbox console <vm-id>` |

改密码：平台 VM 里 `passwd`；`provision-cloud-vm.ps1 -Password <新密码>` 可以换 provisioning 时的密码。

### API 配置（都在平台 VM 的 `/opt/agentbox/app/.env`）

| 键 | 必填 | 说明 |
|---|---|---|
| `AGENT_LLM_API_KEY` | ✅ | DeepSeek 的 key（`sk-...`） |
| `AGENT_LLM_BASE_URL` | | 默认 `https://api.deepseek.com`；换供应商只改这个 + 下面两行 |
| `AGENT_LLM_MODEL` | | 默认 `deepseek-chat`；复杂任务可用 `deepseek-reasoner` |
| `AGENT_CONTROL_SECRET` | ✅ | **必须与 Windows 宿主的同名环境变量一致**，否则 401 |
| `AGENT_CONTROL_URL` | | 默认 `http://10.0.2.2:8091`（QEMU 用户态网络里宿主就是 10.0.2.2） |
| `AGENT_EMBEDDING_BACKEND` | | `local`（bge-m3，见下）或 `dashscope` |
| `AGENT_DASHSCOPE_API_KEY` | 条件 | 选 `dashscope` 时必填 |
| `HF_ENDPOINT` | | 本地模型下载镜像，国内建议 `https://hf-mirror.com` |

改完 `.env` 要重启 AI 服务：`sudo systemctl restart agentbox-ai`（或前台 `python -m agent.cli serve ai`）。
宿主机侧验证：`curl http://127.0.0.1:8090/health`（经 hostfwd 到平台 VM）；控制平面：`curl http://127.0.0.1:8091/health`。

### 那个 2 GB 的模型是什么，要不要下

它是 **`BAAI/bge-m3`**（1024 维向量模型），只用于"按意图检索工具库"，让 `search_tools`
能理解自然语言而不是只做关键词匹配。**它是可选的**，三条路随便挑：

| 方案 | 代价 | 怎么做 |
|---|---|---|
| **① 不装（最省事）** | 检索退化为 pg_trgm 关键词匹配，功能完整、不报错 | 什么都不用做；`agent doctor` 会提示 embedder 不可用 |
| **② 本地装** | 权重约 2.3 GB + CPU 版 torch 约 200 MB（首次检索时下载权重） | `sudo INSTALL_LOCAL_EMBED=1 /opt/agentbox/app/deploy/platform/install-platform.sh`（它会先装 **CPU 版** torch，避免默认那 2.5 GB 的 CUDA 包，并在 `.env` 写入 `HF_ENDPOINT`） |
| **③ 用云 embedding** | 需要 DashScope key，按量计费 | `.env` 里 `AGENT_EMBEDDING_BACKEND=dashscope` + `AGENT_DASHSCOPE_API_KEY=...`，维度同为 1024，**不用下模型** |

手动装本地模型的话（不用 install-platform.sh）：

```bash
cd /opt/agentbox/app
.venv/bin/pip install torch --index-url https://mirrors.aliyun.com/pytorch-wheels/cpu   # CPU 版
.venv/bin/pip install -e ".[local-embed]"
echo 'HF_ENDPOINT=https://hf-mirror.com' | sudo tee -a .env     # 权重走镜像下
sudo systemctl restart agentbox-ai
.venv/bin/python -m agent.cli doctor    # embedder 应显示 available
```

### 网络开关与防火墙

**沙箱 VM 永远没有网卡**（这是核心不变量：模型现写的代码无法把 `/workspace` 里的东西发出去）。
"联网"这件事是 **AI 服务的能力**，以两个核心工具的形式暴露给模型：

| 工具 | 作用 |
|---|---|
| `net.fetch` | GET 一个 http(s) URL，把响应体**写进沙箱的 `/workspace`**（默认 `downloads/`） |
| `net.http` | 任意方法/头/体，可选把响应体存进 workspace |

开关与防火墙都在 AI 服务侧执行，**每一次请求和每一次重定向都过一遍**：

| 设置 | 默认 | 作用 |
|---|---|---|
| `AGENT_NET_ENABLED` | `0`（关） | 关掉时 `net.*` **不出现在 `search_tools` 里**，硬调也会被拒（`net_disabled`）|
| `AGENT_NET_ALLOW_HOSTS` | 空 | 逗号分隔白名单；`*.example.com` 通配；`*` 表示全放开（不安全，仅调试）。空 = 什么都不许 |
| `AGENT_NET_MAX_BYTES` | 8000000 | 单次响应体上限（超出截断并标记 `truncated`） |
| `AGENT_NET_TIMEOUT_S` | 30 | 连接/读取超时 |
| `AGENT_NET_MAX_REDIRECTS` | 3 | 跳转次数；**每一跳都重新校验白名单**（防跳转绕过） |
| `AGENT_NET_ALLOW_PRIVATE_HOSTS` | `0` | 只给测试用。为 0 时拒绝环回/内网/链路本地/元数据地址（`127.0.0.1`、`10.x`、`192.168.x`、`169.254.169.254`、`::1`…），既查 URL 里的 IP 字面量，也查**实际连上的对端地址**（防 DNS 重绑定） |

也限制 scheme：只允许 `http`/`https`（`file://`、`ftp://`、`gopher://` 一律拒绝）。

**审计**：每次尝试都打一行结构化日志（含被拒原因与是哪一层拦的），systemd 收进 journal：

```bash
journalctl -u agentbox-ai -g net.audit | tail -20
# {"event":"refused","url":"http://127.0.0.1:8091/health","reason":"refusing '127.0.0.1': ... is a private/internal address","code":"net_denied"}
# {"event":"fetched","url":"https://example.com","status":200,"bytes":577}
```

**实测（本机）**：开关关 → 模型抓取被拒并明确说明"需要操作员开 `AGENT_NET_ENABLED` + 白名单，我无法自行开启"；
开关开 + 白名单 `example.com` → 抓到 200/577 字节并落进 `/workspace/example.html`，`www.iana.org` 被白名单拒；
白名单设 `*` 时 `127.0.0.1` 与 `169.254.169.254` 仍被 SSRF 那层拒（两层独立生效）。

怎么用它装 Python 包（沙箱自身不通网，靠搬运）：

```text
net.fetch(url="https://files.pythonhosted.org/.../requests-2.32.3-py3-none-any.whl",
          dest="/workspace/wheels/requests.whl")
exec.run(argv=["pip","install","--no-index","--find-links=/workspace/wheels",
               "--target=/workspace/.tools/pylibs","requests"])
exec.run(argv=["python3","-c","import requests;print(requests.__version__)"],
         env={"PYTHONPATH":"/workspace/.tools/pylibs"})
```

想彻底关掉：把 `AGENT_NET_ENABLED=0` 写进平台 VM 的 `/opt/agentbox/app/.env`，`sudo systemctl restart agentbox-ai`。

### 控制台指令、权限分级、persona、MCP

对话界面里以 `/` 开头的行是**指令**，不是发给模型的消息。**按 Tab 补全**（指令名、子命令、
persona 名都有候选），`/help` 看总览，**`/help <指令>` 看单条详解**（例如 `/help persona`），
输入历史存在 `var/chat-history`。补全依赖 `prompt_toolkit`；如果它在某个环境里不可用，
控制台会自动降级成普通输入（仍然能用 `/help`）。

| 指令 | 作用 |
|---|---|
| `/net` · `/net on\|off` | 看/开关联网能力（防火墙那套：白名单+端口+SSRF 防护） |
| `/net allow a.com,*.b.com` · `/net deny x` | 增删白名单（运行时生效） |
| `/net ports 80,443,8443` · `/net private on\|off` | 端口白名单；`private` 会额外放行环回/内网/云元数据地址（**危险**，仅测试用） |
| `/perm` · `/perm safe\|trusted` | 权限档位；`safe` 只有沙箱工具，`trusted` 加联网与 MCP |
| `/perm unrestricted <短语>` | **危险**：解锁 `host.exec`（在**这台 Windows** 上执行命令），默认关闭，必须逐字输入 `AGENT_HOST_EXEC_PHRASE` |
| `/persona` | 列出 persona 与当前生效的那个 |
| `/persona roleplay\|teacher\|reviewer\|concise\|engineer` | 切换回答人格（只改 system prompt 的风格段，**不放松任何权限**，安全段落永远在 persona 之后） |
| **`/persona off`** | **取消角色扮演**，回到中性的 `engineer`（同义词：`none` / `default` / `neutral`；直接写 `engineer` 也一样） |
| `/config` · `/save` | 看所有运行时可改项；`/save` 把当前值写进平台 VM 的 `.env`（下次重启仍生效） |
| `/sandbox` | 沙箱池状态（VM 列表/加速器/预热数）；guest 内存与工作区大小是启动/构建期参数 |
| `/log ide\|plain\|json` · `/log lines N` · `/log args on\|off` | 切换工具日志渲染（本地设置） |
| `/voice <音频文件>` | 把本地录音转成文字（POST `/asr`），并作为下一条消息发给 agent |
| `/voice-mode on\|off\|status\|hands-free` | **实时语音模式**：按回车说话，静音自动断句，回答朗读出来（需要麦克风） |
| `/voice-devices` | 列出麦克风，并显示 `sounddevice` / 朗读是否可用；序号可填进 `AGENT_VOICE_INPUT_DEVICE` |
| `/speak on\|off` | 回答是否朗读（Windows 自带 SAPI，无需新依赖；只影响这个终端） |
| `/session` `/new` `/help [指令]` `/exit` | 会话管理 |

自定义 persona：放 `<repo>/personas/<name>.md`（同名覆盖内置）。人格名严格校验
（`[a-z0-9_-]{1,32}`），`../../etc/passwd` 这种直接降级到默认并告警。

运行时改为**立即生效**（AI 服务持有这些设置，且它不许写文件，所以持久化由 `/save` 显式完成）。

#### 实时语音模式（`agent chat --voice`）

按回车说话 → 本地录音（16 kHz 单声道）→ 静音自动断句 → POST `/asr` → 识别结果作为下一条消息
发给 agent → 回答用 Windows 自带的 SAPI 朗读。**需要麦克风**，录音依赖可选的 `sounddevice`：

```powershell
.\.venv\Scripts\pip.exe install sounddevice numpy    # 或 pip install "agentbox[voice]"
agent chat --voice            # 直接进语音模式；--no-speak 只识别不朗读
```

* `/voice-devices` 列设备（序号或名字填进 `AGENT_VOICE_INPUT_DEVICE`，空 = 系统默认设备）；
  `/voice-mode on` / `hands-free` / `off` / `status`；`/speak on|off` 控制朗读。
* 默认是**按回车说一句**（麦克风不会常开，否则助手会把自己的朗读录回去）；只有 `hands-free`
  才连续录，用 Ctrl-C 退出。
* 参数：`AGENT_VOICE_SILENCE_S`（静音多久算说完，默认 1.2 秒）、`AGENT_VOICE_MAX_S`（单句上限
  30 秒）、`AGENT_VOICE_VAD_FLOOR`（能量底噪 0.012）、`AGENT_VOICE_TTS`、`AGENT_VOICE_TTS_MAX_CHARS`
  （朗读截断 300 字）。
* 没装 `sounddevice`、没有麦克风、AI 服务不可用、只听到静音 —— 都会打印中文提示，不会抛异常。

**权限分级**是策略上限，与单个开关叠加生效；两级都会检查：

| 档位 | 允许什么 |
|---|---|
| `safe`（默认） | 只有沙箱工具：`fs.*` `exec.run` `sandbox.*` `toolsmith.*` |
| `trusted` | + `net.*`（仍受白名单/端口/SSRF 限制）、`mcp.*` |
| `unrestricted` | + `host.exec`：在控制平面所在机器上执行命令 |

`search_tools` 只显示当前档位可用的工具，越档调用直接返回 `tier_denied`；`host.exec` 还被**控制平面**独立再检查一次（`AGENT_PERMISSION_TIER=unrestricted` + 短语），并且每次执行都进 `tool_runs` 台账、日志里是 WARNING 级。

```powershell
# 本机实测：safe 档下 net.fetch 连检索都搜不到；
#           /perm trusted 后同一句话立刻抓到 200/577 字节；
#           /perm unrestricted ENABLE-HOST-EXEC 后 agent 真的在 Windows 上跑了 git
```

**Persona**：内置 `engineer`(默认) / `roleplay`(角色扮演) / `teacher` / `reviewer` / `concise`。
切换 `/persona <名字>`，**取消角色扮演用 `/persona off`**（等价于切回 `engineer`；`none`/`default`/`neutral` 同义）。
自己的放 `<repo>/personas/<name>.md`（同名会覆盖内置）。人格名会被严格校验（`[a-z0-9_-]{1,32}`），
`../../etc/passwd` 这种直接降级到默认并告警。

**MCP**（Streamable HTTP 传输）：

```bash
# 平台 VM 的 /opt/agentbox/app/.env
AGENT_MCP_SERVERS={"demo": {"url": "http://10.0.2.2:8971/mcp", "headers": {"Authorization": "Bearer ..."}}}
```

然后对 agent 说"同步 MCP 工具"，它会调 `mcp.sync`，把服务器上的工具注册成
`mcp.<server>.<tool>`（保留原始 inputSchema，自动裁剪成我们的 JSON-Schema 子集），
之后 3 个常驻元工具照旧检索/调用它们。**stdio 类 MCP 服务器不支持**：AI 服务不允许起进程
（隔离守卫），请把这类服务器以 HTTP 暴露出来。本仓库带一个最小桩用于验证：
`python var/mcp_stub_server.py 8971`。

## 四个组件

### AI 服务（`agent.ai`，FastAPI :8090）

* `POST /chat`：SSE 流（`token` / `tool_call` / `tool_result` / `done` / `error`）或 `stream=false` 一次性 JSON
* `POST /sessions`、`GET /sessions/{id}/messages`、`DELETE /sessions/{id}`
* `GET /tools?query=…`（混合检索）、`/tools/{name}`、`/tools/{name}/runs`、`/tools/inventory`
* `POST /tools/check`：拿一份清单跑 G1+G2（不注册），方便人工验证模型要提交的工具
* `GET /health`、`GET /sandbox`（代理控制平面池状态）

上下文预算：系统提示（沙箱事实 + 造工具规范）+ 历史 + 3 个元工具 schema。
工具检索返回 5 条摘要（名字/描述/何时用/参数摘要/权限/分数），单条工具结果超过 32 KB 会被裁剪。

### 控制平面（`agent.control`，FastAPI :8091）

* 预热池 `AGENT_SANDBOX_POOL_SIZE` 台常驻 VM；会话首次调用直接拿预热机（省 3-10 秒）
* 会话绑定 + LRU 淘汰（`AGENT_SANDBOX_MAX_VMS`）+ 空闲回收（`AGENT_SANDBOX_IDLE_REAP_S`）
* 会话再次使用时复用同名 overlay → 文件在 VM 重启后仍在；全新会话拿到干净工作区
* 释放 VM 时先发 `sys.shutdown`（guest `sync` + ACPI poweroff），15 秒不退出才强杀
* 宿主限制：Windows Job Object（内存/进程数/CPU 速率 + `KILL_ON_JOB_CLOSE`）、Linux cgroup v2

### 沙箱 executor（`agent.sandbox`，guest 内）

* PID 1 是自写的 `agent-init`（**刻意不用 systemd**）：挂载 devtmpfs/proc/sys/cgroup2 与受限 tmpfs，
  强制根只读，把 `/dev/vdb` 挂到 `/workspace` 并 chown 给 `sandbox`，从 QEMU `fw_cfg` 取 token，
  拉起 executor 并看守重生，executor 退出即关机（不留孤儿 VM）
* executor 在 `/dev/vport0p1` 上跑 NDJSON JSON-RPC；握手用常量时间比较 + HMAC 回执（双向认证）
* 每个 `exec.run` / `py.run` 都在独立 cgroup v2 子组里跑：`memory.max` 512 MiB、`pids.max` 128、`cpu.max` 200%，
  外加 `RLIMIT_CPU/FSIZE/NOFILE/NPROC/CORE/STACK`、墙钟超时、输出上限，超时用 `killpg` 清整组

### 工具注册中心（`agent.registry` + PostgreSQL）

* `tools`（name+version 唯一、status、tier、executor、params_schema、permissions、source+sha256、`vector(1024)`）
* `tool_runs`（每次调用，含脱敏参数）、`sessions`、`messages`、`sandbox_sessions`
* 索引：`HNSW(vector_cosine_ops)`、`GIN(tags)`、`GIN(search_text gin_trgm_ops)`
* 混合检索：向量 top-20 ∪ 关键词 top-20 → RRF 融合 → tier 加权（core ×1.08）→ 按名字去重 → top-k
  （中文查询靠 `pg_trgm` 的 `word_similarity` 命中，实测 `test_keyword_half_works_for_chinese_queries`）

## 工具作者流水线

模型想造新工具时（先 `search_tools("create a new tool")` 找到 `toolsmith.*`，它们不是常驻元工具）：

```
G0 清单校验     名字 ^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$（3-41 字符）、描述长度、tags、
                params_schema 必须落在受支持的 JSON-Schema 子集内（object 根 + properties/required/items/
                enum/const/数值与长度约束 + additionalProperties:false；拒绝 $ref/oneOf/allOf/union type）、
                permissions ⊆ {fs.read, fs.write, exec, exec.shell}、源码 ≤16 KiB / ≤400 行、
                必须存在 def run(args)、保留前缀（fs./exec./sandbox./toolsmith.）不可占用
G1 静态检查      在沙箱内跑 AST 白名单：允许 import 的模块仅 30 个纯计算/数据模块（含 urllib.parse 但不含
                urllib.request）；禁 eval/exec/compile/__import__/open/input/breakpoint/globals/locals/vars/
                getattr/setattr/delattr；禁一切 dunder 属性；禁 while True 无 break；fs/sh 调用必须声明对应
                permissions（含 sh.run(shell=True) → exec.shell）；io.open / gzip.open / operator.attrgetter /
                typing.get_type_hints 单独拉黑
G2 沙箱测试      作者必须给 ≥3 个用例（成功/边界/错误路径），每个用例独立进程 + 超时；断言语言
                equals / contains / raises / is_true；全绿才继续
G3 注册          计算 embedding → 插入新版本（version = max+1）→ 同名前版本 retire → 返回工具卡片
熔断             连续 3 次失败 → status=quarantined → 检索不再召回，调用会被明确拒绝并提示重写
```

`toolsmith.check` 是"只检查不注册"的快速迭代入口；`toolsmith.create` 是完整流水线；`toolsmith.list_mine` 看自己造过什么。
运行期还有第二道防线：`runner.py` 按清单权限注入 `fs`/`sh`，越权调用直接 `PermissionError`。

## RPC 协议

传输：virtio-serial（宿主侧是 127.0.0.1 的 TCP chardev，控制平面作 **server**，不需要猜端口），NDJSON 分帧，单帧 ≤8 MiB，二进制走 base64。

| 方法 | 说明 |
|---|---|
| `sys.hello` / `sys.ping` / `sys.shutdown` | 认证握手 / 存活 / 优雅关机 |
| `fs.read` `fs.write` `fs.list` `fs.stat` `fs.mkdir` `fs.remove` `fs.move` | 全部限于 `/workspace`（符号链接逃逸也会被拒） |
| `exec.run` | `argv`/`shell`/`cwd`/`env`/`stdin`/`timeout_s`/`max_output_bytes` |
| `sandbox.info` `sandbox.reset` | 环境事实（含根只读探测、网卡列表、计数） / 清空工作区 |
| `py.check` | G1 静态检查 |
| `py.run` | 校验 sha256 → 安装到 `/workspace/.tools/<name>@<ver>/tool.py`（0444）→ 以 sandbox 用户执行 |
| `tool.test` | 跑用例并评估断言 |
| `tool.install` | 只安装（人工排查用） |

AI 服务到控制平面用同一个模型：`sandbox.acquire/release/invoke/status/reset/metrics/image_check`，
`invoke` 分 `kind=native`（工具名即 guest 方法名）与 `kind=python`（携带 `ToolPayload`，含源码与 sha256）。

## 数据库

```sql
tools(id pk, name, version, status, tier, executor, description, when_to_use, tags[], search_text,
      params_schema jsonb, permissions[], timeout_s, entrypoint, examples jsonb,
      source, source_sha256, embedding vector(1024), embedding_model,
      runs, failures, consecutive_failures, last_error, created_by, created_at, updated_at,
      UNIQUE(name, version))
tool_runs(id, tool_id fk, session_id, ok, duration_ms, error_code, error, args_redacted jsonb, created_at)
sessions(id, title, meta jsonb, created_at, last_seen_at)
messages(id, session_id fk, role, content jsonb, tokens, created_at)
sandbox_sessions(session_id pk, vm_id, state, created_at, expires_at)
```

建表用 `python -m agent.cli db init`（`CREATE EXTENSION vector/pg_trgm` + `create_all`，幂等）。

## 安全模型

* **谁被信任**：宿主控制平面（能起 QEMU）> 平台 VM 里的 AI 服务（只能读 PG + 出网调 LLM）> 沙箱内的一切（不可信）
* **AI 服务被 LLM 输出驱动**，所以它被硬性限制成"只会 SQL 和 HTTPS"：见不变量 I1 的 AST 守卫测试
* **guest 内 root ≠ 宿主 root**：executor 是 VM 里的 PID 1/root，但所有不可信代码都以 uid 1000 `sandbox` 运行
  （`test_commands_run_as_unprivileged_sandbox_user` 断言 `id -u` == 1000）
* **通信**：token 由控制平面每台 VM 随机生成，经 QEMU `fw_cfg` 注入（不进 `/proc/cmdline`，沙箱用户读不到），
  握手双向 HMAC；AI↔控制平面用 `AGENT_CONTROL_SECRET`（`X-Agent-Token`，常量时间比较）
* **不共享宿主目录**：不用 9p/virtiofs/块设备直通，`test_no_host_directory_is_shared_with_the_guest` 兜底
* **磁盘**：base rootfs 只读挂载 + 每会话 qcow2 overlay（backing 只读），复位 = 丢弃 overlay

## 测试

```bash
pytest -m "not pg and not sandbox"      # 283 个用例：无需 DB、无需 QEMU（1.7 秒）
pytest -m pg                            # 需要 AGENT_DB_URL 指向带 pgvector 的库
pytest -m sandbox                       # 需要已构建镜像 + 能起 QEMU（真 VM 隔离断言）
python tests/smoke_imports.py           # 全模块导入 + 两个 app 构建 + CLI 解析 + 内置工具清单
python tests/smoke_qemu_argv.py         # 用本机 QEMU 校验 argv 与 overlay（无需可引导镜像）
ruff check src tests
```

单测覆盖：AST 检查器 40+ 反例（含 `__subclasses__`、`getattr` 逃逸、`io.open`、`while True`）、
JSON-RPC 分帧（拆包/粘包/多字节/超限/坏 JSON）、清单与 JSON-Schema 子集、RRF 排序、
guest 认证矩阵与路径策略、工具测试断言语言、agent 循环（脚本化 LLM：工具往返/失败回灌/步数预算/LLM 报错）、
元工具分发（参数校验/检疫/原生 vs Python 路由/结果裁剪）、注册表熔断与 redaction、
QEMU argv 的 12 项安全断言、AI 服务隔离守卫。

沙箱套件（`-m sandbox`）断言：根只读、无网卡、`id -u`==1000、路径穿越与符号链接逃逸被拒、
超时清理进程组、输出截断、内存超限、会话互不可见、`tool.test` 全绿/失败的两种结果、
未声明权限的工具被拒、安装后的源码 0444 不可改、sha256 篡改被拒。

## 运维与排错

| 现象 | 排查 |
|---|---|
| `无法将"pwsh"项识别为...` | 本机只有 Windows PowerShell 5.1，用 `powershell -ExecutionPolicy Bypass -File ...`，不要用 `pwsh` |
| `无法将"...ps1"项识别为...` | 路径写错了：脚本路径相对**当前目录**，先 `cd C:\Users\86133\Desktop\Agent` |
| `禁止运行脚本` / `因为在此系统上禁止运行脚本` | 加 `-ExecutionPolicy Bypass`（上面命令里已有） |
| 中文输出乱码 | `.ps1` 必须是 **UTF-8 with BOM**（PowerShell 5.1 否则按 GBK 解码）；本仓库的脚本已经是 BOM |
| QEMU 起来后 CPU 不涨、串口日志 0 字节 | **WHPX 的 CPU 型号问题**：日志里会有 `WHPX: Unexpected VP exit code 4`。用 `-CpuModel qemu64`（脚本默认），沙箱侧设 `AGENT_SANDBOX_CPU=qemu64`。`deploy/windows/probe-whpx.ps1` 可复现 |
| 装完重启又回到安装器 | 安装器脚本已带 `-no-reboot`；如果你手工敲命令别忘了它 |
| preseed 没生效（一直在问问题） | 安装器必须用 `-kernel/-initrd/-append` 直启；`-cdrom` 启动时内核参数传不进去 |
| `sandbox image is not built` | 在平台 VM 内 `sudo deploy/sandbox/build-sandbox-image.sh` → `agent image verify` |
| VM 起不来 | `agent sandbox console <vm-id>` 看串口日志；`var/sandbox/console/<vm-id>.qemu.log` 是 QEMU 自身 stderr |
| 启动很慢 | `agent doctor` 看 accelerator；`tcg` 比 `whpx`/`kvm` 慢 5-20 倍，把 `AGENT_SANDBOX_POOL_SIZE` 调大预热 |
| `control plane ... unreachable` | 平台 VM 里 `AGENT_CONTROL_URL` 应为 `http://10.0.2.2:8091`；宿主控制平面监听 `0.0.0.0`；两边密钥一致 |
| 401 | `AGENT_CONTROL_SECRET` 两端不一致 |
| 检索总是关键词命中 | `agent doctor` 看 embedder：本地模型未装会退化成关键词检索（`pip install -e ".[local-embed]"` 或切 `AGENT_EMBEDDING_BACKEND=dashscope`） |
| 工具被检疫 | `agent tools runs <name>` 看最近错误；修好后 `toolsmith.create` 出新版本即自动解除（新版本 status=active） |
| `control plane unreachable at http://127.0.0.1:8091` 且 `netstat` 里 8091 有两个监听 | **给平台 VM 加过 8091 的 hostfwd**，QEMU 抢走了宿主的 8091（Windows 上更具体的绑定优先）。删掉那个 hostfwd 再重启平台 VM —— guest 访问宿主走 slirp 的 10.0.2.2，**不需要**任何转发 |
| AI 服务报 `Permission denied: '/home/agent/.postgresql/postgresql.key'` | systemd 单元的 `ProtectHome=true` 让 asyncpg 的客户端证书探测返回 EACCES。DSN 里加 `?ssl=disable`（本地 PG 不需要 SSL，仓库默认已加） |
| 沙箱 VM 起来就 kernel panic / `Attempted to kill init` | 沙箱镜像里的 python 标准库不全（`python3-minimal` 没有 ctypes→PID 1 直接死）。镜像必须装完整 `python3`；构建脚本的 chroot 自检会在构建期就拦住 |
| `./deploy/....sh: Permission denied`（exit 126） | **Windows 打的 tar 包没有可执行位**。provisioning 的 runcmd 已经 `find -name '*.sh' -exec chmod 0755` 兜住了；手工解包要自己 `chmod +x` |
| `set: pipefail: invalid option name` | `.sh` 被存成了 CRLF（`pipefail\r`）。转成 LF：`python var/normalize_lf.py`，`tests/unit/test_file_hygiene.py` 会一直盯着 |
| 改了代码但 VM 里行为没变 | VM 里的 `/opt/agentbox/app` 是 provisioning 时的快照。用 `.\deploy\windows\push-repo-to-vm.ps1`（`-RestartAi` 顺带重启服务）；改了 `src/agent/sandbox/*` 还要在 VM 里**重建沙箱镜像** |
| 串口登录 `Login incorrect` | 串口会回显输入，别用"回显即成功"判断登录；`vm-console.ps1` 已用 printf 输出与输入不同的字符串来探测真实 shell |
| 构建卡在 `Retrieving ...` 不动 | debootstrap 没有下载超时（一个卡住的 TCP 连接能挂住整晚）。构建脚本现在用 `timeout` + 三个镜像回退；卡住的残留 chroot 会在下次构建时自动清理 |
| `agent doctor` 里 postgresql/llm 显示 FAIL | 那些检查在**平台 VM 里**才有意义，宿主上跑会标成 `n/a`。进 VM 里跑 `agent doctor` 才是那几项的真实结果 |
| `fetch-sandbox-image.ps1` 报"文件正被另一进程使用" | 运行中的沙箱 VM 占着 `rootfs.img`/`workspace-blank.qcow2`。加 `-StopControlPlane`（或先 `stop-agent.ps1`）再拉 |
| **重建了沙箱镜像但行为没变** | 两个原因：① 上面的文件锁导致 `rootfs.img` 根本没被替换（加 `-StopControlPlane`）；② 控制平面池里的 VM 跑的还是旧镜像 —— **换镜像后必须重启控制平面**。overlay 会按 base 指纹自动作废（`create_overlay`），不需要手工删 `var/sandbox/sessions` |
| 沙箱里 `id` 只显示数字、没有 `sandbox` 名字 | 镜像构建时 `useradd -g 1000` 因 GID 1000 的组不存在而失败（曾被 `|| true` 吞掉）。现在构建脚本先 `groupadd`、再 `useradd`、并用 `getent passwd sandbox` 校验，失败即中断 |
| 内存限制没生效（分配超过限额还成功） | 命令必须既受 cgroup v2 也受 `RLIMIT_AS` 约束。看 `sandbox.info` 的 `cgroup_last_error` / 执行结果里的 `limits.cgroup`；`exec.run` 现在可用 `memory_mb`（32-4096）显式调预算 |
| 跑 `-m sandbox` / `-m pg` 报 `got Future attached to a different loop` | module 作用域的异步 fixture 与"每测试一个事件循环"冲突。两个套件已用 `pytest.mark.asyncio(loop_scope="module")` + `loop_scope="module"` 固定到同一循环 |
| `-m pg` 全被跳过 | 它要求环境变量 `AGENT_DB_URL`。**必须指向测试库**（`*_test`）：这个套件会删表并把核心工具的向量重写成 hash 桩，指到生产库会让语义检索静默失效。`export AGENT_DB_URL="postgresql+asyncpg://agent:agent@127.0.0.1:5432/agentbox_test?ssl=disable"`（PG 只在平台 VM 内可达，所以在 VM 里跑） |
| `-m pg` 报"refusing to run against 'agentbox'" | 守卫生效了（见上一行）。确实要跑生产库就加 `AGENT_PG_ALLOW_PROD=1`，跑完记得 `agent db reembed` 重算向量 |
| 仓库根目录冒出一堆空目录 `tmp*/`、`pytest-of-*/` | 这台机器的 `TEMP` 落在工作区里、而 pytest 默认不删自己的临时目录。已在 `pyproject.toml` 固定 `--basetemp=var/pytest-tmp`（`var/` 被 gitignore）并设 `tmp_path_retention_policy = "none"`；清历史残留：`Get-ChildItem -Directory -Filter 'tmp*' \| Remove-Item -Recurse -Force; Remove-Item -Recurse -Force pytest-of-*` |
| 检索/向量相关的命令卡很久（尤其报 `couldn't connect to huggingface.co`） | `HF_HOME`/`HF_ENDPOINT` 没进进程环境，huggingface_hub 去连不可达的官网、每个文件重试 5 次。现在 `agent.config` 导入时会自动把 `.env` 里的 `HF_*`/代理变量注入 `os.environ`；模型缓存在 `/opt/agentbox/models`（`agent` 用户可读）。排查时可加 `HF_HUB_OFFLINE=1` 让它**立刻失败**而不是死等 |
| `RuntimeError: NumPy was built with baseline optimizations: (X86_V2)` | guest CPU 型号太老。`-cpu qemu64` 没有 SSE4.2，最新 numpy 等 wheel 直接拒绝加载。平台/沙箱 VM 现在默认 `Nehalem`（WHPX 实测可用且带 SSE4.2）；确认方法：VM 内 `grep -o sse4_2 /proc/cpuinfo` |

## 已知限制

* **WHPX 必须用 `-cpu qemu64`**：Windows 上 WHPX 不能虚拟化 `-cpu host`/`-cpu max`（guest 立刻以
  `WHPX: Unexpected VP exit code 4` 死掉，表现为 QEMU 进程 CPU 不涨、串口日志 0 字节）。代码里的默认值
  已经按加速器选好，可用 `AGENT_SANDBOX_CPU` 覆盖（如 `Nehalem`）。
* **没有嵌套虚拟化**：沙箱 VM 不能跑在 Debian 平台 VM 里面（那会退化成 TCG）。要单机全栈，请把 Debian 装成实机或在 Hyper-V 里开嵌套虚拟化，然后用 `deploy/systemd/agentbox-control.service` 在 Debian 内同时跑控制平面。
* **pgvector 维度固定 1024**：换 embedding 模型必须同维度，`config.py` 会在启动时拒绝其它值。
* **本地 BGE-M3 需要额外依赖**：`pip install -e ".[local-embed]"`（会拉 torch，数 GB）；不装则自动降级为关键词检索，功能不中断。
* 仅 x86_64 guest；沙箱镜像构建需要能访问 Debian mirror（构建期联网，运行期断网）。
* 平台 VM 的 `preseed/late_command` 里 `curl install-platform.sh` 依赖安装期网络；即使失败也不影响安装，
  之后手动 `sudo install-platform.sh` 即可。
* `deploy/windows/*.ps1` 需要用 UTF-8 **with BOM** 保存（PowerShell 5.1 否则按 GBK 解码中文）；
  `iso_extract.py` 只解析 ISO9660 主卷描述符，对非 Debian 的异构 ISO 可能找不到安装器内核。
* 不做多模态、不做 Web UI、不做工具市场/签名分发、不做 GPU 直通。
* `py.run` 的结果上限 1 MiB，大结果必须由工具写进 `/workspace` 再让模型用 `fs.read` 分段取。

---

## 附：打包 / 搬到另一台 Windows 机器（`packaging\`）

> 本节由打包工作新增（只追加，不改上文）。细节和实测数字都在 [`packaging/README.md`](packaging/README.md)。

`packaging\` 下三个脚本：

| 脚本 | 什么时候跑 | 作用 |
| --- | --- | --- |
| `make-bundle.ps1` | 源机器 | 打包成 `dist\agentbox-<版本>-<时间>[-lean|-fat].zip`，附 `MANIFEST.sha256` + `SOURCE-COMMIT.txt` |
| `setup-agentbox.ps1` | 目标机器 | **一键**：预检 → `.venv` + 依赖（有 wheelhouse 就全离线）→ `.env` → 平台 VM → 模型 → 起服务自检 → `已就绪/未就绪` |
| `install.ps1` | 目标机器 | 只做 Windows 侧：`.venv` + 依赖 + `.env` + 体检（`-Check`） |

实测体积（2026-10，agentbox 0.1.0）：代码包 **494.9 KB**；`-Lean`（+ wheelhouse 37 MB +
精简 QEMU 210 MB + 沙箱镜像 1.42 GB）**1.52 GB**；`-Fat`（+ 平台磁盘 10.64 GB + ISO 756 MB）**~13 GB**。

搬到新机器的两条命令：

```powershell
Expand-Archive .\agentbox-0.1.0-<stamp>-lean.zip -DestinationPath C:\agentbox
cd C:\agentbox
powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Check   # 先体检（不改动）
powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Yes     # 一键装
```

之后只需要：把 DeepSeek key 填进 `.env` 的 `AGENT_LLM_API_KEY=`，然后
`powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1`。

**打包协议（硬要求）**：`make-bundle.ps1` 只在**工作树干净**时打包（`git status --porcelain`
里不能有 tracked 改动、`src/tests/deploy` 下不能有未跟踪文件），否则中止并列出问题 ——
防止"另一个代理正在改源码，包里那份和仓库对不上"。包内三处记录来源 commit：
`SOURCE-COMMIT.txt`（包根）、`BUNDLE-README.md` 顶部、`MANIFEST.sha256` 头部注释。
`-DryRun` 可以只复核打包结果而不产出 zip。

**离线覆盖不到**：Python 解释器本身、QEMU、`var\vm_key`（SSH 私钥，永不进包）、
平台 VM 的安装期联网（apt/pip）、以及只能在 Linux/VM 内构建的沙箱镜像。

