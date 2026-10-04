<#
.SYNOPSIS
  一件启动 + 鲸鱼娘：起服务（如未在跑），然后用 whale 人格跟 agentbox 对话。

.DESCRIPTION
  这是一层薄封装，只做三件事：
    1. 若控制平面 :8091 或 AI 服务 :8090 没在跑，先调 deploy\windows\start-agent.ps1 -NoStart 之外的一键启动（-NoChat）；
    2. 设置 AGENT_PERSONA=whale（蓝鲸管家人格，见 src\agent\ai\personas\whale.md）；
    # 人格由 AI 服务端决定（不是这个进程）：通过 /admin/config 设置，它会写进 VM 的 .env
    $personaSet = $false
    try {
        $secretLine = Select-String -Path (Join-Path $PSScriptRoot '.env') -Pattern '^AGENT_CONTROL_SECRET=' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($secretLine) {
            $secret = ($secretLine.Line -replace '^AGENT_CONTROL_SECRET=', '').Trim()
            $body = '{"persona":"' + $Persona + '"}'
            Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8090/admin/config' `
                -Headers @{ 'X-Agent-Token' = $secret } -ContentType 'application/json' -Body $body -TimeoutSec 20 | Out-Null
            Write-Host "  已设置人格：$Persona（服务端，已持久化到 VM 的 .env）" -ForegroundColor Green
            $personaSet = $true
        }
    } catch {
        Write-Host "  人格设置失败（可在对话里用 /persona $Persona 手动切换）：$_" -ForegroundColor Yellow
    }
    if (-not $personaSet) { Write-Host "  提示：进去后可用 /persona $Persona 切换人格" -ForegroundColor DarkGray }

    3. 用仓库自己的 .venv\Scripts\python.exe 跑 python -m agent.cli chat。

  它不改任何配置、不碰 .env、不重启已经在跑的服务；服务启动的细节与耗时表
  都在 start-agent.ps1 里（日志 var\logs\start-*.log）。

.PARAMETER Voice
  进入语音模式（等价 chat --voice）：按回车或 Ctrl+T 说话，回答朗读出来。需要麦克风。

.PARAMETER NoSpeak
  不朗读回答（等价 chat --no-speak）。装不了音箱、或不想被念的时候用。

.PARAMETER Message
  非交互跑一轮就退出（等价 chat --message "…"）。适合脚本/自检。

.PARAMETER NoStart
  不自动启服务：服务没在跑就直接报错退出（用于"我自己管服务"的场景）。

.PARAMETER Persona
  换一个人格名，默认 whale（按目录扫描自动发现，可填 engineer / roleplay / …）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\whale-girl.ps1
  powershell -ExecutionPolicy Bypass -File .\whale-girl.ps1 -Message "在沙箱里跑 uname -a"
  powershell -ExecutionPolicy Bypass -File .\whale-girl.ps1 -Voice
  powershell -ExecutionPolicy Bypass -File .\whale-girl.ps1 -NoStart -NoSpeak

.NOTES
  Windows PowerShell 5.1 兼容（本机只有 5.1，没有 pwsh）；本文件是 UTF-8 with BOM。
#>
[CmdletBinding()]
param(
    [switch]$Voice,
    [switch]$NoSpeak,
    [string]$Message,
    [switch]$NoStart,
    [string]$Persona = 'whale',
    [int]$AiPort = 8090,
    [int]$ControlPort = 8091,
    # 用法说明。注意：`-?` 这个名字在 Windows PowerShell 5.1 里会被 PowerShell 自己
    # 吃掉（实测连脚本都不加载，直接报"无法加载文件"），所以它只是别名之一；
    # 可靠写法是 -Usage / -Help / -h；不带任何参数 = 直接启动（默认文字对话）。
    [Alias('h', '?')][switch]$Usage,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$starter = Join-Path $repoRoot 'deploy\windows\start-agent.ps1'

function Show-Usage {
    Write-Host ''
    Write-Host '  whale-girl.ps1 — 一件启动 + 鲸鱼娘（agentbox 的蓝鲸管家人格）' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '  用法：powershell -ExecutionPolicy Bypass -File .\whale-girl.ps1 [选项]' -ForegroundColor White
    Write-Host ''
    Write-Host '    -Voice              语音模式（按回车/Ctrl+T 说话，回答朗读；需要麦克风）'
    Write-Host '    -NoSpeak            不朗读回答'
    Write-Host '    -Message "…"        非交互跑一轮就退出'
    Write-Host '    -NoStart            不自动启服务（服务没跑就直接报错）'
    Write-Host '    -Persona <名字>      换人格，默认 whale'
    Write-Host '    -Usage / -Help      本说明（不带参数 = 直接启动鲸鱼娘管家）'
    Write-Host ''
    Write-Host '  例：.\whale-girl.ps1 -Message "在沙箱里跑 uname -a"' -ForegroundColor DarkGray
    Write-Host '  例：.\whale-girl.ps1 -Voice -NoSpeak' -ForegroundColor DarkGray
    Write-Host ''
}

# 不带参数 = 只想看用法（不会去启动 VM / 服务，这点很重要）。
if ($Usage -or $Help) {
    Show-Usage
    exit 0
}

function Fail([string]$text, [string]$hint) {
    Write-Host "  FAIL $text" -ForegroundColor Red
    if ($hint) { Write-Host "  提示: $hint" -ForegroundColor Yellow }
    Write-Host ''
    exit 1
}

function Test-Listen([int]$port) {
    # 只看本机有没有人在听这个端口：AI 服务在平台 VM 里，经 hostfwd 映射到 127.0.0.1。
    $found = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    return [bool]$found
}

function Http-Ok([string]$url) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
        return $r.StatusCode -eq 200
    } catch { return $false }
}

# ------------------------------------------------------------------ 0. 前置检查
if (-not (Test-Path -LiteralPath $python)) {
    Fail "缺少虚拟环境 $python" '先在仓库根执行: py -m venv .venv; .\.venv\Scripts\pip install -e ".[dev]"'
}
if ($Voice -and $NoSpeak) {
    Write-Host '  注意: -Voice 与 -NoSpeak 同时给出 -> 说话但不朗读' -ForegroundColor Yellow
}
if ($Message -and $Voice) {
    Write-Host '  注意: -Message 是非交互单轮，-Voice 会被忽略' -ForegroundColor Yellow
}

$personaFile = Join-Path $repoRoot "src\agent\ai\personas\$Persona.md"
if (-not (Test-Path -LiteralPath $personaFile)) {
    Write-Host "  注意: 没有内置人格文件 $personaFile" -ForegroundColor Yellow
    Write-Host "        （操作员人格也可以放在 $repoRoot\personas\$Persona.md；服务会把未知名字降级到 engineer）" -ForegroundColor DarkGray
}

# ------------------------------------------------------------------ 1. 服务
$aiUp = Test-Listen $AiPort
$cpUp = Test-Listen $ControlPort
if ($aiUp -and $cpUp) {
    Write-Host "  服务已在运行（AI :$AiPort · 控制平面 :$ControlPort），跳过启动" -ForegroundColor Green
} elseif ($NoStart) {
    Fail "服务没在跑（AI :$AiPort = $aiUp · 控制平面 :$ControlPort = $cpUp），而 -NoStart 要求不自动启动" `
        '去掉 -NoStart，或先手工: powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1 -NoChat'
} else {
    Write-Host "  服务没在跑（AI :$AiPort = $aiUp · 控制平面 :$ControlPort = $cpUp），先一键启动…" -ForegroundColor Cyan
    if (-not (Test-Path -LiteralPath $starter)) {
        Fail "找不到 $starter" '仓库不完整：deploy\windows\start-agent.ps1 是必需的'
    }
    Write-Host ('  -> powershell -ExecutionPolicy Bypass -File "{0}" -NoChat' -f $starter) -ForegroundColor DarkGray
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $starter -NoChat
    if ($LASTEXITCODE -ne 0) {
        Fail "start-agent.ps1 退出码 $LASTEXITCODE（启动没成功）" `
            '看 var\logs\start-*.log 的最后几行；VM 卡住用 .\deploy\windows\stop-agent.ps1 再重试'
    }
    if (-not (Http-Ok "http://127.0.0.1:$AiPort/health")) {
        Fail "启动脚本说成功了，但 http://127.0.0.1:$AiPort/health 不通" `
            '平台 VM 里的 AI 服务还没起来：ssh -i var\vm_key -p 2222 agent@127.0.0.1 "sudo systemctl status agentbox-ai"'
    }
    Write-Host "  服务已就绪（:$AiPort/health ok）" -ForegroundColor Green
}

# ------------------------------------------------------------------ 2. 鲸鱼娘 + 对话
$env:AGENT_PERSONA = $Persona
if (-not $env:PYTHONIOENCODING) { $env:PYTHONIOENCODING = 'utf-8' }

# 人格由 AI 服务端决定（本进程里的 AGENT_PERSONA 不生效）：通过 /admin/config 设置
$body = '{"set":{"persona":"' + $Persona + '"}}'
try {
    $secretLine = Select-String -Path (Join-Path $PSScriptRoot '.env') -Pattern '^AGENT_CONTROL_SECRET=' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($secretLine) {
        $secret = ($secretLine.Line -replace '^AGENT_CONTROL_SECRET=', '').Trim()
        Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8090/admin/config' `
            -Headers @{ 'X-Agent-Token' = $secret } -ContentType 'application/json' -Body $body -TimeoutSec 20 | Out-Null
        Write-Host "  已设置人格：$Persona（服务端，已持久化到 VM 的 .env）" -ForegroundColor Green
    } else {
        Write-Host "  .env 里没有 AGENT_CONTROL_SECRET，进去后用 /persona $Persona 切换" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  人格设置失败（进去后用 /persona $Persona 切换）：$_" -ForegroundColor Yellow
}

$chatArgs = @('-m', 'agent.cli', 'chat')
if ($Voice -and -not $Message) { $chatArgs += '--voice' }
if ($NoSpeak) { $chatArgs += '--no-speak' }
if ($Message) { $chatArgs += @('--message', $Message) }

Write-Host ''
Write-Host '  🐋 鲸鱼娘管家已就位' -ForegroundColor Cyan
Write-Host "     人格: $Persona  (AGENT_PERSONA=$Persona)" -ForegroundColor DarkGray
Write-Host "     命令: $python $($chatArgs -join ' ')" -ForegroundColor DarkGray
if (-not $Message) {
    Write-Host '     /help 看命令 · /persona 看当前人格 · /persona off 取消人格 · /exit 退出' -ForegroundColor DarkGray
}
Write-Host ''

& $python @chatArgs
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ''
    if ($code -eq 2) {
        Fail "chat 退出码 2（命令行参数被拒绝）" "人工核对: $python -m agent.cli chat --help"
    }
    Fail "chat 退出码 $code" `
        'AI 服务不可用 / 会话被网关拒绝时看这里：curl http://127.0.0.1:8090/health ; 日志在平台 VM: journalctl -u agentbox-ai -n 50'
}
exit 0