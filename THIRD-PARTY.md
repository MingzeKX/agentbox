# 第三方组件与许可（THIRD-PARTY）

交付包（`make-bundle.ps1 -Slim`）里**只有本仓库的代码 + 文档 + Python wheel**。
下面这些第三方东西**不在包里**，由接收方在安装时从公开源自己获取。

## 1. 不随包分发的东西（`-Slim` 强制排除）

| 东西 | 许可 | 从哪来 |
| --- | --- | --- |
| QEMU for Windows（`qemu-system-x86_64.exe`、`qemu-img.exe`、DLL、固件） | QEMU 项目自己的许可（GPLv2） | 自己装：<https://www.qemu.org/download/#windows>（默认装到 `C:\Program Files\qemu`），然后在仓库 `.env` 里写一行 `AGENT_QEMU_DIR=C:\Program Files\qemu`（**不要加引号**），或把该目录加进系统 PATH，或把安装目录拷成 `<仓库>\qemu\` |
| 沙箱镜像 `var\sandbox\`（`rootfs.img`、`vmlinuz`、`initrd.img`、`workspace-blank.qcow2`） | 镜像内是 **Debian** 组件（各自许可，多为 GPL/LGPL/BSD/MIT）+ Linux 内核（GPLv2） | 第一次安装时在平台 VM 里用 `deploy/sandbox/build-sandbox-image.sh` 现建（debootstrap + apt + pip，需要联网，约 5-10 分钟），再用 `deploy\windows\fetch-sandbox-image.ps1` 拉回宿主 |
| 平台磁盘 `var\platform\platform.qcow2`（只有 `-Fat` 包带） | 同上（Debian + 内核） | `deploy\windows\fetch-platform-image.ps1` 下 Debian 云镜像，再 `provision-cloud-vm.ps1` |
| Debian 安装 ISO（只有 `-Fat` 包带） | Debian 自己的许可 | <https://www.debian.org/download> |
| 模型权重（`BAAI/bge-m3`、faster-whisper 的 `small`/`base`） | 以模型卡为准（两者通常标 MIT） | 安装时在平台 VM 里下载（默认 `HF_ENDPOINT=https://hf-mirror.com`） |
| Python 解释器本体 | PSF-2.0 | <https://www.python.org/downloads/windows/> |

一句话：**QEMU、VM 镜像里的 Debian 组件、模型权重都由接收方从公开源自己获取，本包没有重新分发它们**，
所以 `-Slim` 包不承担这些组件的再分发义务。

> `-Lean` / `-Fat` 是内部预设：它们**会**把 QEMU 和镜像一起打进去，只适合内部使用，不要对外交付。

## 2. 随包分发的 wheel（`wheelhouse\`，全是宽松许可）

运行时依赖（`pyproject.toml` 的 `dependencies`）：

| 包 | 许可 |
| --- | --- |
| fastapi、pydantic、pydantic-settings、SQLAlchemy、jsonschema、rich、pgvector | MIT |
| uvicorn、httpx、prompt_toolkit | BSD-3-Clause |
| asyncpg | Apache-2.0 |

构建/测试工具（离线建 venv、跑测试用）：hatchling、editables、pip、setuptools、wheel、pytest、ruff（MIT）、
pytest-asyncio（Apache-2.0）。

传递依赖（starlette、anyio、h11、httpcore、idna、sniffio、typing-extensions、greenlet、certifi、click、
watchfiles 等）各自保留自己的许可，也都是 MIT / BSD / Apache-2.0 / PSF-2.0 / MPL-2.0 这类宽松许可；
准确信息以 wheel 里的 `*.dist-info/METADATA` 为准（`python -m pip show <包名>` 也能看到）。

## 3. 本仓库自己的代码

仓库里**没有 LICENSE / COPYING 文件**：作者保留所有权利（all rights reserved）。
`pyproject.toml` 里的 `license` 字段只是打包元数据，不构成对外授权；要商用/再分发请联系作者。
