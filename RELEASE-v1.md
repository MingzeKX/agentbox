# agentbox v1 发布说明

**来源 commit**：`6be97c6`（打包时可用 `SOURCE-COMMIT.txt` 核对）
**状态**：可交付。单元测试 **910 passed / 3 skipped / 0 failed**；ruff、编码、hygiene 全绿。

---

## 1. v1 包含什么

**核心架构**：Windows 宿主 = 控制平面（FastAPI :8091，拥有 QEMU/WHPX）；Debian 平台 VM = AI 服务（:8090 + PostgreSQL 17 + pgvector）；沙箱 = 独立 QEMU VM（自研 PID 1、只读根、非 root、cgroup+RLIMIT、超时、`killpg`）。
**AI 服务不碰宿主文件/命令**（有隔离守卫测试钉死）；只有 3 个常驻元工具（`search_tools` / `get_tool_schema` / `call_tool`），其余工具按语义检索注入。

| 模块 | 内容 |
|---|---|
| **工具库** | 检索（pgvector + 全文检索融合，中文可用）；自造工具四道门 `check`(AST 白名单) → 自测 → 注册（sha256 版本化）；**白名单三档** `strict`/`extended`/`unrestricted` + 权限感知（`subprocess`/`smtplib` 要声明 `exec`/`net`，`open()` 要开关+`fs.*`），拒绝信息**自带修复命令**（`/config tool_extra_modules smtplib`）|
| **工具删除** | `toolsmith.retire`（agent 只能删自己写的；核心工具拒）+ 操作员 `/tools` / `/tools retire` / `/tools delete --purge`（打名字确认）；**审计台账保留**（外键 `ON DELETE SET NULL`）|
| **会话** | 系统提示词 + persona；历史护栏（工具结果入库 ≤12KB、剪未应答 `tool_calls`、丢孤儿 tool 消息、剥离重放图片、出站总量上限）；4xx 时透出网关原文且**零工具调用** |
| **联网** | 沙箱默认**无网卡**；`AGENT_SANDBOX_NET_MODE=full` 给网卡（只出不进）；AI 侧防火墙 `net.fetch`/`net.http`/`net.ping`（白名单 + 端口 + SSRF 防护 + 字节上限 + 审计）|
| **权限分级** | `safe` / `trusted` / `unrestricted`；`host.exec`（宿主执行，默认关，需短语，全量审计）|
| **控制台** | 中文 `/help`（含逐条详解）、Tab 补全、`/clear`、折叠日志 + `/more`、`/net` `/perm` `/persona` `/config` `/save`(自动持久化) `/sandbox` `/log` `/tools`、`/model` `/think` `/image`、`/voice` 与**实时语音模式**（`--voice`，推按式默认、可打断、空格不再误触发）|
| **多模态 / 模型** | 带图轮次自动切 `deepseek-flash`；`/model` 切换；`/think low\|high\|max` |
| **语音识别** | 本地 faster-whisper（`small` int8，共享缓存 `/opt/agentbox/models`）；`POST /asr` + `/voice <文件>` + 实时模式 |
| **沙箱规格** | 2GB RAM / 4 vCPU / **4GB workspace**；`net_mode=full` 下 ping / DNS / `apt-get update` 实测可用 |
| **启动体验** | `start-agent.ps1` / `stop-agent.ps1`：进度条 + 每步耗时表 + `var\logs\start-*.log` |
| **打包** | `packaging\make-bundle.ps1`（`-Lean` 实测 ~1.5GB / `-Fat` ≈13GB，可 zstd 压到 ~5.3GB）+ `setup-agentbox.ps1`（一件脚本）+ `install.ps1 -Check`；**只从干净提交态打包**，包内写 `SOURCE-COMMIT.txt` |

## 2. 已实测验证

- 单元：**910 passed**（沙箱实测另需 VM：沙箱套件 25 项、PG 套件 13 项之前均已通过）
- **会话毒化导致永久 400**：根因 = 一个工具调用崩了（控制平面不可达）只存了"声明"没存"结果"；同会话修前 `HTTP 400 / 502ms` → 修后 `stop=stop / 16947 tokens` ✓
- **硬删除不生效**：根因 = `tool_runs.tool_id NOT NULL` + `ON DELETE CASCADE`（`create_all` 不 ALTER）→ 修后 `delete → 200 {"purged":true,"deleted":1}`、重复操作 404 ✓；agent 侧 purge 自己的工具成功、`toolsmith.retire fs.read` 被拒 ✓
- **显示四条缺陷**（行尾补空格 / 续行丢缩进 / 代码围栏压行 / 空行不统一）已在真实 `exec.run uname -a` 那轮修好（空行仍为 3，见 §3）✓
- 联网：沙箱 `ping 10.0.2.2` 0.6ms、`ping mirrors.tuna.tsinghua.edu.cn` 32ms、DNS 解析 ✓
- 视觉：纯色 PNG → `"content": "橙色"` ✓；ASR `small`：英文整句一字不差、中文 `今天天氣很好我們一起去公園散步` ✓
- 关停复核：0 个 QEMU、8090/8091/8099/2222 无监听、无残留进程 ✓

## 3. v1 **未做 / 未在真机验证**（如实列出）

| 项 | 说明 |
|---|---|
| **压缩对话** | 未做（设计已定：`context_*` + `/compact` + `/context`；agent 侧草稿已丢弃）。长会话目前靠历史护栏 + `/new` |
| **控制台块间空行** | 应为 1 行，实测是 **3 行**；单测通过但未复现真实渲染路径 |
| **沙箱镜像重建 + 白名单三档真机** | 未跑（三档策略在 guest 侧，需重建镜像才生效）|
| `time.now` / `net.ping` 真机 | 未跑（代码 + 单测在）|
| **实时语音真机** | 未跑（本机无麦克风/音箱）：采集、朗读、按键打断需你实测 |
| `-Fat` 包 | 未实测（按组件相加 ≈13GB）|

## 4. 已知限制

- `effort`（思考强度）在该网关**不稳定**：实测思考 token 中位数 low 104 / high 156 / max 124，传无效值也照收 → 当实验开关
- `apt-get update` 需 tmpfs 修复后复验；**`apt-get install` 仍受只读根限制**（需 overlay 可写根，属 v1.1）
- ASR `small` 对**合成音**（espeak）识别差，真实人声未测
- 无 GPU；WHPX 必须 `-cpu Nehalem`（`host`/`max` 会挂）；模型权重 4.8GB **不随包**
- MCP 仅 Streamable HTTP（stdio 类需自行暴露为 HTTP）

## 5. 对方安装（`-Lean` 包）

```powershell
# 1) 解压后一条脚本（预检 → venv(离线 wheelhouse) → 生成 .env → 建平台 VM → 下模型 → 自检）
powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1
# 只想体检：加 -Check
# 2) 把 API key 填进 .env 的 AGENT_LLM_API_KEY，然后
powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1
.\.venv\Scripts\python.exe -m agent.cli chat
```

**必设**：`AGENT_SANDBOX_CPU=Nehalem`（否则 guest 里 numpy 等现代 wheel 拒绝加载）；`AGENT_SANDBOX_NET_MODE=off|full`。
**离线边界**：Python 本体、QEMU、`var\vm_key`（私钥永不进包）、平台 VM 安装期联网、沙箱镜像与模型（需在 VM 内构建/下载）。

## 6. v1.1 候选

压缩对话（含 `/compact` `/context`）· 控制台空行修正 · 流式 ASR（sherpa-onnx，逐字 ~200ms）· 可写根 overlay（`apt install` 真能用）· stdio 类 MCP · `-Fat` 包实测与瘦身
