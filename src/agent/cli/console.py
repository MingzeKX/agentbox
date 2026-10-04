"""Console commands for the interactive chat: ``/net``, ``/perm``, ``/persona``, ...

Everything that changes the *service* goes through the AI service's
``/admin/config`` endpoint (the service owns those settings and must not write files).
Commands that only affect this terminal (``/log``, ``/more``, ``/clear``, ``/session``,
``/new``) are handled locally.  ``/voice`` uploads a local recording to the AI
service's ``/asr`` endpoint and feeds the transcript back in as the next message.
``/stop`` is the other kind of local command: it runs the host-side teardown script
(``deploy/windows/stop-agent.ps1``) and then leaves the chat -- deliberately never
touching ``/admin/config``, because the AI service it is about to stop may already be down.

Persistence is automatic: every successful change is written to the VM's ``.env`` right
away (the same ssh rewrite ``/save`` performs), so a runtime change survives a restart
without the operator having to remember ``/save``.  ``/save`` stays as a manual re-sync.
"""

from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent.ai.llm import FALLBACK_MODEL_IDS
from agent.cli.voice import (
    MODE_HANDS_FREE,
    MODE_LABELS,
    MODE_OFF,
    MODE_PUSH_TO_TALK,
    devices_table,
    list_input_devices,
    powershell_executable,
    sounddevice_available,
    transcribe,
)
from agent.config import project_root, settings

#: every console command, in the order the help prints them
COMMANDS: tuple[str, ...] = (
    "help",
    "net",
    "perm",
    "persona",
    "model",
    "think",
    "image",
    "image-clear",
    "voice",
    "voice-mode",
    "voice-devices",
    "speak",
    "tools",
    "config",
    "save",
    "sandbox",
    "log",
    "more",
    "clear",
    "cls",
    "session",
    "new",
    "stop",
    "exit",
    "quit",
)

#: words that complete after a command
SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    "net": ("status", "on", "off", "allow", "deny", "ports", "private"),
    "perm": ("safe", "trusted", "unrestricted"),
    "log": ("ide", "plain", "json", "lines", "args", "fold"),
    "persona": (),  # filled in from the service at completion time
    "voice": (),  # paths are typed, not completed
    "voice-mode": ("on", "off", "status", "hands-free"),
    "voice-devices": (),  # nothing to complete: it lists what the machine has
    "speak": ("on", "off"),
    "image": (),  # paths are typed, not completed
    "tools": ("list", "retire", "delete"),
    "stop": ("now",),
}

#: tiers the registry groups tools by, in the order /tools prints them
TOOL_TIERS: tuple[str, ...] = ("core", "extra", "generated")
TOOL_TIER_LABELS: dict[str, str] = {
    "core": "核心（core）",
    "extra": "扩展（extra）",
    "generated": "生成（generated）",
}

#: the AI service endpoint that owns the tool lifecycle (GET) and its two mutating calls
ADMIN_TOOLS_PATH = "/admin/tools"
ADMIN_TOOLS_RETIRE_PATH = "/admin/tools/retire"
ADMIN_TOOLS_DELETE_PATH = "/admin/tools/delete"

#: the thinking-effort levels the gateway accepts; the order is the help order
EFFORT_LEVELS: tuple[str, ...] = ("low", "high", "max")
#: synonyms for "send no effort field at all"
EFFORT_OFF: tuple[str, ...] = ("default", "off", "none", "clear", "")

#: what /image asks when the command carried no question of its own
DEFAULT_IMAGE_QUESTION = "请描述这张图里有什么。"

#: image containers the AI service accepts, by file extension
IMAGE_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

#: audio types POST /asr accepts; anything else is sent as a generic blob
ASR_MEDIA_TYPES: dict[str, str] = {
    ".wav": "audio/wav",
    ".wave": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mpeg",
    ".mp4": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
    ".flac": "audio/ogg",
}

#: the host-side teardown ``/stop`` runs, relative to the repository root
STOP_SCRIPT = Path("deploy") / "windows" / "stop-agent.ps1"

#: what ``/stop`` asks before it tears the stack down (``/stop now`` skips this)
STOP_CONFIRM_PROMPT = "输入 y 确认关闭 agentbox（AI 服务 + 平台 VM + 沙箱 VM + 控制平面）；其它任意键取消："

#: printed whenever the teardown did not happen, so the operator is never stuck
STOP_MANUAL_HINT = "手动关闭：powershell -ExecutionPolicy Bypass -File .\\deploy\\windows\\stop-agent.ps1"


def stop_command(script: Path) -> list[str]:
    """The teardown command line, exactly as documented (Windows PowerShell 5.1 only)."""
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]


COMMAND_HELP: dict[str, str] = {
    "net": """[cyan]/net[/cyan] [dim]status[/dim]                       查看开关、白名单、端口
[cyan]/net on[/cyan] | [cyan]off[/cyan]                     联网总开关（net.* 工具）
[cyan]/net allow a.com,*.b.com[/cyan]        添加主机（支持通配符）
[cyan]/net deny a.com[/cyan]                 移除主机
[cyan]/net ports 80,443,8443[/cyan]          防火墙放行的端口
[cyan]/net private on[/cyan] | [cyan]off[/cyan]              [red]危险[/red]：同时放行 loopback/私有/元数据地址

沙箱本身没有网卡；net.* 运行在本防火墙之后的 AI 服务里。""",
    "perm": """[cyan]/perm[/cyan]                           查看当前权限档位
[cyan]/perm safe[/cyan]                      仅沙箱工具
[cyan]/perm trusted[/cyan]                   + 受防火墙管控的 net.* / mcp.* 工具
[cyan]/perm unrestricted <phrase>[/cyan]     [red]危险[/red]：+ 在本机上运行 host.exec

`host.exec` 还需要控制面设置 AGENT_PERMISSION_TIER=unrestricted，并且
它运行的每一条命令都会被记录下来。""",
    "persona": """[cyan]/persona[/cyan]                        列出所有角色扮演并显示当前生效的那个
[cyan]/persona roleplay[/cyan]               切换（内置：engineer、roleplay、teacher、
                                 reviewer、concise）
[cyan]/persona off[/cyan]                    [bold]取消角色扮演[/bold] -> 回到中立的
                                 'engineer'（别名：none、default、
                                 neutral；也可以直接写 `engineer`）

角色扮演只改变回答风格，不能更改权限。
添加自己的角色：把 .md 文件放到 <repo>/personas/<name>.md""",
    "log": """[cyan]/log[/cyan]                           查看当前样式
[cyan]/log ide|plain|json[/cyan]             工具活动如何渲染
[cyan]/log lines 40[/cyan]                   折叠开启时保留几行（关闭时显示几行结果）
[cyan]/log fold on|off[/cyan]                长输出折叠或展开（默认 [bold]on[/bold]）
[cyan]/log args on|off[/cyan]                显示或隐藏工具参数

折叠时保留前 6 行，并用一行说明折叠了多少行；[cyan]/more[/cyan] 可查看全文。
仅客户端：它只改变这个终端，不改变服务。""",
    "more": """[cyan]/more[/cyan]                        重新打印最近一次工具结果的[bold]完整[/bold]输出
[cyan]/more answer[/cyan]                 重新打印上一条[bold]回答[/bold]的完整文本
[cyan]/more 2[/cyan]                      往前数第 2 个结果（最多可回溯 5 个）
[cyan]/more 1[/cyan]                      与 /more 相同：最近的那一个

只为方便阅读：内存里最多保留 5 个结果、合计约 200 KB 的输出，更早的会丢弃。
服务端仍按 --log-lines / preview_chars 截断，所以 /more 看到的是收到的全文。

[bold]回答[/bold]不折叠也不重复打印：流式回答会按终端宽度换行、以 agent › 开头，
并在工具日志结束后单独成一个段落；[cyan]/more answer[/cyan] 可再完整看一遍。""",
    "tools": """[cyan]/tools[/cyan]                         列出注册表里的工具（按 tier 分组，标出 retired）
[cyan]/tools retire <名称> [版本][/cyan]     退休一个版本（默认当前 active 版本）
[cyan]/tools delete <名称> [版本][/cyan]     删除一个版本
[cyan]/tools delete <名称> [版本] --purge[/cyan]  硬删除：连带历史记录，
                                 需要输入工具名确认

[bold]只读。[/bold]/tools 只调用 AI 服务的 GET /admin/tools，不碰 /admin/config，
也不写 VM 的 .env。retire / delete 是[bold]服务端[/bold]注册表改动：会立即影响
agent 能搜到/调用的工具，但同样[bold]不写 .env[/bold]（重启后仍生效）。

--purge 是不可恢复的：确认提示要求你把工具名原样打一遍。""",
    "voice": """[cyan]/voice <音频文件>[/cyan]               把本地录音转成文字，并直接发给 agent
[cyan]/voice-mode on[/cyan]                 [bold]实时语音模式[/bold]：按回车或 Ctrl+T 说话（/help voice-mode）

支持 wav / mp3 / ogg / webm 等；请求发到 AI 服务的 /asr。
识别结果会先打印（识别到：…），然后作为下一条消息发给 agent。
可用 AGENT_ASR_MODEL 等设置配置识别模型；识别不可用时服务会返回明确错误。""",
    "voice-mode": """[cyan]/voice-mode on[/cyan]                     实时语音模式：按回车或 Ctrl+T 说话
[cyan]/voice-mode hands-free[/cyan]             免提：麦克风一直开着，Ctrl-C 退出
[cyan]/voice-mode off[/cyan]                    关闭语音模式
[cyan]/voice-mode status[/cyan]                 模式、设备、静音阈值、朗读状态

[bold]需要麦克风。[/bold]在空提示符上按回车开始录音，[cyan]Ctrl+T[/cyan]（或 F2）
则可以在[bold]正在输入时[/bold]随时开始录音；说完停顿一下自动结束（静音
[cyan]voice_silence_s[/cyan] 秒，最长 [cyan]voice_max_s[/cyan] 秒），识别结果直接作为下一条消息发给
agent，回答会用 Windows 自带的 SAPI 读出来（[cyan]/speak off[/cyan] 可静音）。

[bold]普通输入永远是文本：[/bold]只按一下空格不会开始录音、也不会发送，它就是空格；
要发送就照常按回车。语音模式只在空行上理解为「说话」。

提示行会一直说明当前状态：[cyan]🎤 录音中…（停顿 1.2 秒自动结束 / 按 Ctrl+C 取消）[/cyan]、
[cyan]识别中…[/cyan]、[cyan]🔊 朗读中（按任意键打断）[/cyan]。

[bold]可以打断：[/bold]朗读时按任意键立即停止朗读并回到聆听（打断后按 q 退出语音模式）；
agent 正在生成时按 Ctrl-C 只取消这一轮，回到提示符继续，不会退出控制台。

识别和 /voice 走同一个 AI 服务 /asr；服务不可用、没装 sounddevice、没听到声音
都会给出中文提示。输入 q（或 /voice-mode off、在提示符上 Ctrl-C）退出语音模式。
设备不对时先用 [cyan]/voice-devices[/cyan] 查序号，再设 AGENT_VOICE_INPUT_DEVICE。

[dim]默认是「按回车说话」，不是常开麦克风：否则助手会把自己的朗读录回去。[/dim]""",
    "voice-devices": """[cyan]/voice-devices[/cyan]                    列出麦克风，以及 sounddevice / 朗读是否可用

「序号」可以直接填进 AGENT_VOICE_INPUT_DEVICE（空 = 系统默认设备），
也可以填设备名的一部分（sounddevice 自己匹配子串）。
朗读走 Windows 自带的 SAPI（powershell.exe + System.Speech），不需要装新依赖。""",
    "speak": """[cyan]/speak[/cyan]                           查看回答是否朗读
[cyan]/speak on[/cyan] | [cyan]off[/cyan]                  打开或关闭朗读

朗读用 Windows 自带的 SAPI，不装任何新 Python 依赖；过长的回答会截断到
AGENT_VOICE_TTS_MAX_CHARS 字（默认 300）。只影响这个终端，默认值来自
AGENT_VOICE_TTS；这两项不会写进 VM 的 .env（服务端没有喇叭，听了也没用）。""",
    "image": """[cyan]/image <图片路径> [附带的问题][/cyan]   把本地图片附在下一条消息上

支持 png / jpg / jpeg / webp / gif（按扩展名判断类型）。
可以连写多张，累计最多 4 张（AGENT_LLM_MAX_IMAGES）；单张上限
由 AGENT_LLM_MAX_IMAGE_BYTES 决定（当前默认 8 MB）。

[cyan]/image a.png 这是什么颜色？[/cyan]       图片 + 你的问题一起发出
[cyan]/image a.png[/cyan]                    只发图片，自动附一句
                                 「请描述这张图里有什么。」
[cyan]/images[/cyan]                         查看已排队但还没发出的图片
[cyan]/image-clear[/cyan]                    清空已排队的图片

带图的那一轮，AI 服务会自动改用视觉模型（AGENT_LLM_VISION_MODEL，
默认 deepseek-flash）；纯文字仍然用 /model 选定的模型。
图片以 base64 内联在 POST /chat 的 images 字段里，不落盘。""",
    "images": "参见 [cyan]/help image[/cyan]。",
    "image-clear": "参见 [cyan]/help image[/cyan]。",
    "model": """[cyan]/model[/cyan]                         显示当前模型、视觉模型和可选列表
[cyan]/model deepseek-flash[/cyan]          切换（会写入 VM 的 .env）
[cyan]/model deepseek-v4-pro[/cyan]         只有 [bold]deepseek-flash[/bold] 能看图

可选项来自 AI 服务的 GET /llm（供应商的 /models）；供应商不可达时
只列出已知可用的两个：deepseek-flash、deepseek-v4-pro。
模型名不在列表里会被拒绝，避免把自己切到一个不存在的模型上。""",
    "think": """[cyan]/think[/cyan]                         显示当前思考强度（effort）
[cyan]/think low|high|max[/cyan]            设置思考强度
[cyan]/think default[/cyan]                 清空：不发送 effort 字段
                                 （别名：off、none、clear）

[red]注意：在当前网关上这个参数的效果不稳定。[/red]实测思考 token 的
中位数 low ≈ 104、high ≈ 156、max ≈ 124，波动比档位差别还大，
传无效值网关也照收。所以请把它当成实验开关，不要期待
「提高档位就一定更好」。留空（default）即沿用网关默认。""",
    "clear": """[cyan]/clear[/cyan] [dim]（别名 /cls）[/dim]       清屏：清空这个终端，不改变当前会话
                                 （要开新会话用 /new）""",
    "cls": "参见 [cyan]/help clear[/cyan]。",
    "config": """[cyan]/config[/cyan]                        所有运行时可改的设置

改动会立即生效，并立刻写入 VM 的 .env（重启后仍生效）。
[cyan]/save[/cyan]                          手动再同步一次（正常情况下不需要）""",
    "sandbox": """[cyan]/sandbox[/cyan]                       沙箱池状态：虚拟机、加速器、预热数量

来宾机 RAM/CPU 来自宿主 .env 里启动时的 AGENT_SANDBOX_VM_MEMORY_MB / _CPUS；
工作区大小是沙箱镜像的构建期常量。""",
    "save": "参见 [cyan]/help config[/cyan]。",
    "session": "[cyan]/session[/cyan]                       打印当前会话 id",
    "new": "[cyan]/new[/cyan]                           开启一个新会话（下一条消息才会真正创建）",
    "stop": """[cyan]/stop[/cyan]                          关闭整个 agentbox（先确认）
[cyan]/stop now[/cyan]                      不再确认，立即关闭

会依次停掉：控制平面 → AI 服务 → 平台 VM（干净关机）→ 沙箱 VM。
实际执行的命令（输出实时打印到这里，成功后本控制台自行退出，退出码 0）：
  powershell -NoProfile -ExecutionPolicy Bypass -File <repo>\\deploy\\windows\\stop-agent.ps1
只想离开聊天而不关服务，请用 [cyan]/exit[/cyan]。

[bold]宿主机动作：[/bold]不经过 AI 服务的 /admin/config，所以 AI 服务已经挂了也能用。
脚本不存在或执行失败时[bold]不会[/bold]退出控制台，会打印试过的完整路径、退出码，
以及可以自己敲的手动命令。""",
    "exit": "[cyan]/exit[/cyan] 或 [cyan]/quit[/cyan]                 离开控制台（不关闭服务；关闭整个 agentbox 用 [cyan]/stop[/cyan]）",
    "help": "[cyan]/help[/cyan] 或 [cyan]/help <command>[/cyan]        本说明，或某一条命令的帮助",
}

HELP = """
[bold]控制台命令[/bold]  [dim](按 Tab 补全 · /help <command> 查看详情 · /exit 退出)[/dim]

[cyan]/net[/cyan]       联网：开关、白名单、端口、SSRF 防护          [dim](/help net)[/dim]
[cyan]/perm[/cyan]      权限档位：safe | trusted | unrestricted       [dim](/help perm)[/dim]
[cyan]/persona[/cyan]   回答风格；[bold]/persona off 取消角色扮演[/bold]     [dim](/help persona)[/dim]
[cyan]/model[/cyan]     回答模型；[bold]deepseek-flash 能看图[/bold]          [dim](/help model)[/dim]
[cyan]/think[/cyan]     思考强度 low | high | max（效果不稳定）      [dim](/help think)[/dim]
[cyan]/image[/cyan]     把本地图片附在下一条消息上（最多 4 张）       [dim](/help image)[/dim]
[cyan]/log[/cyan]       工具日志：ide | plain | json，长输出折叠         [dim](/help log)[/dim]
[cyan]/more[/cyan]      重新显示最近一次工具结果（或回答）的完整输出       [dim](/help more)[/dim]
[cyan]/voice[/cyan]     录音转文字，并把结果发给 agent                [dim](/help voice)[/dim]
[cyan]/voice-mode[/cyan] 实时语音：按回车或 Ctrl+T 说话，回答朗读出来     [dim](/help voice-mode)[/dim]
[cyan]/voice-devices[/cyan] 列出麦克风 · [cyan]/speak on|off[/cyan] 朗读开关   [dim](/help voice-devices)[/dim]
[cyan]/tools[/cyan]     注册表工具：列表 / retire / delete（不写 .env）  [dim](/help tools)[/dim]
[cyan]/config[/cyan]    所有运行时可改的设置                          [dim](/help config)[/dim]
[cyan]/save[/cyan]      手动把当前值再同步到 VM 的 .env（改设置时已自动写入）
[cyan]/sandbox[/cyan]   沙箱池状态
[cyan]/clear[/cyan]     清屏（同义：[cyan]/cls[/cyan]，不改变会话）
[cyan]/stop[/cyan]      关闭整个 agentbox：AI 服务 + 平台 VM + 沙箱 VM + 控制平面  [dim](/help stop)[/dim]
[cyan]/session[/cyan] [cyan]/new[/cyan] [cyan]/exit[/cyan]

不以斜杠开头的行会作为消息发给 agent。
"""


def _version_argument(value: str | None) -> int | None:
    """``[version]`` from a typed command: ``None`` when it was omitted (pick active)."""
    if value is None:
        return None
    text = value.strip()
    if not text.isdigit():
        raise RuntimeError(f"版本号必须是数字：{value!r}（用法：/tools delete <名称> [版本] [--purge]）")
    return int(text)


def known_models() -> list[str]:
    """Model ids the console offers: the service's list, else the two known ones."""
    try:
        response = httpx.get(f"http://127.0.0.1:{settings.ai_port}/llm", timeout=2.0)
        models = response.json().get("available_models")
    except (httpx.HTTPError, ValueError):
        models = None
    if not models:
        return list(FALLBACK_MODEL_IDS)
    return [str(name) for name in models]


def complete(text: str, personas: list[str] | None = None, models: list[str] | None = None) -> list[str]:
    """Completion candidates for a partially typed console line (testable, no I/O)."""
    if not text.startswith("/"):
        return []
    body = text[1:]
    if " " not in body:
        return [f"/{name}" for name in COMMANDS if name.startswith(body)]
    command, _, rest = body.partition(" ")
    if " " in rest:  # only the first argument completes
        return []
    if command == "persona":
        options = list(personas or [])
        if not options:
            options = ["engineer", "roleplay", "teacher", "reviewer", "concise", "off"]
        return [f"/{command} {name}" for name in options if name.startswith(rest)]
    if command == "model":
        if models is None:
            options = known_models()
        else:
            options = list(models)
        return [f"/{command} {name}" for name in options if name.startswith(rest)]
    if command == "think":
        options: list[str] = [*EFFORT_LEVELS, "default"]
        return [f"/{command} {name}" for name in options if name.startswith(rest)]
    options = SUBCOMMANDS.get(command, ())
    if command == "net" and rest:
        # allow/deny take hosts; ports takes numbers; nothing useful to suggest
        return []
    return [f"/{command} {name}" for name in options if name.startswith(rest)]



@dataclass
class ConsoleState:
    session_id: str | None = None
    log_style: str = "ide"
    log_lines: int = 24
    show_args: bool = True
    fold: bool = True
    #: /voice language hint; None lets the service auto-detect
    asr_language: str | None = "zh"
    exit_requested: bool = False
    new_session: bool = False
    #: transcript waiting to be sent as the next message (/voice)
    pending_message: str | None = None
    #: real-time voice mode: MODE_OFF | MODE_PUSH_TO_TALK | MODE_HANDS_FREE (/voice-mode, --voice)
    voice_mode: str = MODE_OFF
    #: images waiting to ride along with the next message (/image)
    pending_images: list[dict[str, str]] = field(default_factory=list)
    #: the live renderer, so /more can reprint what it retained
    renderer: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


class SlashConsole:
    def __init__(self, console: Console, state: ConsoleState, err_console: Console | None = None) -> None:
        self.console = console
        self.err = err_console or console
        self.state = state

    # ------------------------------------------------------------------ transport
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if settings.control_secret:
            headers["X-Agent-Token"] = settings.control_secret
        return headers

    def _admin(self, changes: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"http://127.0.0.1:{settings.ai_port}/admin/config"
        try:
            if changes is None:
                response = httpx.get(url, headers=self._headers(), timeout=10.0)
            else:
                response = httpx.post(url, headers=self._headers(), json={"set": changes}, timeout=10.0)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"无法连接 AI 服务：{exc}") from exc
        body = response.json()
        if response.status_code >= 400 or not body.get("ok"):
            raise RuntimeError(body.get("error") or f"HTTP {response.status_code}")
        return body

    def _effective(self) -> dict[str, Any]:
        return self._admin().get("effective", {})

    # --------------------------------------------------------------- persistence
    @staticmethod
    def _env_keys(values: dict[str, Any]) -> dict[str, Any]:
        """The mutable settings, mapped onto the names the VM's .env uses."""
        return {
            "AGENT_PERSONA": values.get("persona"),
            "AGENT_PERMISSION_TIER": values.get("permission_tier"),
            "AGENT_NET_ENABLED": "1" if values.get("net_enabled") else "0",
            "AGENT_NET_ALLOW_HOSTS": values.get("net_allow_hosts") or "",
            "AGENT_NET_ALLOW_PORTS": values.get("net_allow_ports") or "80,443",
            "AGENT_NET_MAX_BYTES": values.get("net_max_bytes"),
            "AGENT_NET_ALLOW_PRIVATE_HOSTS": "1" if values.get("net_allow_private_hosts") else "0",
            # /model and /think: also runtime changes, so they persist like the rest
            "AGENT_LLM_MODEL": values.get("llm_model"),
            "AGENT_LLM_EFFORT": values.get("llm_effort") or "",
        }

    def _persist(self, keys: dict[str, Any]) -> None:
        """Rewrite the mutable keys in the VM's ``.env`` over ssh (what /save did).

        Raises ``RuntimeError`` when the ssh client or the VM key is missing, so a host
        CLI without a VM gets a clear message instead of a traceback.
        """
        ssh = Path("C:/Windows/System32/OpenSSH/ssh.exe")
        key = project_root() / "var" / "vm_key"
        if not ssh.is_file() or not key.is_file():
            raise RuntimeError("无法持久化：缺少 ssh 客户端或 var/vm_key（请改在 VM 内运行）")
        script = ["set -e", "cd /opt/agentbox/app"]
        for name, value in keys.items():
            if value is None:
                continue
            script.append(f"sudo sed -i '/^{name}=/d' .env")
            script.append(f"printf '%s\\n' '{name}={value}' | sudo tee -a .env >/dev/null")
        script.append("grep -c '^AGENT_' .env")
        remote = " && ".join(script)
        result = subprocess.run(  # noqa: S603 - fixed binary, explicit argv
            [
                str(ssh),
                "-i",
                str(key),
                "-p",
                "2222",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=NUL",
                "-o",
                "LogLevel=ERROR",
                "agent@127.0.0.1",
                f"bash -lc {json.dumps(remote)}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"持久化失败：{result.stderr.strip()[:200]}")

    def _sync_env(self, values: dict[str, Any], note: str = "已写入 .env（重启后仍生效）") -> bool:
        """Persist automatically; a failure warns but never loses the runtime change."""
        try:
            self._persist(self._env_keys(values))
        except RuntimeError as exc:
            self.err.print(f"[yellow]警告：{exc}[/yellow]")
            self.err.print("[yellow]这次改动只在当前运行有效[/yellow]")
            return False
        self.console.print(f"[green]{note}[/green]")
        return True

    # ------------------------------------------------------------------- commands
    def handle(self, line: str) -> bool:
        """Handle a ``/command``; returns True when the line was consumed."""
        if not line.startswith("/"):
            return False
        parts = line[1:].split()
        if not parts:
            return False
        command, args = parts[0].lower(), parts[1:]
        handlers = {
            "help": self._cmd_help,
            "net": self._cmd_net,
            "perm": self._cmd_perm,
            "persona": self._cmd_persona,
            "model": self._cmd_model,
            "think": self._cmd_think,
            "image": self._cmd_image,
            "images": self._cmd_images,
            "image-clear": self._cmd_image_clear,
            "config": self._cmd_config,
            "save": self._cmd_save,
            "sandbox": self._cmd_sandbox,
            "log": self._cmd_log,
            "more": self._cmd_more,
            "voice": self._cmd_voice,
            "voice-mode": self._cmd_voice_mode,
            "voice-devices": self._cmd_voice_devices,
            "speak": self._cmd_speak,
            "tools": self._cmd_tools,
            "clear": self._cmd_clear,
            "cls": self._cmd_clear,
            "session": self._cmd_session,
            "new": self._cmd_new,
            "stop": self._cmd_stop,
            "exit": self._cmd_exit,
            "quit": self._cmd_exit,
        }
        handler = handlers.get(command)
        if handler is None:
            self.err.print(f"[red]未知命令[/red] /{command} — 试试 /help")
            return True
        try:
            handler(args)
        except RuntimeError as exc:
            self.err.print(f"[red]{exc}[/red]")
        return True

    def _apply_mutable(self, changes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Like :meth:`_apply`, but a key the service cannot change reads as Chinese.

        ``/model`` and ``/think`` touch keys that may not be on the service's mutable
        whitelist yet; a silent no-op would be worse than a clear refusal.
        """
        try:
            effective, applied = self._apply(changes)
        except RuntimeError as exc:
            message = str(exc)
            if "cannot change" in message or "unknown setting" in message:
                keys = "、".join(changes)
                raise RuntimeError(
                    f"服务暂不支持运行时修改 {keys}（{message}）——"
                    "请在 VM 的 .env 里改，然后 sudo systemctl restart agentbox-ai"
                ) from exc
            raise
        return effective, applied

    def _cmd_help(self, args: list[str]) -> None:
        if not args:
            self.console.print(HELP.strip())
            return
        topic = args[0].lower()
        text = COMMAND_HELP.get(topic)
        if text is None:
            self.err.print(f"[red]没有 /{topic} 的帮助[/red] — 已知命令： {', '.join(COMMANDS)}")
            return
        self.console.print(text)

    # ------------------------------------------------------------------------ net
    def _net_summary(self, effective: dict[str, Any] | None = None) -> Table:
        values = effective or self._effective()
        table = Table(title="联网", show_header=False, box=None)
        table.add_column(style="cyan")
        table.add_column()
        table.add_row("开关", "[green]开[/green]" if values.get("net_enabled") else "[yellow]关[/yellow]")
        table.add_row("白名单", str(values.get("net_allow_hosts") or "[dim]（空 = 不允许任何主机）[/dim]"))
        table.add_row("端口", str(values.get("net_allow_ports")))
        table.add_row("字节上限", str(values.get("net_max_bytes")))
        table.add_row(
            "私有主机",
            "[red]已放行[/red]" if values.get("net_allow_private_hosts") else "已阻止（loopback/私有/元数据）",
        )
        return table

    def _cmd_net(self, args: list[str]) -> None:
        action = args[0].lower() if args else "status"
        if action == "status":
            self.console.print(self._net_summary())
            return
        if action in {"on", "off"}:
            effective, applied = self._apply({"net_enabled": action == "on"})
            self.console.print(self._net_summary(effective))
            if action == "on" and not (effective.get("net_allow_hosts") or "").strip():
                self.console.print(
                    "[yellow]白名单为空，所以现在还抓不到任何东西：[/yellow] "
                    "[cyan]/net allow pypi.org,files.pythonhosted.org[/cyan]"
                )
            self._note(applied)
            return
        if action in {"allow", "deny"}:
            if len(args) < 2:
                raise RuntimeError(f"用法：/net {action} host[,host]")
            current = [h.strip() for h in str(self._effective().get("net_allow_hosts") or "").split(",") if h.strip()]
            wanted = [h.strip().lower() for h in " ".join(args[1:]).replace(";", ",").split(",") if h.strip()]
            if action == "allow":
                merged = sorted({*current, *wanted})
            else:
                merged = [h for h in current if h not in set(wanted)]
            effective, applied = self._apply({"net_allow_hosts": ",".join(merged)})
            self.console.print(self._net_summary(effective))
            self._note(applied)
            return
        if action == "ports":
            if len(args) < 2:
                raise RuntimeError("用法：/net ports 80,443[,8443]")
            ports = ",".join(p.strip() for p in " ".join(args[1:]).split(",") if p.strip())
            effective, applied = self._apply({"net_allow_ports": ports})
            self.console.print(self._net_summary(effective))
            self._note(applied)
            return
        if action == "private":
            if len(args) < 2 or args[1].lower() not in {"on", "off"}:
                raise RuntimeError("用法：/net private on|off")
            turn_on = args[1].lower() == "on"
            if turn_on:
                self.console.print(
                    "[red]警告：[/red]这会让 agent 能访问 loopback、私有网段和云元数据地址"
                    "（SSRF 攻击面）。仅供测试。"
                )
            effective, applied = self._apply({"net_allow_private_hosts": turn_on})
            self.console.print(self._net_summary(effective))
            self._note(applied)
            return
        raise RuntimeError(f"未知的 /net 子命令 {action!r} — 试试 /help")

    # ----------------------------------------------------------------------- perm
    def _cmd_perm(self, args: list[str]) -> None:
        values = self._effective()
        if not args:
            tier = values.get("permission_tier")
            self.console.print(
                Panel(
                    f"权限档位：[bold]{tier}[/bold]\n"
                    "[dim]safe：仅沙箱工具 · trusted：+ 受防火墙管控的 net.* 和 MCP · "
                    "unrestricted：+ 在本机上运行 host.exec[/dim]",
                    border_style="cyan" if tier != "unrestricted" else "red",
                )
            )
            return
        wanted = args[0].lower()
        if wanted not in {"safe", "trusted", "unrestricted"}:
            raise RuntimeError("用法：/perm safe|trusted|unrestricted <phrase>")
        if wanted == "unrestricted":
            phrase = " ".join(args[1:]).strip()
            if not phrase:
                self.console.print(
                    Panel(
                        "[red]unrestricted 模式会让 agent 在【本机】上运行命令[/red]，"
                        "绕过沙箱，在配置的工作目录里执行。\n\n"
                        "它运行的每一条命令都会作为工具调用被记录。要启用它，请带上下面这个短语"
                        f"再执行一次命令：\n\n  [bold]/perm unrestricted {settings.host_exec_phrase}[/bold]",
                        title="[red]危险[/red]",
                        border_style="red",
                    )
                )
                return
            if phrase != settings.host_exec_phrase:
                raise RuntimeError("确认短语与 AGENT_HOST_EXEC_PHRASE 不匹配")
            self.console.print(
                Panel(
                    "[red]unrestricted 模式已启用[/red]：`host.exec` 会在本机上运行。\n"
                    "用完后请用 [cyan]/perm safe[/cyan] 切回去。",
                    border_style="red",
                )
            )
        effective, applied = self._apply({"permission_tier": wanted})
        self.console.print(f"[dim]权限档位：{effective.get('permission_tier')}[/dim]")
        self._note(applied)

    # -------------------------------------------------------------------- persona
    def _cmd_persona(self, args: list[str]) -> None:
        if not args:
            try:
                body = httpx.get(
                    f"http://127.0.0.1:{settings.ai_port}/personas", timeout=10.0
                ).json()
                rows = body.get("personas") or []
                active = body.get("active")
            except httpx.HTTPError as exc:
                raise RuntimeError(f"无法连接 AI 服务：{exc}") from exc
            table = Table(title=f"角色扮演（当前：{active}）")
            table.add_column("名称", style="cyan")
            table.add_column("来源")
            table.add_column("", style="green")
            for row in rows:
                table.add_row(row["name"], row.get("source", ""), "<- 当前" if row["name"] == active else "")
            self.console.print(table)
            self.console.print(
                "[dim]用 /persona <name> 切换 · 用 [/dim][cyan]/persona off[/cyan]"
                "[dim] 取消角色扮演（回到 'engineer'）[/dim]"
            )
            self.console.print("[dim]添加自己的角色：把 .md 文件放到 <repo>/personas/<name>.md[/dim]")
            return
        wanted = args[0].lower()
        if wanted in {"off", "none", "default", "neutral"}:
            wanted = "engineer"
        effective, applied = self._apply({"persona": wanted})
        self.console.print(f"[dim]角色扮演：{effective.get('persona')}[/dim]")
        if effective.get("persona") == "engineer":
            self.console.print("[dim]  （中立风格：已取消角色扮演）[/dim]")
        self._note(applied)

    # ---------------------------------------------------------------------- model
    def _llm_info(self) -> dict[str, Any] | None:
        """``GET /llm`` — the configured models, vision routing and effort; None if unusable."""
        url = f"http://127.0.0.1:{settings.ai_port}/llm"
        try:
            response = httpx.get(url, headers=self._headers(), timeout=10.0)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"无法连接 AI 服务：{exc}") from exc
        try:
            body = response.json()
        except ValueError:
            return None
        if response.status_code >= 400:
            return None
        return body if isinstance(body, dict) else None

    @staticmethod
    def _model_names(info: dict[str, Any] | None) -> list[str]:
        names = [str(name) for name in ((info or {}).get("available_models") or [])]
        return names or list(FALLBACK_MODEL_IDS)

    def _model_line(self, info: dict[str, Any]) -> str:
        current = info.get("model")
        vision_model = info.get("vision_model")
        vision = "开" if info.get("vision_enabled") else "关"
        return f"[dim]模型：[/dim][bold]{current}[/bold] · 视觉：{vision}（带图的轮次用 {vision_model}）"

    def _cmd_model(self, args: list[str]) -> None:
        """Show or switch the answering model (a runtime setting, persisted to .env)."""
        if not args:
            try:
                info = self._llm_info()
            except RuntimeError as exc:
                self.console.print(f"[yellow]{exc}[/yellow]")
                info = None
            if info is None:
                self.console.print("[yellow]无法读取模型列表[/yellow]")
                self.console.print(f"[dim]已知可用的模型：{', '.join(FALLBACK_MODEL_IDS)}[/dim]")
                return
            self.console.print(self._model_line(info))
            available = self._model_names(info)
            self.console.print(f"[dim]可选模型（GET /llm）：{', '.join(available)}[/dim]")
            self.console.print(
                "[dim]用 [/dim][cyan]/model <名称>[/cyan][dim] 切换；只有 deepseek-flash 能看图[/dim]"
            )
            return
        wanted = args[0].strip()
        available: list[str] = []
        try:
            info = self._llm_info()
        except RuntimeError as exc:
            self.console.print(f"[yellow]{exc}[/yellow]")
            info = None
        if info is not None:
            available = self._model_names(info)
            if wanted not in available:
                raise RuntimeError(f"未知模型 {wanted!r} — 可用：{', '.join(available)}")
        else:
            self.console.print(
                f"[yellow]无法读取模型列表，按已知模型继续：{', '.join(FALLBACK_MODEL_IDS)}[/yellow]"
            )
        effective, applied = self._apply_mutable({"llm_model": wanted})
        self.console.print(f"[dim]模型：{effective.get('llm_model')}[/dim]")
        if info is not None and info.get("vision_enabled") and wanted != info.get("vision_model"):
            self.console.print(
                f"[dim]  提示：{wanted} 不能看图；带图的轮次会自动改用 {info.get('vision_model')}[/dim]"
            )
        self._note(applied)

    # ---------------------------------------------------------------------- think
    def _cmd_think(self, args: list[str]) -> None:
        """Show or set the thinking effort (a runtime setting, persisted to .env)."""
        if not args:
            info = self._llm_info()
            if info is None:
                self.console.print("[yellow]无法连接 AI 服务，无法读取思考强度[/yellow]")
                return
            effort = info.get("effort") or ""
            self.console.print(f"[dim]思考强度：{effort or '（空 = 网关默认）'}[/dim]")
            self.console.print(f"[dim]可选：{' | '.join(EFFORT_LEVELS)} | default（清空）[/dim]")
            self.console.print("[dim]该参数在当前网关效果不稳定，仅供参考。[/dim]")
            return
        wanted = args[0].strip().lower()
        value = "" if wanted in EFFORT_OFF else wanted
        if value and value not in EFFORT_LEVELS:
            raise RuntimeError(f"未知强度 {args[0]!r} — 可用：{' | '.join(EFFORT_LEVELS)} | default（清空）")
        effective, applied = self._apply_mutable({"llm_effort": value})
        shown = effective.get("llm_effort") or "（空 = 网关默认）"
        self.console.print(f"[dim]思考强度：{shown}[/dim]")
        self._note(applied)
        if applied:
            self.console.print("[dim]  该参数在当前网关效果不稳定，仅供参考。[/dim]")

    # ---------------------------------------------------------------------- image
    def _read_image(self, path: Path) -> dict[str, str]:
        """Validate one local image and turn it into the inline shape POST /chat wants."""
        media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
        if media_type is None:
            allowed = "、".join(sorted(IMAGE_MEDIA_TYPES))
            raise RuntimeError(f"不支持的图片格式 {path.suffix or '(无扩展名)'} — 支持：{allowed}")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"无法读取图片 {path}：{exc}") from exc
        if not raw:
            raise RuntimeError(f"图片文件是空的：{path}")
        limit = settings.llm_max_image_bytes
        if len(raw) > limit:
            raise RuntimeError(
                f"图片过大：{path.name} 有 {len(raw)} 字节，上限是 {limit} 字节"
                "（AGENT_LLM_MAX_IMAGE_BYTES）"
            )
        return {
            "media_type": media_type,
            "data_base64": base64.b64encode(raw).decode("ascii"),
            "name": path.name,
        }

    def _select_image_args(self, args: list[str]) -> tuple[list[str], str | None]:
        """First the run of paths that exist, then the rest as the question to send."""
        paths: list[str] = []
        question: list[str] = []
        for position, item in enumerate(args):
            if question:
                question.append(item)
                continue
            if Path(item).expanduser().is_file():
                paths.append(item)
            else:
                question = args[position:]
        return paths, (" ".join(question).strip() or None)

    def _cmd_image(self, args: list[str]) -> None:
        """Queue local image(s) to ride along with the next message."""
        if not args:
            raise RuntimeError("用法：/image <图片路径> [附带的问题]")
        paths, question = self._select_image_args(args)
        if not paths:
            raise RuntimeError(f"找不到图片文件：{args[0]}")
        limit = settings.llm_max_images
        if len(self.state.pending_images) + len(paths) > limit:
            raise RuntimeError(
                f"图片数量超过上限：已排队 {len(self.state.pending_images)} 张，"
                f"本次再加 {len(paths)} 张，最多 {limit} 张（AGENT_LLM_MAX_IMAGES）"
            )
        loaded = [self._read_image(Path(path).expanduser()) for path in paths]
        self.state.pending_images.extend(loaded)
        for item in loaded:
            size_kb = len(item["data_base64"]) * 3 // 4 / 1024
            self.console.print(
                f"[green]已加入附图[/green] {item['name']}（{item['media_type']} · {size_kb:.1f} KB）"
            )
        self.state.pending_message = question or DEFAULT_IMAGE_QUESTION
        self.console.print(f"[dim]附图 {len(self.state.pending_images)}/{limit} 张 · 下一条消息一起发送[/dim]")
        if question:
            self.console.print(f"[dim]附带的问题：{question}[/dim]")
        else:
            self.console.print(f"[dim]未附带问题，将发送默认提问：{DEFAULT_IMAGE_QUESTION}[/dim]")

    def _cmd_images(self, args: list[str]) -> None:  # noqa: ARG002
        """List the images queued for the next message."""
        if not self.state.pending_images:
            self.console.print("[dim]还没有排队的图片（用 /image <路径> 添加）[/dim]")
            return
        self.console.print(f"[dim]已排队 {len(self.state.pending_images)} 张图片：[/dim]")
        for item in self.state.pending_images:
            self.console.print(f"[dim]  · {item.get('name', '?')}（{item['media_type']}）[/dim]")

    def _cmd_image_clear(self, args: list[str]) -> None:  # noqa: ARG002
        count = len(self.state.pending_images)
        self.state.pending_images = []
        self.console.print(f"[dim]已清空 {count} 张排队的图片[/dim]")


    # --------------------------------------------------------------------- config
    def _cmd_config(self, args: list[str]) -> None:  # noqa: ARG002
        body = self._admin()
        table = Table(title="运行时设置")
        table.add_column("配置项", style="cyan")
        table.add_column("当前值")
        table.add_column("提示", style="dim")
        for key, hint in (body.get("mutable") or {}).items():
            table.add_row(key, str(body["effective"].get(key)), hint)
        self.console.print(table)

    def _cmd_save(self, args: list[str]) -> None:  # noqa: ARG002
        """Manual re-sync: write the current settings into the VM's .env over ssh."""
        values = self._effective()
        self._persist(self._env_keys(values))
        self.console.print(
            "[green]已保存[/green] 到 VM 的 .env — 用 "
            "[cyan]sudo systemctl restart agentbox-ai[/cyan] 使其生效（在此之前运行时值一直有效）"
        )

    # -------------------------------------------------------------------- sandbox
    def _cmd_sandbox(self, args: list[str]) -> None:  # noqa: ARG002
        from agent.cli.main import _control_rpc

        status = _control_rpc("sandbox.status")
        table = Table(title=f"沙箱池（accel={status.get('accel')} warm={status.get('warm')} active={status.get('active')}）")
        table.add_column("虚拟机")
        table.add_column("会话")
        table.add_column("状态")
        table.add_column("运行时长", justify="right")
        for vm in status.get("vms") or []:
            table.add_row(
                vm.get("vm_id", ""), vm.get("session_id") or "-", vm.get("state", ""), f"{vm.get('uptime_s', 0)}s"
            )
        self.console.print(table)
        self.console.print(
            "[dim]来宾机 RAM/CPU 和工作区大小是启动/构建期设置：宿主 .env 里的 "
            "AGENT_SANDBOX_VM_MEMORY_MB 和 AGENT_SANDBOX_VM_CPUS，"
            "以及构建镜像时的 WORKSPACE_SIZE[/dim]"
        )

    # ------------------------------------------------------------------------ log
    def _cmd_log(self, args: list[str]) -> None:
        if not args:
            self.console.print(
                f"[dim]日志样式：{self.state.log_style} · 行数：{self.state.log_lines} · "
                f"参数：{'显示' if self.state.show_args else '隐藏'} · "
                f"折叠：{'开' if self.state.fold else '关'}[/dim]"
            )
            return
        action = args[0].lower()
        if action in {"ide", "plain", "json"}:
            self.state.log_style = action
        elif action == "lines" and len(args) > 1 and args[1].isdigit():
            self.state.log_lines = max(1, min(500, int(args[1])))
        elif action == "args" and len(args) > 1 and args[1].lower() in {"on", "off"}:
            self.state.show_args = args[1].lower() == "on"
        elif action == "fold" and len(args) > 1 and args[1].lower() in {"on", "off"}:
            self.state.fold = args[1].lower() == "on"
            if self.state.renderer is not None:
                self.state.renderer.fold = self.state.fold
        else:
            raise RuntimeError("用法：/log [ide|plain|json] [lines N] [args on|off] [fold on|off]")
        # /log is client side only: no .env to write
        self._note(
            [
                f"日志：{self.state.log_style}，{self.state.log_lines} 行，"
                f"参数 {'开' if self.state.show_args else '关'}，"
                f"折叠 {'开' if self.state.fold else '关'}"
            ],
            persist=False,
        )

    # ----------------------------------------------------------------------- more
    def _cmd_more(self, args: list[str]) -> None:
        """Reprint a retained tool result -- or the last answer -- in full."""
        back = 1
        if args:
            if args[0].lower() == "answer":
                renderer = self.state.renderer
                if renderer is None or not renderer.print_full_answer():
                    self.console.print("[dim]还没有可展开的回答[/dim]")
                return
            if not args[0].isdigit():
                raise RuntimeError("用法：/more [n|answer]（n = 往前数第几个结果，默认 1）")
            back = max(1, int(args[0]))
        renderer = self.state.renderer
        if renderer is None or not getattr(renderer, "retained", 0):
            self.console.print("[dim]还没有可展开的工具结果[/dim]")
            return
        if not renderer.print_full_output(back):
            self.console.print(
                f"[yellow]只保留了最近 {getattr(renderer, 'retained', 0)} 个工具结果，"
                "没有第 %d 个[/yellow]" % back
            )
            return
        self.console.print("[dim]以上是完整输出（折叠不会丢弃内容）[/dim]")

    # ---------------------------------------------------------------------- tools
    def _admin_request(
        self, path: str, payload: dict[str, Any] | None = None, *, method: str = "GET"
    ) -> dict[str, Any]:
        """One AI-service ``/admin/*`` call, with the control token and Chinese errors.

        Deliberately *not* :meth:`_admin`: the tool registry lives on its own endpoints, and
        keeping them apart is what makes ``/tools`` provably read-only (it never touches
        ``/admin/config`` and therefore never writes the VM's ``.env``).
        """
        url = f"http://127.0.0.1:{settings.ai_port}{path}"
        try:
            if method == "POST":
                response = httpx.post(url, headers=self._headers(), json=payload or {}, timeout=30.0)
            else:
                response = httpx.get(url, headers=self._headers(), timeout=30.0)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"无法连接 AI 服务（工具注册表）：{exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"工具注册表返回了无法解析的响应（HTTP {response.status_code}）") from exc
        if response.status_code == 401:
            raise RuntimeError("工具注册表被拒绝：控制面令牌不匹配（检查 AGENT_CONTROL_SECRET）")
        if response.status_code >= 400 or not body.get("ok"):
            raise RuntimeError(body.get("error") or f"工具注册表返回 HTTP {response.status_code}")
        return body

    def _registry_tools(self) -> list[dict[str, Any]]:
        return list(self._admin_request(ADMIN_TOOLS_PATH).get("tools") or [])

    def _tool_rows(self, name: str) -> list[dict[str, Any]]:
        return [row for row in self._registry_tools() if str(row.get("name")) == name]

    def _tool_version(self, name: str, version: int | None) -> int:
        """The version to act on: the one given, else the single active one."""
        if version is not None:
            return int(version)
        rows = self._tool_rows(name)
        active = [row for row in rows if str(row.get("status")) == "active"]
        if len(active) == 1:
            return int(active[0]["version"])
        if not rows:
            raise RuntimeError(f"注册表里没有工具 {name}")
        if not active:
            raise RuntimeError(f"{name} 没有 active 版本；请带上版本号（先看 /tools 列表）")
        versions = "、".join(str(row["version"]) for row in active)
        raise RuntimeError(f"{name} 有多个 active 版本（{versions}）；请带上版本号")

    def _tools_table(self, tools: list[dict[str, Any]]) -> Table:
        """The registry grouped by tier, with retired versions marked, not hidden."""
        table = Table(title=f"注册表工具（{len(tools)}）", title_justify="left")
        table.add_column("名称", style="cyan")
        table.add_column("版本", justify="right")
        table.add_column("状态")
        table.add_column("tier")
        table.add_column("创建者", style="dim")
        known = {tier: 0 for tier in TOOL_TIERS}
        for tool in tools:
            known[str(tool.get("tier") or "extra")] = known.get(str(tool.get("tier") or "extra"), 0) + 1
        for tier in (*TOOL_TIERS, *sorted(set(known) - set(TOOL_TIERS))):
            rows = [tool for tool in tools if str(tool.get("tier") or "extra") == tier]
            if not rows:
                continue
            table.add_row(
                f"[bold]{TOOL_TIER_LABELS.get(tier, tier)}[/bold]",
                f"[dim]{len(rows)}[/dim]",
                "",
                "",
                "",
            )
            for tool in sorted(rows, key=lambda row: (str(row.get("name")), int(row.get("version") or 0))):
                status = str(tool.get("status") or "")
                style = "dim" if status in {"retired", "quarantined"} else ""
                if status == "retired":
                    marker = "[yellow]retired[/yellow]"
                elif status == "quarantined":
                    marker = "[red]quarantined[/red]"
                elif status:
                    marker = f"[green]{status}[/green]"
                else:
                    marker = "[dim]?[/dim]"
                table.add_row(
                    Text(str(tool.get("name") or "?"), style=style),
                    Text(f"v{tool.get('version', '?')}", style=style),
                    marker,
                    Text(str(tool.get("tier") or ""), style="dim"),
                    Text(str(tool.get("created_by") or ""), style=style),
                )
        return table

    def _cmd_tools(self, args: list[str]) -> None:
        """List / retire / delete registry tools -- never through /admin/config."""
        action = args[0].lower() if args else "list"
        if action == "list":
            rows = self._registry_tools()
            if not rows:
                self.console.print("[dim]注册表里还没有工具[/dim]")
                return
            self.console.print(self._tools_table(rows))
            self.console.print(
                "[dim]用 [/dim][cyan]/tools retire <名称> [版本][/cyan][dim] 退休，"
                "[/dim][cyan]/tools delete <名称> [版本] --purge[/cyan][dim] 彻底删除。"
                "只读查询，不写 .env。[/dim]"
            )
            return
        if action == "retire":
            if len(args) < 2:
                raise RuntimeError("用法：/tools retire <名称> [版本]")
            name = args[1]
            version = self._tool_version(name, _version_argument(args[2] if len(args) > 2 else None))
            body = self._admin_request(
                ADMIN_TOOLS_RETIRE_PATH, {"name": name, "version": version}, method="POST"
            )
            self.console.print(
                f"[green]已退休 {name} v{body.get('version', version)}[/green]"
                "（仅影响注册表，不写 .env）"
            )
            return
        if action == "delete":
            purge = "--purge" in args[2:]
            rest = [item for item in args[1:] if item != "--purge"]
            if not rest:
                raise RuntimeError("用法：/tools delete <名称> [版本] [--purge]")
            name = rest[0]
            version = self._tool_version(name, _version_argument(rest[1] if len(rest) > 1 else None))
            if purge:
                self._confirm_purge(name)
            body = self._admin_request(
                ADMIN_TOOLS_DELETE_PATH,
                {"name": name, "version": version, "purge": purge, "confirm": True},
                method="POST",
            )
            done = "已彻底删除（purge）" if body.get("purged", purge) else "已删除"
            self.console.print(
                f"[green]{done} {name} v{body.get('version', version)}[/green]"
                "（仅影响注册表，不写 .env）"
            )
            return
        raise RuntimeError("用法：/tools [list] | /tools retire <名称> [版本] | /tools delete <名称> [版本] [--purge]")

    def _confirm_purge(self, name: str) -> None:
        """Hard delete asks for the tool name back -- the same shape as /perm unrestricted."""
        self.console.print(
            Panel(
                f"[red]--purge 是不可恢复的[/red]：会连同 {name} 的历史记录一起删除。\n"
                "确认请把工具名原样输入一遍。",
                title="[red]危险[/red]",
                border_style="red",
            )
        )
        answer = self.console.input(f"[bold red]输入 {name} 以确认硬删除：[/bold red]").strip()
        self.console.print()
        if answer != name:
            raise RuntimeError("确认不匹配：已取消硬删除（没有改动任何东西）")

    # ---------------------------------------------------------------------- voice
    def _transcribe(self, path: Path) -> dict[str, Any]:
        """Read a local audio file and hand it to the AI service's ``/asr``.

        The request itself lives in :func:`agent.cli.voice.transcribe`, which the
        real-time loop shares: one place owns the token, the URL and the error mapping.
        """
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"无法读取音频文件 {path}：{exc}") from exc
        if not content:
            raise RuntimeError(f"音频文件是空的：{path}")
        media_type = ASR_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return transcribe(content, media_type=media_type, language=self.state.asr_language or "")

    def _cmd_voice(self, args: list[str]) -> None:
        """Transcribe a local recording, then send the transcript as the next message."""
        if not args:
            raise RuntimeError("用法：/voice <音频文件>（支持 wav/mp3/ogg/webm 等）")
        path = Path(" ".join(args)).expanduser()
        if not path.is_file():
            raise RuntimeError(f"找不到音频文件：{path}")
        body = self._transcribe(path)
        text = str(body.get("text") or "").strip()
        if not text:
            self.console.print("[yellow]没有识别到语音内容（录音太短或全是静音？）[/yellow]")
            return
        detail = " · ".join(
            str(part)
            for part in (
                body.get("language") or "auto",
                f"{body['duration_s']:.1f}s" if isinstance(body.get("duration_s"), int | float) else None,
                body.get("model"),
            )
            if part
        )
        self.console.print(f"[bold cyan]识别到：[/bold cyan]{text}")
        if detail:
            self.console.print(f"[dim]（{detail}）[/dim]")
        self.state.pending_message = text
        self.console.print("[dim]已作为下一条消息发给 agent[/dim]")

    # ----------------------------------------------------------------- voice mode
    def _voice_status(self) -> Table:
        """What the real-time voice mode is doing right now (all of it local to this CLI)."""
        available, reason = sounddevice_available()
        executable = powershell_executable()
        labels = MODE_LABELS
        table = Table(title="语音模式", show_header=False, box=None)
        table.add_column(style="cyan")
        table.add_column()
        table.add_row("模式", labels.get(self.state.voice_mode, self.state.voice_mode))
        table.add_row("输入设备", settings.voice_input_device or "[dim]系统默认[/dim]")
        table.add_row("静音结束", f"{settings.voice_silence_s:g} 秒不说话就结束一句")
        table.add_row("最长录音", f"{settings.voice_max_s:g} 秒（超长截断）")
        table.add_row("VAD 底噪", f"{settings.voice_vad_floor:g}（低于它不算说话）")
        table.add_row(
            "朗读",
            f"{'开' if settings.voice_tts else '关'} · "
            f"{'SAPI 可用' if executable else '[yellow]未找到 powershell.exe[/yellow]'} · "
            f"最多 {settings.voice_tts_max_chars} 字",
        )
        table.add_row(
            "sounddevice",
            Text("已安装", style="green") if available else Text(reason, style="yellow"),
        )
        return table

    def _cmd_voice_mode(self, args: list[str]) -> None:
        """Turn the real-time loop on/off; the REPL runs it after this command returns."""
        action = args[0].lower() if args else "status"
        if action == "status":
            self.console.print(self._voice_status())
            self.console.print(
                "[dim]语音模式需要麦克风；识别走 AI 服务的 /asr（和 /voice 一样），"
                "回答用 Windows 自带的 SAPI 朗读。[/dim]"
            )
            return
        if action == "off":
            self.state.voice_mode = MODE_OFF
            self.console.print("[dim]语音模式：关[/dim]")
            return
        if action in {"on", "hands-free"}:
            available, reason = sounddevice_available()
            if not available:
                raise RuntimeError(reason)
            self.state.voice_mode = MODE_HANDS_FREE if action == "hands-free" else MODE_PUSH_TO_TALK
            if action == "hands-free":
                self.console.print(
                    "[yellow]免提模式：麦克风会一直开着，Ctrl-C 退出。[/yellow]"
                    "[dim]建议戴耳机，否则助手会把自己的朗读录回去。[/dim]"
                )
            else:
                self.console.print(
                    "[green]语音模式：开[/green] · [dim]按回车开始说话，每轮一次"
                    "（麦克风不会常开）[/dim]"
                )
            self.console.print(
                "[dim]需要麦克风。说完停顿一下自动结束；输入 q 或 /voice-mode off 退出。[/dim]"
            )
            return
        raise RuntimeError("用法：/voice-mode on|hands-free|off|status")

    def _cmd_voice_devices(self, args: list[str]) -> None:  # noqa: ARG002
        """List microphones and say whether the two optional pieces are available."""
        devices = list_input_devices()
        if devices and devices[0].get("error"):
            # the reason embeds an exception message: print it as text, not as markup
            self.err.print(Text(str(devices[0]["error"]), style="yellow"))
        else:
            self.console.print(devices_table(devices))
        available, _ = sounddevice_available()
        executable = powershell_executable()
        self.console.print(f"[dim]sounddevice：{'已安装' if available else '未安装（实时录音不可用）'}[/dim]")
        self.console.print(
            f"[dim]朗读（SAPI）：{'可用 · ' + executable if executable else '不可用（没找到 powershell.exe）'}[/dim]"
        )
        self.console.print(
            "[dim]把上面的序号或设备名填进 AGENT_VOICE_INPUT_DEVICE 即可指定设备"
            "（空 = 系统默认）。[/dim]"
        )

    def _cmd_speak(self, args: list[str]) -> None:
        """Toggle reading the answer aloud; a terminal-local setting, nothing is persisted."""
        if args:
            if args[0].lower() not in {"on", "off"}:
                raise RuntimeError("用法：/speak on|off")
            settings.voice_tts = args[0].lower() == "on"
        state = "[green]开[/green]" if settings.voice_tts else "[yellow]关[/yellow]"
        self.console.print(f"[dim]朗读：{state}[/dim]")
        self.console.print(
            f"[dim]用 Windows 自带的 SAPI（上限 {settings.voice_tts_max_chars} 字）；"
            "只影响这个终端，默认值来自 AGENT_VOICE_TTS。[/dim]"
        )

    # ---------------------------------------------------------------------- clear
    def _cmd_clear(self, args: list[str]) -> None:  # noqa: ARG002
        """Clear the terminal only: the session and the service are untouched."""
        self.console.clear()
        self.console.print("[dim]已清屏（会话未变，继续提问即可）[/dim]")

    # -------------------------------------------------------------------- session
    def _cmd_session(self, args: list[str]) -> None:  # noqa: ARG002
        self.console.print(f"[dim]会话：{self.state.session_id or '（新）'}[/dim]")

    def _cmd_new(self, args: list[str]) -> None:  # noqa: ARG002
        self.state.new_session = True
        self.state.session_id = None
        self.console.print("[dim]下一条消息会开始一个新会话[/dim]")

    def _cmd_exit(self, args: list[str]) -> None:  # noqa: ARG002
        self.state.exit_requested = True

    # ----------------------------------------------------------------------- stop
    def _stop_script(self) -> Path:
        """Where the host-side teardown lives: ``<repo>/deploy/windows/stop-agent.ps1``."""
        return project_root() / STOP_SCRIPT

    def _stop_failed(self, script: Path, code: int | None) -> None:
        """Say what was tried and how to do it by hand -- and leave the console running."""
        self.err.print(f"[red]关闭未完成[/red] 脚本：{script}")
        self.err.print(f"[red]退出码：{'（未运行）' if code is None else code}[/red]")
        self.err.print(f"[yellow]{STOP_MANUAL_HINT}[/yellow]")
        self.err.print("[dim]控制台继续运行：可以重试，或手动执行上面这条命令。[/dim]")

    def _run_stop_script(self, script: Path) -> int | None:
        """Run the teardown script, streaming its output; ``None`` if it cannot start.

        ``stderr`` is merged into ``stdout`` so the operator reads the shutdown in the
        order it happened.  Windows PowerShell 5.1 writes UTF-8 when its output is
        redirected (measured on this host); ``errors="replace"`` keeps an unexpected code
        page from raising halfway through a shutdown.
        """
        argv = stop_command(script)
        self.console.print(Text("运行：" + " ".join(argv), style="dim"))
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed binary, explicit argv
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.err.print(f"[red]无法启动关闭脚本：{exc}[/red]")
            return None
        with process:
            stream = process.stdout
            if stream is not None:
                for raw in stream:
                    self.console.print(Text(raw.decode("utf-8", "replace").rstrip("\r\n")))
        code = process.returncode
        return None if code is None else int(code)

    def _cmd_stop(self, args: list[str]) -> None:
        """Shut the whole stack down, then leave the chat -- a host-side action.

        Deliberately local: it never calls ``/admin/config`` (the AI service it is about
        to stop may already be down), and it only sets ``exit_requested`` once the
        teardown really succeeded, so a failure leaves the operator in control.
        """
        if [item.lower() for item in args] not in ([], ["now"]):
            raise RuntimeError("用法：/stop（先确认）或 /stop now（直接关闭）")
        script = self._stop_script()
        if not script.is_file():
            self._stop_failed(script, None)
            return
        self.console.print("[bold red]即将关闭 agentbox[/bold red]：AI 服务 + 平台 VM + 沙箱 VM + 控制平面")
        if args:
            self.console.print("[dim]/stop now：跳过确认[/dim]")
        else:
            answer = self.console.input(f"[bold red]{STOP_CONFIRM_PROMPT}[/bold red]")
            if str(answer).strip().lower() != "y":
                self.console.print("[yellow]已取消[/yellow]：没有关闭任何东西，控制台继续运行")
                return
        code = self._run_stop_script(script)
        if code != 0:
            self._stop_failed(script, code)
            return
        self.console.print("[green]agentbox 已关闭[/green]：AI 服务、平台 VM、沙箱 VM、控制平面都已停止")
        self.console.print("[dim]控制台退出（退出码 0）。[/dim]")
        self.state.exit_requested = True

    # -------------------------------------------------------------------- helpers
    def _apply(self, changes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        body = self._admin(changes)
        return body.get("effective", {}), body.get("applied", [])

    def _note(self, applied: list[str], persist: bool = True) -> None:
        """Report what changed; a real change is written to the VM's .env immediately.

        ``persist=False`` is for the purely local commands (/log), which have nothing to
        write.  A failed write only warns -- the runtime change itself is never undone.
        """
        for line in applied:
            self.console.print(f"[dim]  {line}[/dim]")
        if persist:
            self._sync_env(self._effective())
