# agentbox 打包 / 离线安装（`packaging\`）

这个目录回答一个问题：**怎么把这套东西搬到另一台 Windows 机器上？**

三个脚本，各管一段，都能单独跑：

| 脚本 | 作用 |
| --- | --- |
| `make-bundle.ps1` | 在**源机器**上打包：产出 `dist\agentbox-<版本>-<yyyyMMdd-HHmm>[-lean|-fat].zip` + `MANIFEST.sha256` + `SOURCE-COMMIT.txt` |
| `setup-agentbox.ps1` | 在**目标机器**上的**一键入口**：预检 → 建 `.venv` → 生成 `.env` → 平台 VM → 模型 → 起服务自检 → `已就绪/未就绪` |
| `install.ps1` | 只做 Windows 侧那一段：建 `.venv`、装依赖（可用 wheelhouse 全离线）、生成 `.env`、体检 |

> 只改了 `packaging\`（以及 `README.md` 末尾一节）。`src/`、`tests/`、`deploy/` 不归这里管。

---

## 打包协议：包必须能对应到一个 commit（硬要求）

**为什么**：有一次打包是在"活的"源码树上收的源码 —— 另一个代理同时在改 `src/tests`，
结果包里的那份源码 ≠ 仓库里的源码，出问题无法复现。现在协议是：

1. **只从 git 提交态收源码**。`make-bundle.ps1` 先看 `git status --porcelain`：
   * 有 **tracked 文件被改动/暂存** → 中止并列出（这是最危险的一类）；
   * `src/`、`tests/`、`deploy/` 下有**未跟踪文件** → 中止并列出；
   * `packaging/`、`dist/`、`tmp*/` 之类我们自己的产物**允许**存在（列在 `SOURCE-COMMIT.txt` 的
     `untracked_extras` 里，不影响源码↔commit 的对应关系）。
   真的需要打一个"脏树快照"就显式加 `-AllowDirty`：包名会带 `-dirty`，
   `SOURCE-COMMIT.txt` 里写 `worktree: DIRTY`，**这种包不要当正式交付**。
2. **包内记录来源 commit**（三处，都写完整 40 位 hash + `git describe` + 分支 + 打包时间）：
   * 包根目录的 **`SOURCE-COMMIT.txt`**（还有 wheelhouse 的 Python 版本、脏项清单）；
   * **`BUNDLE-README.md`** 顶部"这份包对应哪份源码"一节；
   * **`MANIFEST.sha256`** 头部的 `#` 注释行。
3. **`-DryRun`（复核用，不产出 zip）**：跑完检查、写好 staging 里的元数据、把
   `SOURCE-COMMIT.txt` 和 MANIFEST 头部打印出来，然后**保留 staging 目录**、不生成 zip。
   用来验证"这份树如果现在打包会打成什么样"，不会误发。
4. **`-IncludePlatform` 时如果平台磁盘正被 QEMU 占用就拒绝拷贝**（拷正在写的 qcow2 会得到坏镜像）：
   先 `deploy\windows\stop-agent.ps1`（或 `stop-agent.ps1`）再打包。

冻结后的正式打包流程（操作员确认"源码冻结"之后）：

```powershell
git status                     # 必须干净
git rev-parse HEAD             # 记下这个 hash
.\packaging\make-bundle.ps1 -Lean -Force     # 或 -Fat
# 交验：
#   1) 包里 SOURCE-COMMIT.txt 的 commit == git rev-parse HEAD
#   2) 解压副本上 powershell -File .\packaging\install.ps1 -Check 跑通
```

> `dist\` 里的旧产物（20261004-1650/1651/1658/1701/1706/1707 那几个）是**协议生效前的中途快照**，
> 已按操作员要求**全部删除**（含 `dist\_staging-*`）。在源码冻结、协议走完之前不会再产出 zip。

---

## 三个预设（实测数字，2026-10，agentbox 0.1.0）

| 预设 | 命令 | 包里有什么 | 实测 zip |
| --- | --- | --- | --- |
| **代码包** | `.\packaging\make-bundle.ps1` | 源码 + `deploy\` + `packaging\` | **494.9 KB**（139 个文件） |
| **`-Lean`（推荐）** | `.\packaging\make-bundle.ps1 -Lean` | 代码包 + **wheelhouse** + **精简 QEMU** + **沙箱镜像** | **1.52 GB**（401 个文件） |
| **`-Fat`（全量）** | `.\packaging\make-bundle.ps1 -Fat` | `-Lean` + **平台磁盘 qcow2** + **Debian ISO** | 见下面"胖包实测" |

细粒度开关（可单独组合）：`-IncludeWheelhouse`、`-IncludeSandbox`、`-IncludePlatform`、`-IncludeIso`、
`-IncludeImages`（= 沙箱 + 平台盘 + ISO，旧名字）、`-IncludeQemu`、`-FullQemu`、`-WheelExtras voice,asr`、`-OutDir`、`-Force`。

### 每块东西到底多大（本机实测）

| 组成 | 实测 | 谁带 |
| --- | --- | --- |
| 源码 + 脚本（`git ls-files`，132 个 tracked 文件） | 1.3 MB | 所有包 |
| `deploy\` | 174.6 KB | 所有包 |
| `packaging\`（三个脚本 + 本文档） | 116 KB | 所有包 |
| `wheelhouse\`（57 个 wheel，含构建后端） | **37.1 MB** | `-Lean` / `-Fat` |
| QEMU **精简白名单** | **209.7 MB / 201 个文件** | `-Lean` / `-Fat` |
| QEMU **全量**（`-FullQemu`） | 1,197.8 MB / 3,385 个文件 | 可选 |
| 沙箱镜像 `var\sandbox\`（4 个文件） | **1.42 GB** | `-Lean` / `-Fat` |
| 平台磁盘 `var\platform\platform.qcow2` | **10.64 GB** | 只有 `-Fat` |
| Debian 安装 ISO | 756.0 MB | 只有 `-Fat` |
| VM 内模型 `/opt/agentbox/models`（bge-m3 + whisper） | ~4.8 GB（**在 qcow2 内部**） | 随平台磁盘走 |
| `qemu\` 全量目录 | 1.17 GB | —— |
| `.env` / `var\vm_key`（密钥） | — | **永远不进包** |

磁盘镜像、ISO、`.whl` 用 *NoCompression* 写进 zip（它们本身已接近不可再压），源码/文档用 *Optimal*，
所以 zip 体积 ≈ 原体积，压缩只花时间不省空间。

### 精简 QEMU 的白名单规则（这是本仓库自己定的，不是 QEMU 官方）

**保留**：

* `qemu\` 根目录**全部 `*.dll`**（114 个 / 141.4 MB）——动态库是一个整体，缺一个就起动不了；
* `qemu-system-x86_64.exe`、`qemu-system-x86_64w.exe`、`qemu-img.exe`（3 个 / 50.4 MB）；
* 许可文件 `COPYING`、`COPYING.LIB`、`README.rst`、`VERSION`（重新分发二进制时应该带上）；
* `share\` 里 **x86 用得到的固件**（37 个 / 16.9 MB）：`bios*.bin`、`vgabios*.bin`、
  `kvmvapic.bin`、`linuxboot_dma.bin`、`multiboot_dma.bin`、`pvh.bin`、`qboot.rom`、
  `efi-*.rom`、`pxe-*.rom`、`edk2-i386*.fd`、`edk2-x86_64*.fd`、`qemu_vga.ndrv`；
* `share\keymaps\` + `share\firmware\`（42 个 / 0.9 MB）、`lib\`（1 个）；
* **实测合计 209.7 MB / 201 个文件**。

**丢掉**：其它架构的 `qemu-system-<arch>*.exe`（aarch64/arm/ppc/riscv/s390x/…）、
`qemu-io/nbd/storage-daemon/ga/edid/uninstall.exe`、`share\` 里 arm/aarch64/riscv/loongarch 的
`*.fd`（光这三个架构就 ~287 MB）、`share\doc\` + `share\icons\`（~29 MB）、`dtb\`、`man\`、`locale\`、
`applications\`。
**实测丢掉 988.1 MB / 3,184 个文件** —— 也就是说精简动作省下 **988 MB**，只保留真正要用的 210 MB。

> 为什么固件必须带：`deploy\windows\*.ps1` 和 `src/agent/control/vm.py` 都没有传 `-L`/`-bios`，
> QEMU 按自己的数据目录（`<exe 目录>\share`）找 BIOS。把 `share\` 整个删掉，QEMU 会
> `Could not open 'bios-256k.bin'` 直接起不来。压缩包里的 QEMU 用
> `-L <解压目录>\qemu\share` 实测能正常冻结启动（见"验证记录"）。

### 胖包（`-Fat`）实测与瘦身建议

`-Fat` 会把 `var\platform\platform.qcow2`（10.64 GB）和 Debian ISO（756 MB）也装进去，
所以它是 **~13 GB 级**的包。它值得打的前提是：目标机器没有网（装不了 apt/pip）、
也不想等 10-40 分钟的 provision。

**瘦身（按省下来的空间排序，都是实测）**：

| 动作 | 省 |
| --- | --- |
| 打包时用精简 QEMU（默认就是；`-FullQemu` 才会多带） | ~988 MB |
| VM 内清缓存后再打：`sudo apt-get clean; .venv/bin/pip cache purge; sudo rm -rf /root/.cache /home/agent/.cache` | 需要自己量（和用过多少有关） |
| 不需要随包带模型：`rm -rf /opt/agentbox/models`（到目标机器再下，走 hf-mirror） | ~4.8 GB |
| `qemu-img convert -O qcow2 -c`（zlib 压缩）平台磁盘 | 见下面实测 |
| 或者干脆别带平台磁盘（用 `-Lean` + `fetch-platform-image.ps1` + `provision-cloud-vm.ps1`） | 10.64 GB |

---

## 用法一（推荐）：`-Lean` 包 + 一键脚本

```powershell
# ① 源机器（能上网）
.\packaging\make-bundle.ps1 -Lean

# ② 把 dist\agentbox-0.1.0-<stamp>-lean.zip 拷到目标机器

Expand-Archive .\agentbox-0.1.0-<stamp>-lean.zip -DestinationPath C:\agentbox
cd C:\agentbox
powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Check   # 先体检，不改任何东西
powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Yes     # 一键装（长任务不再问）
```

`setup-agentbox.ps1` 的 8 步（每一步幂等，可以反复跑）：

1. **预检**：Windows / PowerShell 5.1 / Python / 磁盘 / `qemu\` / `var\sandbox\*` / `ssh.exe` / `wheelhouse`
   —— 每项 `✔`/`✘` + 中文修复建议（`-SkipChecks` 可跳过）；
2. **`.venv` + 依赖**：委托 `install.ps1`，有 `wheelhouse\` 就 `pip install --no-index --find-links wheelhouse -e .`（全离线）；
3. **`.env`**：生成随机 `AGENT_CONTROL_SECRET`、留 `AGENT_LLM_API_KEY=` 占位，并**强制写入 `AGENT_SANDBOX_CPU=Nehalem`**
   （不写的话 WHPX 下的 guest 用老 CPU 型号，`numpy` 等 x86-64-v2 wheel 会拒绝加载）；
4. **平台 VM**：已有 qcow2 且 `var\vm_key` 在 → 跳过；否则
   `fetch-platform-image.ps1` →（有 ISO 且 `-UseIso` 时 `new-platform-vm.ps1`，否则
   `provision-cloud-vm.ps1`）→ `run-platform-vm.ps1 -Headless` → 轮询 VM 内
   `/opt/agentbox/PROVISIONED`；失败会打印 `/var/log/agentbox-install.log` 最后 40 行；
5. **模型**：VM 内下载 `BAAI/bge-m3` + faster-whisper（`HF_HOME=/opt/agentbox/models`、
   `HF_ENDPOINT=https://hf-mirror.com`、**`HF_HUB_DISABLE_XET=1`**、**不传 `download_root=`**），
   再 `chown -R agent:agent` + `chmod -R a+rX`，最后重启 `agentbox-ai`（`-SkipModels` 可跳过）；
6. **沙箱镜像**：随包带了就直接用；没带就打印在平台 VM 里重建的三条命令；
7. **启动 + 自检**：`start-agent.ps1 -NoChat` → `:8091/health`、`:8090/health` →
   `agent.cli sandbox status` 看 `warm=`；
8. **结论**：成功/失败清单 + `已就绪 / 未就绪`（未就绪时退出码 1）。

只想要 Windows 侧（不碰 VM、不下模型）：

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -SkipVm -SkipModels -Yes
```

## 用法二：手动分步（`install.ps1`）

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\install.ps1 -Check   # 体检
powershell -ExecutionPolicy Bypass -File .\packaging\install.ps1          # 建 venv + 装依赖 + 生成 .env
```

其他开关：`-WhatIf` / `-DryRun`（干跑）、`-Force`（重建 `.venv`）、`-SkipEnvFile`、`-Python <路径>`、
`setup-agentbox.ps1` 另有 `-SkipChecks`、`-SkipVm`、`-SkipModels`、`-Yes`、`-UseIso`、
`-VmTimeoutMinutes`、`-ModelTimeoutMinutes`、`-StartTimeoutMinutes`。

---

## 首次运行检查清单（新机器）

1. **Python 3.13+ x64**：安装时勾 *py launcher*，验证 `py -3.13 -V`。
   注意 **PATH 上的 `python.exe` 可能是 Microsoft Store 别名**：执行它返回 9009、"什么都没发生"，
   不是真解释器（这台开发机就是这样，只有 `py` 可用）。脚本会识别并提示。
   `pyproject.toml` 实际要求 `>=3.11`，但 **wheelhouse 和解释器版本强绑定**（见下）。
2. **QEMU**：`<仓库>\qemu\` 里要有 `qemu-system-x86_64.exe` + `qemu-img.exe`。
   `-Lean`/`-Fat` 包里已经带了精简版（209.7 MB）；代码包里没有，要自己装
   （<https://qemu.weilnetz.de/w64/>，或从旧机器 `robocopy qemu\ <新>\qemu /E`，或设 `AGENT_QEMU_DIR`）。
3. **SSH 密钥**：`var\vm_key` + `var\vm_key.pub`。默认包**不含**（私钥属凭据）。
   如果平台磁盘是从别的机器带过来的，磁盘里的 `authorized_keys` 是**那把**旧公钥，
   必须把配对的私钥带过去，或者按第 4 步重建平台 VM（会生成新密钥）。
4. **平台 VM**：`fetch-platform-image.ps1`（Debian 13 云镜像 ~330 MB）→ `provision-cloud-vm.ps1`
   （cloud-init 装 PostgreSQL/pgvector/python，**安装期需要联网**，10-40 分钟）。
   成功标志：串口日志出现 `agentbox provisioning OK`，VM 内存在 `/opt/agentbox/PROVISIONED`。
   备选是 ISO 安装器路径 `new-platform-vm.ps1`（要 `debian-*-netinst.iso`，只有 `-Fat` 包带）。
5. **沙箱镜像**：`-Lean` 包里带了 4 个文件；没有的话必须在**平台 VM 里**重建
   （`deploy/sandbox/build-sandbox-image.sh`，构建期要 Debian mirror），再用
   `deploy\windows\fetch-sandbox-image.ps1` 拉回来。
6. **`.env`**：`install.ps1` 已写好随机 `AGENT_CONTROL_SECRET`；**你必须**把 DeepSeek key 填进
   `AGENT_LLM_API_KEY=`（唯一必须手工做的一步）。
7. **启动**：`powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1`
8. **复检**：`powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Check`

---

## "离线"覆盖不到什么（别过度期待）

| 东西 | 为什么覆盖不到 | 怎么办 |
| --- | --- | --- |
| Python 解释器本身 | 包里只有 wheel，没有 CPython 安装程序 | 自己装 3.13/3.14 x64（和 wheelhouse 版本对齐） |
| QEMU（代码包里） | 1.17 GB 全量太大；`-Lean` 带的是精简版 210 MB | 上游安装包 / 旧机器 robocopy / `AGENT_QEMU_DIR` |
| 平台磁盘 / ISO / 模型权重 | 只有 `-Fat` 才打（+11.4 GB） | 用 `-Fat`，或在目标机器上重建 + 重下 |
| 平台 VM 的**安装期联网** | apt + pip 必须能从镜像源拉包（哪怕只重装一次） | 带了 `platform.qcow2` 就不需要；否则必须有网 |
| 沙箱镜像的**构建** | ext4 镜像只能在 Linux 里做（debootstrap） | 在平台 VM 里构建一次，之后镜像可离线复用（`-Lean` 已带上） |
| VM 内模型权重 | 只在平台磁盘里；或需要 huggingface 镜像 | `-Fat` 间接带；否则在 VM 里下（hf-mirror） |
| `var\vm_key`（SSH 私钥） | 安全规则强制排除 | 手工从旧机器拷（**不要**放进包里） |
| wheelhouse 的 Python 版本 | wheel 名里的 `cp3XX` 绑定解释器 minor 版本 + `win_amd64` | 见下一节，按目标机器的版本重新生成 |

一句话：**这个包覆盖"代码 + Python 依赖 + 沙箱镜像 +（可选）QEMU +（可选）平台磁盘"，
覆盖不了操作系统级的东西（Python、QEMU、私钥）和必须在 VM 内构建/下载的东西。**

## wheelhouse 和 Python 版本的耦合

本仓库现有的 `packaging\wheelhouse\` 是在 **Python 3.14 / win_amd64** 上下载的
（wheel 标签是 `cp314` + `cp310-abi3`）。目标机器如果是 **Python 3.13，这批 wheel 装不上**。

* 目标机器也用 3.14 → 直接 `-Lean` 打包，开箱即用；
* 目标是别的 minor → 在**能上网的源机器**上按目标版本重新下载：

  ```powershell
  .\.venv\Scripts\python.exe -m pip download --dest packaging\wheelhouse --only-binary=:all: `
      --python-version 3.13 --implementation cp --abi cp313 --platform win_amd64 `
      "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27" "pydantic>=2.8" `
      "pydantic-settings>=2.4" "SQLAlchemy[asyncio]>=2.0.30" "asyncpg>=0.29" "pgvector>=0.3.0" `
      "jsonschema>=4.22" "rich>=13.7" "prompt_toolkit>=3.0" `
      "hatchling>=1.24" "editables>=0.3" pip setuptools wheel `
      "pytest>=8.2" "pytest-asyncio>=0.23" "ruff>=0.5"
  ```

  **`editables` 不能少**：`pip install -e .` 在构建隔离环境里要它（hatchling 做 editable 构建时用），
  少了它会离线失败在 `Installing backend dependencies`（实测踩过，`make-bundle.ps1` 现在已经把它加进列表）。
  `--only-binary=:all:` 是故意的：宁可在打包时响亮地失败，也不要在目标机器上现编源码（那需要编译器）。
* `setup-agentbox.ps1 -Check` / `install.ps1 -Check` 会核对 wheel 的 `cp3XX` 标签和本机解释器，
  不匹配就报 `!` 并给出这条修复建议。

## 安全规则（`make-bundle.ps1` 强制）

* **永不进包**：`.env` / `.env.*`（但 `.env.example` 必须进）、`var\vm_key`、
  `*.pem` / `*.key` / `id_rsa*` / `*credential*` / `*secret*`，以及**内容**里出现真实密钥赋值的文件
  （`AGENT_LLM_API_KEY=sk-…` 之类；占位符放过）。被跳过的文件名会打印在
  `因密钥规则被跳过的文件` 一节里。打包结束会再核对一次：`MANIFEST.sha256` 里不应出现 `.env`/`vm_key`。
* 源码清单以 `git ls-files` 为准，所以 `var/`、`.venv/`、`qemu/`、`*.iso`、`dist/`、`.env` 天生不在里面。
* `install.ps1` / `setup-agentbox.ps1` 只**创建** `.env`（已存在就绝不改内容），密钥值从不打印。
* 自查：

  ```powershell
  Select-String -Path .\MANIFEST.sha256 -Pattern '(^|\s)\.env$|vm_key|\.pem$|\.key$'   # 应该没有输出
  ```

## 校验、复现

* 包根目录 **`SOURCE-COMMIT.txt`**：这份包是从哪个 commit 打出来的（完整 hash、`git describe`、
  分支、打包时间、工作树是否干净、脏项清单、wheelhouse 的 Python 版本）。
  出问题请拿这个 commit 复现：`git checkout <hash>`。
* `MANIFEST.sha256`：开头 8 行是 `#` 注释（含 **commit**），后面每行 `<sha256>  <相对路径>`
  （UTF-8 无 BOM，不含自身）。**注释行会让 `sha256sum -c` 报 improperly formatted**，所以：

  ```bash
  grep -v '^#' MANIFEST.sha256 | sha256sum -c -      # Linux
  ```

  Windows 上 `BUNDLE-README.md` 里那段 PowerShell 只用正则匹配合法行，注释自动跳过。
* 每个 zip 旁边有 `<zip>.sha256`，用于传输完整性校验。
* 重新打包 = 重跑 `make-bundle.ps1`（**工作树必须干净**）；源码清单 = 当时的 `git ls-files`。
* `packaging\wheelhouse\`、`dist\`、`_staging-*` 都不进 git（见 `packaging\.gitignore`）；
  `_staging-*` 在打包结束（成功或失败）都会被删掉，只有 `-DryRun` 会故意留着供检查。

## 验证记录（这些数字是跑出来的，不是估的）

* `make-bundle.ps1`（代码包）：139 个文件、494.9 KB；`-Lean`：401 个文件、1.52 GB
  （这两个 zip 是**协议生效前的中途快照，已删除**；下面的行为验证仍然有效，冻结后会重打并复测）。
* `Expand-Archive -LiteralPath <lean.zip>`：**15-33 秒**，解压后 1,697.8 MB / 401 个文件，
  `packaging\*.ps1` 都带 UTF-8 BOM。
* 在解压副本上跑**真实** `install.ps1`（不是 `-Check`）：`wheelhouse` 57 个 wheel、
  `pip install --no-index --find-links wheelhouse -e .` 成功（**全程无网**），
  生成 `.venv`（110.7 MB，11 个依赖 + `agent.exe`）和 `.env`；退出码 1 只是因为
  平台磁盘 / SSH 私钥 / API key 还没准备（正是该报的 3 项）。
  这一步暴露出 `editables` 缺失（`Installing backend dependencies` 失败）——已修：wheelhouse 现在带它，
  `install.ps1` 也加了 `--no-build-isolation` 兜底重试。
* 打包协议（新）实测：
  * 脏工作树（`M src/agent/ai/history.py` 等 4 项）+ 不给 `-AllowDirty` → **拒绝打包，退出码 1**
    并逐项列出；
  * `-AllowDirty -DryRun` → 写出 `SOURCE-COMMIT.txt` / `MANIFEST.sha256` 头部
    （commit `9ae7fb9c…`、`describe 9ae7fb9-dirty`、`worktree DIRTY`），**不产出 zip**（`dist\` 里 0 个 zip）；
  * `-IncludePlatform -DryRun` 而平台磁盘正被 QEMU（pid 9220）占用 → **拒绝拷贝，退出码 1**。
* `tests\unit\test_file_hygiene.py`：**152 passed**（BOM 检查覆盖 `packaging\*.ps1`）。
  注意：`edit` 工具会吃掉 BOM，改完 `.ps1` 必须补 BOM 再跑测试（本仓库的 hygiene 测试会抓）。
* QEMU 精简白名单的固件是否够用：
  * **决定性证据**：从 `qemu-system-x86_64.exe` 里抽出它引用的固件文件名 —— 26 个，
    其中 25 个在完整 `share\` 里真实存在，而这 25 个**在精简集合里一个不缺**（缺失 0 个）；
    丢掉的只是其它架构的 `*.fd`（aarch64/arm/riscv/loongarch）和文档。
  * 启动探针（`-S -machine q35`）在这台机器上**不能作为证据**：它同时也装了
    `C:\Program Files\qemu`，所以就算 `-L` 指向空目录，QEMU 仍可能回退到系统固件而"看起来正常"。
    在目标机器上要确认，就正常起一次沙箱 VM（`start-agent.ps1` 第 5 步）。
* `qemu-img convert -O qcow2 -c`（平台磁盘 10.64 GB）实测：
  * **zlib（`-c`）**：输出 **5,410.6 MB**（比例 0.497），耗时 **805 秒**，省 **5.35 GiB**；
  * **zstd（`-c -o compression_type=zstd`，QEMU 11.1.0 支持）**：输出 **5,349.4 MB**（比例 0.491），
    耗时 **106 秒**，省 **5.41 GiB** —— 比 zlib **快 7.6 倍**且略小，推荐用它。
    也就是说 `-Fat` 包里的 10.64 GB 平台磁盘可以压到 ~5.35 GB（代价：VM 读盘多一层解压，
    写回会重新膨胀；对磁盘做这个操作前先备份）。

