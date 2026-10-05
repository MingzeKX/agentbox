# agentbox 使用手册（操作员版）

> 这份文档写给**用工具的人**，不写给读源码的人。
> 技术内部结构看 [`README.md`](README.md)；这一版**做了什么、没做什么、有什么限制**看
> [`RELEASE-v1.md`](RELEASE-v1.md)（§3 未验证项、§4 已知限制）。本手册不重复它们的内容。

---

## 1. 一句话是什么

**agentbox 是一个"AI 在虚拟机里干活"的工具**：你跟它对话，它自己查工具、写工具、
在沙箱里跑命令，然后把结果原样拿回来给你看。你的 Windows 桌面本身不会被它乱动。

三个组件（各在一个地方，出问题时先想清楚是哪一个）：

| 组件 | 在哪 | 干什么 |
|---|---|---|
| **Windows 控制平面**（:8091） | 你这台 Windows | 唯一能起/停 QEMU 沙箱 VM 的地方，管 VM 池、磁盘 overlay、资源限额 |
| **平台 VM 的 AI 服务**（:8090） | Debian VM（QEMU，宿主上跑） | 对话循环、调 LLM、检索工具库、PostgreSQL + pgvector |
| **沙箱 VM** | 独立 QEMU VM | **真正执行** AI 写的代码/命令：只读根、非 root、无网卡、cgroup 限额 |

```
你 ── chat ──► 平台 VM 的 AI 服务 :8090 ──► 控制平面 :8091 ──► 沙箱 VM（干活的地方）
```

**离线边界（一句话）**：核心是离线可用的，但**首次要把平台 VM 建出来、并在 VM 里构建沙箱镜像/
下载模型**时需要联网；装好之后日常对话只需要能连到 LLM 网关。细节见 RELEASE-v1.md §5。

---

## 2. 安装（`-Slim` 包，三步）

拿到的是 `dist\agentbox-*-slim.zip`（旁边有 `.sha256` 可以校验）。**在三步之前先解压 + 装 QEMU**：

```powershell
# 0) 解压到一个没有中文/空格的路径，例如 C:\agentbox
Expand-Archive .\agentbox-0.1.0-*-slim.zip -DestinationPath C:\agentbox
```

**先装 QEMU**（`-Slim` 包里**不含** QEMU，它是 GPLv2 的第三方程序，见 `THIRD-PARTY.md`）：
官方下载页 <https://www.qemu.org/download/#windows>，安装器默认装到 `C:\Program Files\qemu`。
然后**二选一**让脚本找到它：

* **推荐**：在仓库根 `.env` 里写一行 `AGENT_QEMU_DIR=C:\Program Files\qemu`（**不要加引号**）；
* 或者把 `C:\Program Files\qemu` 加进系统 PATH（新开终端里 `qemu-system-x86_64.exe --version` 能出版本号）。

```powershell
# 1) 一件装好：预检 → venv（离线 wheelhouse）→ 生成 .env → 建平台 VM → 下模型
#    → **在平台 VM 里现建沙箱镜像**（需要联网，约 5-10 分钟）→ 自检
cd C:\agentbox
powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1
# 只想体检、不动任何东西：加 -Check
```

```powershell
# 2) 把 LLM 的 key 填进 .env（就在解压出来的根目录）
notepad .env
#    AGENT_LLM_API_KEY=sk-......      <-- 必填
```

```powershell
# 3) 启动并对话
powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1
```

### 必须设的环境变量

这两个写进**根目录的 `.env`**（setup 已经建好，改完重启服务生效）：

| 变量 | 值 | 为什么 |
|---|---|---|
| `AGENT_SANDBOX_CPU` | `Nehalem` | WHPX 下必须是有 SSE4.2 的型号，否则 guest 里 numpy 等现代 wheel 直接拒绝加载。`host`/`max` 在 WHPX 上会挂 |
| `AGENT_SANDBOX_NET_MODE` | `off`（默认）/ `full` | `off` = 沙箱**没有网卡**（最安全）；`full` = 给沙箱网卡（只出不进），`ping`/DNS/`apt-get update` 才能用 |

**离线边界**：`-Slim` 包里只有**代码 + Python wheel（宽松许可）**；QEMU、沙箱镜像里的 Debian 组件、
模型权重、Python 本体都由接收方在安装时从公开源自己取（明细见 `THIRD-PARTY.md`）。
`var\vm_key`（SSH 私钥）**不会**打进包，由 setup 现场生成。平台 VM 的安装期、以及沙箱镜像与模型
需要在 VM 内构建/下载时**要联网**（首次构建沙箱镜像约 5-10 分钟）。

---

## 3. 启动 / 停止

```powershell
# 启动（幂等，已经在跑的东西不会重复启动）→ 最后进交互对话
powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1

# 常用变体
... -NoChat                              # 只把环境拉起来，不进对话
... -Message "在沙箱里跑 uname -a"        # 跑一条就退出
... -ForceRestartVm                      # 平台 VM 卡住时强制重启
```

```powershell
# 优雅停止：让 VM 干净关机（qcow2 不会脏）
powershell -ExecutionPolicy Bypass -File .\deploy\windows\stop-agent.ps1
... -Force                               # 确实卡死了才强杀
```

两个脚本都会：

* 屏幕上给**横幅 + 原地刷新的进度条 + 结尾的步骤耗时表**（哪一步慢一眼就看出来）；
* 把这次运行的**完整日志**写进 `var\logs\start-<时间戳>.log` / `var\logs\stop-<时间戳>.log`
  —— 出问题**先看这个文件**，它含每一步的中间状态和耗时；
* 输出被重定向（写日志/CI）时自动降级成整行文本，不会出现 `\r` 垃圾。

> **启动顺序有讲究（已修）**：控制平面**先**起，AI 服务后起。
> 反过来的话 AI 服务的 `/health` 会同步去探测控制平面、白等一个超时，
> 于是"第一步就卡住"。

> **把平台 VM 里的服务开给宿主（默认关闭，只绑回环）**：在根目录 `.env` 里写
> `AGENT_PLATFORM_PORTS=2121,30000-30010`（逗号分隔的单个端口或闭区间），重启平台 VM 后
> 就能在宿主上按 `ftp://127.0.0.1:2121` 访问 VM 里的 FTP/HTTP/…；屏幕上会打印
> `VM port -> host 127.0.0.1:port` 的对照表。写上绑定地址会被直接拒绝（要绑别处请自己改
> `run-platform-vm.ps1`），**445 / 139 也会被拒绝**（Windows 宿主自己占着 SMB，请换宿主端口，
> 例如 8445）。**沙箱 VM 的隔离没有变化 —— 这只影响平台 VM。**

---

## 4. 对话与全部控制台命令

进对话后，**以 `/` 开头的行是指令**（不是发给模型的消息）；普通文本才是消息。
按 **Tab** 补全（指令名、子命令、persona 名都有候选），`/help` 看总览。

| 指令 | 说明 | 例子 |
|---|---|---|
| `/help` | 命令总览；**`/help <命令>` 看单条详解** | `/help persona` |
| `/clear` | 清屏（不改变当前会话），别名 `/cls` | `/clear` |
| `/new` | 开新会话（下一条消息才真正创建），历史不串味 | `/new` |
| `/session` | 打印当前会话 id | `/session` |
| `/exit` | 离开控制台（别名 `/quit`） | `/exit` |
| `/net` | 联网开关 / 白名单 / 端口 / SSRF 防护 | `/net on`、`/net allow *.example.com`、`/net ports 80,443` |
| `/perm` | 权限档位 `safe` / `trusted` / `unrestricted` | `/perm trusted`；危险档要打短语 `/perm unrestricted ENABLE-HOST-EXEC` |
| `/persona` | 列出人格 + 当前生效的那个 | `/persona whale`；`/persona off` 取消扮演 |
| `/log` | 工具日志渲染：`ide` / `plain` / `json`，行数、参数、折叠 | `/log json`、`/log lines 40`、`/log fold off` |
| `/more` | 重看最近一次工具结果的**完整**输出 | `/more`、`/more answer`、`/more 2` |
| `/tools` | 注册表工具的列表 / 退休 / 删除（不写 `.env`） | `/tools`、`/tools retire my_tool` |
| `/model` | 看/切回答模型（**只有 `deepseek-flash` 能看图**） | `/model deepseek-flash` |
| `/think` | 思考强度 `low` / `high` / `max`（**该网关效果不稳定**） | `/think high`、`/think default` |
| `/image` | 把本地图片附在下一条消息上（最多 4 张，自动切视觉模型） | `/image a.png 这是什么颜色？` |
| `/voice` | 把本地录音转文字，并作为下一条消息发给 agent | `/voice C:\tmp\ask.wav` |
| `/voice-mode` | 实时语音：`on` / `hands-free` / `off` / `status` | `/voice-mode on` |
| `/voice-devices` | 列出麦克风 + `sounddevice`/朗读是否可用（序号可填进 `AGENT_VOICE_INPUT_DEVICE`） | `/voice-devices` |
| `/speak` | 回答是否朗读（Windows 自带 SAPI，只影响这个终端） | `/speak off` |
| `/config` | 所有运行时可改项（改完**立即生效并写入 VM 的 `.env`**） | `/config`、`/config tool_extra_modules smtplib` |
| `/save` | 手动再把当前值同步一次到 VM 的 `.env`（正常不需要） | `/save` |
| `/sandbox` | 沙箱池状态（VM 列表 / 加速器 / 预热数） | `/sandbox` |
| `/get` | 把沙箱里 AI 生成的文件/图片取到本机 `var\pulled\`（图片会自动打开） | `/get /workspace/plot.png`、`/get /workspace/out.csv report/a.csv` |

> **`/get` 取文件**（AI 侧对应的工具是 **`fs.pull`**）：读 `sandbox.invoke` → 来宾
> `fs.read`，写控制面 RPC **`host.pull.write`**，**不经过 AI 服务的 `/admin/config`**，
> 所以 AI 服务挂了也能取。只允许 `/workspace` 下的文件，单个文件上限 **8 MB**
> （`AGENT_FS_PULL_MAX_BYTES`）；目标名只能是 `var\pulled\` 下的相对路径
> （不允许 `..` 或绝对路径，子目录会自动建）；不写目标名时默认自动改名避免覆盖
> （`acg.jpg` → `acg-2.jpg`），要覆盖同名就加 `--overwrite`。
> 图片（png/jpg/jpeg/gif/webp/bmp）取完后用默认看图程序打开。AI 自己生成图片/文件后
> 也可以直接调 `fs.pull`，效果一样。

> 自定义 persona：把 `<name>.md` 放进仓库根的 `personas\`（同名会覆盖内置）。
> 人格名只允许 `[a-z0-9_-]{1,32}`，`..\..\etc\passwd` 这种会被拒并降级。

---

## 5. 语音

```powershell
.\.venv\Scripts\python.exe -m agent.cli chat --voice     # 直接进语音模式
... --voice --no-speak                                   # 只识别、不朗读
```

* **默认是"推按式"**：在空提示符上**按回车**开始说话（或者在正在输入时按 **Ctrl+T** / F2），
  说完停顿一下自动断句 → 识别 → 当作你的下一条消息。
  **只按一下空格不会开始录音**（它就是空格），这是故意的。
* **`hands-free`**（`/voice-mode hands-free`）才是麦克风常开；用 Ctrl-C 退出。
  默认不常开是因为：否则助手会把自己的朗读录回去。
* **朗读可以打断**：朗读中**按任意键**立即停止并回到聆听。
* **Ctrl-C 取消当轮**：agent 正在生成时按 Ctrl-C 只取消这一轮，回到提示符继续，**不会退出**控制台。
* **设备不对**：先 `/voice-devices` 看序号，再把序号（或名字的一部分）填进 `.env` 的
  `AGENT_VOICE_INPUT_DEVICE`；留空 = 系统默认设备。
* **需要麦克风**。⚠️ **本机没有麦克风，所以真机语音（采集/朗读/打断）未在无麦克风机器上验证**，
  见 `RELEASE-v1.md` §3。没装 `sounddevice` / 没麦克风 / 服务不可用都会给中文提示，不会抛异常。

---

## 6. 图片 / 模型 / 思考强度

* **图片**：`/image a.png 这是什么颜色？` —— 图片 + 问题一起发出去。
  只写 `/image a.png` 会自动补一句"请描述这张图里有什么。"。
  支持 png/jpg/jpeg/webp/gif，最多 4 张；用 `/images` 看排队中的图片，`/image-clear` 清空。
  **带图的那一轮会自动切到视觉模型 `deepseek-flash`**；纯文字仍用 `/model` 选的模型。
* **模型**：`/model` 看当前模型和可选列表，`/model deepseek-flash` 切换。
  只有 `deepseek-flash` 能看图；模型名不在列表里会被拒绝（免得切到不存在的模型）。
* **思考强度**：`/think low|high|max`，`/think default` 清空。
  **⚠️ 写明：`effort` 在当前网关上效果不稳定** —— 实测思考 token 中位数 low ≈ 104 /
  high ≈ 156 / max ≈ 124，波动比档位差别还大，传无效值网关也照收。
  请当成实验开关，不要指望"档位越高越好"。见 `RELEASE-v1.md` §4。

---

## 7. 让它自己造工具 + 白名单三档

AI 可以自己写新工具，但要过**四道门**：

```
G0 清单校验   名字/描述/tags/参数 schema/声明的权限/源码大小，必须存在 def run(args)
G1 静态检查   在沙箱内跑 AST 白名单：能 import 什么、禁 eval/exec/open/dunder 等
G2 沙箱测试   作者要自带 ≥3 个用例（成功/边界/错误），每个独立进程 + 超时，全绿才继续
G3 注册       算 embedding → 插新版本 → 旧的同版本 retire
```

**白名单三档**（决定 G1 放行哪些 import，可用 `/config` 运行时改）：

| 档位 | 放行 | 什么时候用 |
|---|---|---|
| `strict`（默认） | 只有一小撮纯计算/数据模块 | 默认、最省心 |
| `extended` | + `os` `sys` `time` `pathlib` `subprocess` `smtplib` `email` `sqlite3` … | 需要真脚本能力时 |
| `unrestricted` | 任何模块 | **别用**，除非你清楚后果 |

**被拒绝时，拒绝信息自带修复命令**，例如：

```text
... ; to allow it: /config tool_extra_modules smtplib   (only this module)
    or /config tool_import_profile extended   (the usual scripting set)
```

于是**一行放宽**就够了：

```text
/config tool_extra_modules smtplib        # 只多放 smtplib 这一个模块
```

`/config tool_allow_open on` 是另一件事：它放开工具里的 `open()`，
**代价**是模型现写的代码可以直接读写 `fs.*` 权限范围内的文件——
只在确实需要、并且你愿意承担这个面的时候开。

---

## 8. 删除工具（`/tools`）

```text
/tools                                 列出注册表里的工具（按 tier 分组，标出 retired）
/tools retire <名称> [版本]             退休一个版本（默认当前 active 版本）
/tools delete <名称> [版本]             删除一个版本
/tools delete <名称> [版本] --purge     硬删除（连带历史记录），需要把工具名原样打一遍确认
```

* **`retire` 是可逆的**：工具**检索不到**了（agent 不会再召回、也调不到），
  但**台账/历史记录保留**，以后能看出它曾经存在、被谁调过。
* **`delete --purge` 是真删**：不可恢复，所以要你**打名字确认**。
* **谁有权删**：agent（模型）**只能删自己写出来的工具**；
  核心工具（`fs.*` / `exec.run` / `sandbox.*` / `toolsmith.*`）会被拒。
  操作员在控制台里用 `/tools` 不受这个限制。
* `/tools` 系列**只动服务端注册表，不写 `.env`**；retire/delete 立即影响 agent 能搜到什么。

---

## 9. 权限分级与宿主执行

三档**策略上限**（与单个开关叠加生效，两级都会检查）：

| 档位 | 允许什么 |
|---|---|
| `safe`（默认） | 只有沙箱工具：`fs.*` `exec.run` `sandbox.*` `toolsmith.*` |
| `trusted` | + 受防火墙管制的 `net.*`、`mcp.*` |
| `unrestricted` | + **`host.exec`：在你这台 Windows 上执行命令** |

越档调用直接返回 `tier_denied`；`search_tools` 也只显示当前档位能用的工具。

**`host.exec` 的风险**：它执行的地方是**宿主**（不是沙箱），也就是你的桌面 ——
能干的事和你在 PowerShell 里手敲一样多。开启条件：控制平面侧 `AGENT_PERMISSION_TIER=unrestricted`
**加上**在控制台逐字输入短语（默认 `AGENT_HOST_EXEC_PHRASE=ENABLE-HOST-EXEC`）：

```text
/perm unrestricted ENABLE-HOST-EXEC
```

每一次执行都会进 `tool_runs` 台账、日志里是 WARNING 级。用完建议 `/perm safe` 收回来。

---

## 10. 排错速查表

| 现象 | 怎么办 |
|---|---|
| **VM 卡住**（没反应、串口不动） | 先看有几台 QEMU：`Get-Process qemu-system-x86_64`。**只有平台 VM 有 `hostfwd`**（SSH 2222、:8090、:8099）；沙箱 VM **不该**监听任何宿主端口。卡住了 `stop-agent.ps1` 再 `start-agent.ps1` |
| **端口被占** | `netstat -ano \| findstr 8091` 看是谁。**给平台 VM 加过 8091 的 hostfwd 就会抢走宿主的 8091**（Windows 上更具体的绑定优先）—— 删掉那个 hostfwd 重启平台 VM；guest 访问宿主走 slirp 的 `10.0.2.2`，**不需要**任何转发 |
| **`:8090` 很慢才算通** | 冷启动/模型加载。启动顺序已经修好（控制平面先起），慢主要来自平台 VM 冷启动 + AI 服务里 PostgreSQL 与 embedder 就绪；`start-agent.ps1` 的耗时表能看出慢在哪一步 |
| **会话 400（已修）** | 以前一个工具调用崩了（控制平面不可达）会只存"声明"不存"结果"，把会话毒化，之后**永久** 400。**现在已修，不用再 `/new` 绕**；真遇到 4xx 会透出网关原文且**零工具调用** |
| **HF 镜像两个坑** | ① 下不动就加 `HF_HUB_DISABLE_XET=1`；② **不要**用 `download_root=`（会绕开共享缓存，VM 里重复下 4.8GB） |
| **`numpy was built with baseline optimizations (X86_V2)`** | guest CPU 型号太老。设 `AGENT_SANDBOX_CPU=Nehalem`（WHPX 实测可用且有 SSE4.2）；确认：VM 内 `grep -o sse4_2 /proc/cpuinfo` |
| **`apt-get install` 失败** | **受只读根限制**（`apt-get update` 在 tmpfs 修好后可用，但装包需要可写根，属 v1.1）。要装 Python 包请走 `net.fetch` 下 wheel + `pip install --target` 搬进 `/workspace` |
| **语音没设备** | `/voice-devices` 看列表；空列表 = 没麦克风或没装 `sounddevice`（`.\.venv\Scripts\pip.exe install sounddevice numpy`）。序号填进 `AGENT_VOICE_INPUT_DEVICE` |

---

## 11. 已知限制

一句话：**沙箱的隔离很强，但代价是有些事现在还做不了** ——
`apt-get install` 受只读根限制、思考强度 `effort` 在该网关不稳定、无 GPU、
语音与部分功能未在真机验证、MCP 只支持 Streamable HTTP。
**完整清单（含"哪些没在真机验证"）请看 [`RELEASE-v1.md`](RELEASE-v1.md) §3 和 §4**，这里不重复。

---

## 12. 常用命令速查（复制即用）

```powershell
# 1 一键启动（没装过先解压 + setup-agentbox.ps1 + 填 AGENT_LLM_API_KEY）
powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1

# 2 只拉环境不对话
powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1 -NoChat

# 3 跑一条就退出
powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1 -Message "在沙箱里跑 uname -a"

# 4 鲸鱼娘管家（一件启动 + AGENT_PERSONA=whale）
powershell -ExecutionPolicy Bypass -File .\whale-girl.ps1

# 5 优雅停止
powershell -ExecutionPolicy Bypass -File .\deploy\windows\stop-agent.ps1

# 6 自检（宿主上看沙箱/镜像；平台 VM 里看 PG/LLM/embedder）
.\.venv\Scripts\python.exe -m agent.cli doctor

# 7 沙箱池状态 / 起一台验一下
.\.venv\Scripts\python.exe -m agent.cli sandbox status
.\.venv\Scripts\python.exe -m agent.cli sandbox start smoke

# 8 工具库检索 / 看源码
.\.venv\Scripts\python.exe -m agent.cli tools list -q "读取文件"
.\.venv\Scripts\python.exe -m agent.cli tools show fs.read --source

# 9 进 VM 排错（正常走 SSH；SSH 不通走串口控制台）
ssh -i var\vm_key -p 2222 agent@127.0.0.1
.\deploy\windows\vm-console.ps1 -Command 'uptime; free -m'

# 10 看这次启动到底花在哪
Get-ChildItem var\logs\start-*.log | Sort-Object LastWriteTime | Select-Object -Last 1 | Get-Content -Tail 40
```
