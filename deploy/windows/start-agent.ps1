<#
.SYNOPSIS
  一键启动 agentbox：控制平面 → 平台 VM → AI 服务 → 沙箱预热池 → 自检 → 对话。

.DESCRIPTION
  幂等：已经在跑的东西不会重复启动。每一步都会打印它做了什么、当前状态，
  任何一步失败都会给出下一步该敲什么命令（不会静默停住）。

  顺序是有讲究的（2026-10 修正）：**控制平面必须先起来**。
  AI 服务的 /health 处理器会同步探测控制平面（gateway().health()），控制平面没起来
  它就等超时，所以“先等 :8090/health、后启动控制平面”会让第 2 步必然卡满超时。
  现在的顺序是：先起控制平面 → 再起平台 VM → 再等 AI 服务 active → 最后等 /health。

  流程（屏幕上是 [1/8] … [8/8]）：
    0. 前置检查（qemu / 沙箱镜像 / .env / SSH 密钥 / 平台磁盘）
    1. 控制平面没跑就启动，等 :8091/health 通（必须在 /health 之前）
    2. 平台 VM 没跑就启动（无头 + 串口日志），等 SSH 通
    3. AI 服务（平台 VM 内）：等 systemctl active，再等 :8090/health 通
    4. 确保 VM 内 8099 在发沙箱镜像（宿主缺镜像时用得上）
    5. 等沙箱预热池就绪（sandbox status 里出现 ready 的 VM）
    6. 跑 agent doctor，打印摘要
    7. 进交互对话（-NoChat 可跳过）

  显示：deploy\windows\ui.ps1 提供横幅 + 原地刷新的进度条 + 计时表（含子项）。
  输出被重定向（写日志、CI）时自动降级成带时间戳的整行文本，不会出现 `r 垃圾。
  每次运行都会写 var\logs\start-<yyyyMMdd-HHmmss>.log，含每一步的中间状态和耗时。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1 -NoChat
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1 -Message "在沙箱里跑 uname -a"
#>
[CmdletBinding()]
param(
    [string]$VmDir,
    [string]$QemuDir,
    [int]$MemoryMb = 6144,
    [int]$Cpus = 6,
    [int]$SshPort = 2222,
    [int]$AiPort = 8090,
    [int]$ControlPort = 8091,
    [int]$SeedPort = 8099,
    [int]$VmReadyTimeoutSec = 240,
    [int]$ServiceReadyTimeoutSec = 120,
    # AI 服务在 VM 冷启动后还要自己起 PostgreSQL + embedder，单独给它一个窗口。
    [int]$AiReadyTimeoutSec = 240,
    [int]$SandboxReadyTimeoutSec = 240,
    [string]$Message,
    [switch]$NoChat,
    [switch]$ForceRestartVm
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if (-not $VmDir) { $VmDir = Join-Path $repoRoot 'var\platform' }
if (-not $QemuDir) { $QemuDir = Join-Path $repoRoot 'qemu' }
$VmDir = [System.IO.Path]::GetFullPath($VmDir)

. (Join-Path $PSScriptRoot 'ui.ps1')

function Invoke-Native([scriptblock]$Block) {
    # 原生命令(stderr 有输出)在 $ErrorActionPreference='Stop' 下会抛 NativeCommandError，
    # 这里临时放开，把 stderr 合并进捕获的文本。
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        return (& $Block 2>&1 | Out-String)
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Die($text, $hint) {
    Stop-UiAgent -Text $text -Hint $hint
    Exit-Ui
    exit 1
}
function Ok($text, [switch]$Live) { Write-UiOk -Text $text -Live:$Live }
function Note($text, [switch]$Live) { Write-UiNote -Text $text -Live:$Live }
function Warn($text) { Write-UiWarn -Text $text }
function Bad($text) { Write-UiBad -Text $text }
function Show-Raw($text, $color = '') { Write-UiRaw -Text $text -Color $color }

$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$sshExe = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$sshKey = Join-Path $repoRoot 'var\vm_key'
$sshArgs = @('-i', $sshKey, '-p', "$SshPort", '-o', 'StrictHostKeyChecking=no',
             '-o', 'UserKnownHostsFile=NUL', '-o', 'LogLevel=ERROR', '-o', 'ConnectTimeout=8',
             'agent@127.0.0.1')

function RunRemote([string]$script, [int]$timeoutSec = 120) {
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($script -replace "`r`n", "`n")))
    $job = Start-Job -ScriptBlock {
        param($exe, $args_, $payload)
        $ErrorActionPreference = 'Continue'
        & $exe @args_ "echo $payload | base64 -d | bash -s" 2>&1
    } -ArgumentList $sshExe, $sshArgs, $b64
    if (Wait-Job $job -Timeout $timeoutSec) {
        $out = Receive-Job $job
    } else {
        Stop-Job $job -ErrorAction SilentlyContinue
        $out = "(ssh 超时 ${timeoutSec}s)"
    }
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    return ($out | Out-String)
}

function HttpOk([string]$url, [int]$timeoutSec = 4) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $timeoutSec
        return $r.StatusCode -eq 200
    } catch { return $false }
}

function PlatformVmProcess {
    Get-CimInstance Win32_Process -Filter "Name='qemu-system-x86_64.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$VmDir*platform.qcow2*" } | Select-Object -First 1
}

function ControlPlaneProcess {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*agent.cli*serve*control*' } | Select-Object -First 1
}

# ------------------------------------------------------------------ 显示准备
$logPath = Initialize-UiRun -LogName 'start' -RepoRoot $repoRoot -Title '一键启动 agentbox'
Set-UiSteps @('前置检查', "控制平面（:$ControlPort）", '平台 VM', 'AI 服务（:8090）',
              '沙箱镜像', '沙箱预热池', 'agent doctor 摘要', '对话')
Show-UiBanner -Title '一键启动' -Subtitle '控制平面 → 平台 VM → AI 服务 → 预热池 → 自检 → 对话' `
    -LogPath $logPath -Version 'v1'
Write-UiNote -Text '顺序说明：控制平面先起（AI 服务的 /health 会同步探测它，反了第 4 步必然等超时）'

# ---------------------------------------------------------------- 0. preflight
Start-UiStep -Index 1 -Title '前置检查' -Total 8
Update-UiStep -Percent 5 -Status '检查 qemu / 磁盘 / 密钥 / .env / 沙箱镜像…'
$qemu = Join-Path $QemuDir 'qemu-system-x86_64.exe'
if (-not (Test-Path -LiteralPath $qemu)) { Die "找不到 $qemu" '用 -QemuDir 指定 QEMU 目录' }
Ok "qemu: $qemu"

$disk = Join-Path $VmDir 'platform.qcow2'
if (-not (Test-Path -LiteralPath $disk)) {
    Die "平台磁盘不存在: $disk" '先跑 .\deploy\windows\provision-cloud-vm.ps1 （或 new-platform-vm.ps1）'
}
Ok "平台磁盘: $([math]::Round((Get-Item -LiteralPath $disk).Length / 1GB, 2)) GiB"

if (-not (Test-Path -LiteralPath "$sshKey.pub")) { Die "缺少 SSH 密钥 $sshKey.pub" '先跑 provision-cloud-vm.ps1 生成' }
Ok 'ssh 密钥: var\vm_key'

$envFile = Join-Path $repoRoot '.env'
if (-not (Test-Path -LiteralPath $envFile)) { Die '缺少 .env（控制平面配置）' '运行一次 provision-cloud-vm.ps1 会自动生成' }
$secret = (Select-String -Path $envFile -Pattern '^AGENT_CONTROL_SECRET=(.+)$' | Select-Object -First 1).Matches[0].Groups[1].Value
if (-not $secret) { Die '.env 里 AGENT_CONTROL_SECRET 为空' '删掉 .env 重跑 provision-cloud-vm.ps1，或手工填一个随机值' }
Ok "control secret: $($secret.Substring(0, 8))…（平台 VM 里必须一致）"

$sandboxRoot = Join-Path $repoRoot 'var\sandbox'
$needImage = @('vmlinuz', 'initrd.img', 'rootfs.img', 'workspace-blank.qcow2') |
    Where-Object { -not (Test-Path -LiteralPath (Join-Path $sandboxRoot $_)) }
if ($needImage.Count -gt 0) {
    Note "沙箱镜像缺 $($needImage -join ', ')；稍后尝试从平台 VM 拉取"
} else {
    Ok '沙箱镜像: 4 个文件齐全'
}
if (-not (Test-Path -LiteralPath $python)) { Die "缺少虚拟环境 $python" '先在仓库根执行: py -m venv .venv; .\.venv\Scripts\pip install -e ".[dev]"' }
Complete-UiStep -Detail 'qemu / 磁盘 / 密钥 / .env 都在'

# ---------------------------------------------------------- 1. control plane
# 必须先于 AI 服务 /health：/health 处理器会同步探测控制平面。
Start-UiStep -Index 2 -Title "控制平面（:$ControlPort）" -Total 8
Update-UiStep -Percent 5 -Status "检查端口 $ControlPort 有没有被平台 VM 的 hostfwd 抢走…"
# 端口冲突检查：以前给平台 VM 加过 8091 的 hostfwd，它会抢走宿主的 8091
$occupant = Get-NetTCPConnection -LocalPort $ControlPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($occupant) {
    $owner = Get-Process -Id $occupant.OwningProcess -ErrorAction SilentlyContinue
    if ($owner -and $owner.ProcessName -eq 'qemu-system-x86_64') {
        Update-UiStep -Percent 100 -Status '端口被 QEMU 占用'
        Fail-UiStep -Detail "端口 $ControlPort 被 QEMU hostfwd 占用"
        Die "端口 $ControlPort 被平台 VM 的 QEMU 转发占用（hostfwd 8091）" '从 run-platform-vm.ps1 里去掉 8091 的 hostfwd，然后重启平台 VM'
    }
    Note "端口 $ControlPort 已被 pid $($occupant.OwningProcess) 监听（可能是上一次跑的控制平面）"
}

$cp = ControlPlaneProcess
if ($cp) {
    Ok "已在运行 (pid $($cp.ProcessId))"
} else {
    Note '启动控制平面（日志: var\control.log）'
    Start-Process -WindowStyle Hidden -FilePath $python -ArgumentList @('-m', 'agent.cli', 'serve', 'control') `
        -RedirectStandardOutput (Join-Path $repoRoot 'var\control.log') `
        -RedirectStandardError (Join-Path $repoRoot 'var\control.err.log') | Out-Null
}
Start-UiSubStep -Title "等待 :$ControlPort/health"
$cpOk = $false
$subStart = Get-Date
$deadline = $subStart.AddSeconds($ServiceReadyTimeoutSec)
$poll = 0
while ((Get-Date) -lt $deadline) {
    $poll++
    $elapsedSec = ((Get-Date) - $subStart).TotalSeconds
    $left = [int][math]::Max(0, $ServiceReadyTimeoutSec - $elapsedSec)
    $pct = [int][math]::Min(99, [math]::Floor($elapsedSec * 100 / $ServiceReadyTimeoutSec))
    Update-UiStep -Percent $pct -Status "等待 http://127.0.0.1:$ControlPort/health… 第 $poll 次 · 超时 ${ServiceReadyTimeoutSec}s · 已用 $(Get-UiElapsedText $elapsedSec) · 还剩 ${left}s"
    if (HttpOk "http://127.0.0.1:$ControlPort/health" 4) { $cpOk = $true; break }
    Start-Sleep -Seconds 3
}
if (-not $cpOk) {
    $null = Complete-UiSubStep -Detail "第 $poll 次仍未通"
    Bad '控制平面 /health 不通'
    Get-Content (Join-Path $repoRoot 'var\control.err.log') -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Show-Raw "    $_" }
    Fail-UiStep -Detail "/health 在 ${ServiceReadyTimeoutSec}s 内没通"
    Die '控制平面启动失败' '常见原因: 8091 被占（netstat -ano | findstr 8091）或沙箱镜像缺失'
}
$cpSubSec = Complete-UiSubStep -Detail "第 $poll 次探测通"
Ok "health ok（$poll 次探测，$(Format-UiDuration $cpSubSec)）"
Complete-UiStep -Detail 'health ok'

# ------------------------------------------------------------- 2. platform VM
Start-UiStep -Index 3 -Title '平台 VM' -Total 8
$vm = PlatformVmProcess
if ($ForceRestartVm -and $vm) {
    Note "强制重启（pid $($vm.ProcessId)）"
    Stop-Process -Id $vm.ProcessId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    $vm = $null
}
if ($vm) {
    Ok "已在运行 (pid $($vm.ProcessId))"
} else {
    $runner = Join-Path $PSScriptRoot 'run-platform-vm.ps1'
    Note "启动中: $runner -Headless -MemoryMb $MemoryMb -Cpus $Cpus"
    Start-Process -WindowStyle Hidden -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $runner,
        '-Headless', '-MemoryMb', "$MemoryMb", '-Cpus', "$Cpus", '-SshPort', "$SshPort", '-VmDir', $VmDir
    ) | Out-Null
}

Start-UiSubStep -Title '等待 SSH'
$stepStart = Get-Date
$deadline = $stepStart.AddSeconds($VmReadyTimeoutSec)
$sshUp = $false
$attempt = 0
while ((Get-Date) -lt $deadline) {
    $attempt++
    $elapsedSec = ((Get-Date) - $stepStart).TotalSeconds
    $left = [int][math]::Max(0, $VmReadyTimeoutSec - $elapsedSec)
    $pct = [int][math]::Min(99, [math]::Floor($elapsedSec * 100 / $VmReadyTimeoutSec))
    Update-UiStep -Percent $pct -Status "等待 SSH… 第 $attempt 次探测 · 超时 ${VmReadyTimeoutSec}s · 已用 $(Get-UiElapsedText $elapsedSec) · 还剩 ${left}s"
    if ((RunRemote 'echo __READY__' 20) -match '__READY__') { $sshUp = $true; break }
    Start-Sleep -Seconds 4
}
if (-not $sshUp) {
    $null = Complete-UiSubStep -Detail "第 $attempt 次探测仍未通"
    Fail-UiStep -Detail "等不到 SSH（$VmReadyTimeoutSec 秒）"
    Die "等不到平台 VM 的 SSH（$VmReadyTimeoutSec 秒）" "看串口日志: $VmDir\console.log ；或先手动启动 run-platform-vm.ps1 看图形控制台"
}
$null = Complete-UiSubStep -Detail "第 $attempt 次探测通"
$uname = (RunRemote 'uname -sr; nproc' 20).Trim()
Ok "SSH 已通: $($uname -replace "`r`n", ' / ')"
Complete-UiStep -Detail "SSH 已通 · $(($uname -replace "`r`n", ' / ').Trim())"

# -------------------------------------------------------------- 3. ai service
Start-UiStep -Index 4 -Title 'AI 服务（:8090）' -Total 8
Start-UiSubStep -Title '等待 systemctl active'
$state = (RunRemote 'systemctl is-active agentbox-ai' 20).Trim()
if ($state -ne 'active') {
    Note "当前状态 '$state'，启动它（sudo systemctl start agentbox-ai）"
    $startSw = Get-Date
    RunRemote 'sudo systemctl start agentbox-ai' 60 | Out-Null
    # systemctl start 是同步的：返回时单元已经是 active（或失败），这里确认一次。
    $state = (RunRemote 'systemctl is-active agentbox-ai' 20).Trim()
    Note "systemctl start 返回后状态: '$state'（用了 $(Get-UiElapsedText $((Get-Date) - $startSw).TotalSeconds)）"
} else {
    Ok 'systemd 单元 active'
}
if ($state -ne 'active') {
    $null = Complete-UiSubStep -Detail "状态还是 '$state'"
    Bad "agentbox-ai 单元不是 active（$state）"
    $log = RunRemote 'sudo journalctl -u agentbox-ai -n 15 --no-pager | tail -15' 30
    Show-Raw $log
    Fail-UiStep -Detail "systemctl 状态 $state"
    Die 'AI 服务没起来' '在 VM 里看: sudo journalctl -u agentbox-ai -n 50 --no-pager'
}
$null = Complete-UiSubStep -Detail "状态 active"

# 子步骤 2：/health。它内部会同步探测控制平面，所以控制平面必须先起来（第 2 步已做）。
# 以前这一步最像“卡住”：每 3 秒探一次 /health，屏幕上一个字都不动。
Start-UiSubStep -Title "等待 :$AiPort/health"
$aiOk = $false
$subStart = Get-Date
$deadline = $subStart.AddSeconds($AiReadyTimeoutSec)
$poll = 0
while ((Get-Date) -lt $deadline) {
    $poll++
    $elapsedSec = ((Get-Date) - $subStart).TotalSeconds
    $left = [int][math]::Max(0, $AiReadyTimeoutSec - $elapsedSec)
    $pct = [int][math]::Min(99, [math]::Floor($elapsedSec * 100 / $AiReadyTimeoutSec))
    Update-UiStep -Percent $pct -Status "等待 http://127.0.0.1:$AiPort/health… 第 $poll 次 · 超时 ${AiReadyTimeoutSec}s · 已用 $(Get-UiElapsedText $elapsedSec) · 还剩 ${left}s（它内部要探控制平面）"
    if (HttpOk "http://127.0.0.1:$AiPort/health" 4) { $aiOk = $true; break }
    Start-Sleep -Seconds 3
}
if (-not $aiOk) {
    $null = Complete-UiSubStep -Detail "第 $poll 次仍未通"
    $log = RunRemote 'sudo journalctl -u agentbox-ai -n 15 --no-pager | tail -15' 30
    Bad 'AI 服务 /health 不通'
    Show-Raw $log
    Fail-UiStep -Detail "/health 在 ${AiReadyTimeoutSec}s 内没通"
    Die 'AI 服务启动失败' '在 VM 里看: sudo journalctl -u agentbox-ai -n 50 --no-pager；控制平面没起也会这样'
}
$null = Complete-UiSubStep -Detail "第 $poll 次探测通"
$health = (Invoke-WebRequest -Uri "http://127.0.0.1:$AiPort/health" -UseBasicParsing -TimeoutSec 10).Content | ConvertFrom-Json
Ok "health: db=$($health.db.ok) llm_key=$($health.llm.key) embedder=$($health.embedder.name)"
if (-not $health.llm.key) { Bad 'LLM API key 为空（AGENT_LLM_API_KEY）—— 对话会失败' }
Complete-UiStep -Detail "health ok · db=$($health.db.ok) embedder=$($health.embedder.name)"

# ------------------------------------------------------------ 4. sandbox seed
Start-UiStep -Index 5 -Title '沙箱镜像' -Total 8
Update-UiStep -Percent 10 -Status '检查 var\sandbox …'
if ($needImage.Count -gt 0) {
    Note "在 VM 里确保 8099 在发文件"
    Update-UiStep -Percent 35 -Status "让 VM 内的 8099 开始发文件…"
    RunRemote "sudo pkill -f 'http.server $SeedPort' 2>/dev/null; cd /var/lib/agentbox/sandbox 2>/dev/null && sudo bash -c 'nohup python3 -m http.server $SeedPort --bind 0.0.0.0 >/tmp/seed-http.log 2>&1 & echo started' || echo 'no image in vm'" 40 | Out-Null
    Start-Sleep -Seconds 2
    if (HttpOk "http://127.0.0.1:$SeedPort/") {
        Note '拉取到 var\sandbox ...'
        Update-UiStep -Percent 60 -Status '从平台 VM 拉取沙箱镜像（几百 MB，慢）…'
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'fetch-sandbox-image.ps1') -SandboxDir $sandboxRoot -SourceUrl "http://127.0.0.1:$SeedPort" | Out-Null
    } else {
        Fail-UiStep -Detail 'VM 里的 8099 取不到文件'
        Die "VM 里没有沙箱镜像（8099 取不到）" "在 VM 里执行: sudo /opt/agentbox/app/deploy/sandbox/build-sandbox-image.sh"
    }
}
Update-UiStep -Percent 90 -Status '校验 4 个镜像文件…'
$verify = Invoke-Native { & $python -m agent.cli image verify }
if ($verify -notmatch 'complete') { Bad $verify.Trim(); Fail-UiStep -Detail '镜像不完整'; Die '沙箱镜像不完整' '重跑 fetch-sandbox-image.ps1' }
Ok '镜像校验通过（vmlinuz / initrd.img / rootfs.img / workspace-blank.qcow2）'
Complete-UiStep -Detail '镜像校验通过'

# --------------------------------------------------------------- 5. warm pool
Start-UiStep -Index 6 -Title '沙箱预热池' -Total 8
$rpcHeaders = @{ 'Content-Type' = 'application/json'; 'X-Agent-Token' = $secret }
function Get-SandboxStatus {
    # /sandbox/status 需要 X-Agent-Token（只有 /health 免鉴权），所以走和 CLI 一样的 /rpc
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$ControlPort/rpc" -Method POST -Headers $rpcHeaders `
        -Body '{"method":"sandbox.status","params":{}}' -UseBasicParsing -TimeoutSec 10
    $parsed = $response.Content | ConvertFrom-Json
    if (-not $parsed.ok) { throw "rpc error: $($parsed.error.message)" }
    return $parsed.result
}
Start-UiSubStep -Title '等待沙箱 VM 预热'
$stepStart = Get-Date
$deadline = $stepStart.AddSeconds($SandboxReadyTimeoutSec)
$ready = $false
$lastError = ''
$poolSize = 0
$poll = 0
$readyVmId = ''
while ((Get-Date) -lt $deadline) {
    $poll++
    $elapsedSec = ((Get-Date) - $stepStart).TotalSeconds
    $left = [int][math]::Max(0, $SandboxReadyTimeoutSec - $elapsedSec)
    $pct = [int][math]::Min(99, [math]::Floor($elapsedSec * 100 / $SandboxReadyTimeoutSec))
    try {
        $status = Get-SandboxStatus
        $readyVms = @($status.vms | Where-Object { $_.state -eq 'ready' })
        $warm = [int]$status.warm
        if ([int]$status.pool_size -gt 0) { $poolSize = [int]$status.pool_size }
        $states = (@($status.vms | ForEach-Object { $_.state }) -join ',')
        if ($readyVms.Count -gt 0) {
            $ready = $true
            $readyVmId = $readyVms[0].vm_id
            Update-UiStep -Percent 100 -Status "ready VM: $readyVmId"
            Ok "accel=$($status.accel)  warm=$($status.warm)  active=$($status.active)  ready VM: $readyVmId"
            break
        }
        Update-UiStep -Percent $pct -Status "等待沙箱 VM 预热：ready $warm/$poolSize · 第 $poll 次查询 · 已用 $(Get-UiElapsedText $elapsedSec) · 还剩 ${left}s · 状态 [$(if ($states) { $states } else { '无 VM' })]"
        Note "等待 VM 就绪（当前 $($status.vms.Count) 台, 状态 $(if ($states) { $states } else { '无' })）"
    } catch {
        $lastError = $_.Exception.Message
        Update-UiStep -Percent $pct -Status "status 查询失败：$lastError · 已用 $(Get-UiElapsedText $elapsedSec) · 还剩 ${left}s"
        Note "status 查询失败: $lastError"
    }
    Start-Sleep -Seconds 10
}
if (-not $ready) {
    $null = Complete-UiSubStep -Detail "第 $poll 次查询仍未就绪"
    Bad "没有就绪的沙箱 VM（最后一次错误: $lastError）"
    Get-ChildItem (Join-Path $sandboxRoot 'console') -Filter '*.log' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notlike '*qemu*' } | Select-Object -Last 1 |
        ForEach-Object { Show-Raw "    --- $($_.Name) 尾部 ---"; Get-Content $_.FullName -Tail 15 | ForEach-Object { Show-Raw "    $_" } }
    Fail-UiStep -Detail '预热池在超时前没就绪'
    Die '沙箱 VM 起不来' '上面日志里通常能看到原因（guest panic = 镜像问题；WHPX 报错 = 换 -CpuType）'
}
$null = Complete-UiSubStep -Detail "第 $poll 次查询就绪"
$readyDetail = if ($readyVmId) { $readyVmId } else { @($status.vms | Where-Object { $_.state -eq 'ready' })[0].vm_id }
Complete-UiStep -Detail "ready VM: $readyDetail · warm=$($status.warm)/$poolSize"

# ------------------------------------------------------------------ 6. doctor
Start-UiStep -Index 7 -Title 'agent doctor 摘要' -Total 8
Update-UiStep -Percent 30 -Status '运行 agent.cli doctor…'
$doc = Invoke-Native { & $python -m agent.cli doctor }
# 只留 doctor 的表格行：原生命令的 stderr（警告）在 PS 5.1 里会被包成
# ErrorRecord，Out-String 后带一堆 “+ $doc = …” 噪声，过滤掉。
($doc -split "`n" |
    Where-Object { $_ -match 'python|qemu|accelerator|sandbox image|postgresql|embedder|llm|control plane|ai service' } |
    Where-Object { $_ -notmatch '^\s*\+' } |
    Where-Object { $_.Trim() -notmatch '^python\.exe\s*:' } |
    ForEach-Object { Show-Raw "    $($_.Trim())" })
Complete-UiStep -Detail 'doctor 摘要已打印'

# -------------------------------------------------------------------- 7. chat
if ($Message) {
    Start-UiStep -Index 8 -Title '执行一条消息' -Total 8
    Stop-UiStepLine
    & $python -m agent.cli chat -m $Message
    $chatCode = $LASTEXITCODE
    if ($chatCode -eq 0) { Complete-UiStep -Detail $Message } else { Fail-UiStep -Detail "chat 退出码 $chatCode" }
    Complete-UiRun
    Exit-Ui
    exit $chatCode
}
if ($NoChat) {
    Start-UiStep -Index 8 -Title '对话（-NoChat 跳过）' -Total 8
    Skip-UiStep -Detail '想对话: .\.venv\Scripts\python -m agent.cli chat'
    Note '跳过对话（-NoChat）'
    Note '想对话: .\.venv\Scripts\python -m agent.cli chat'
    Complete-UiRun
    Exit-Ui
    exit 0
}
Start-UiStep -Index 8 -Title '进入对话（/exit 退出）' -Total 8
Note '会话内可用: /new 新会话 · /session 看 id · /exit 退出'
Stop-UiStepLine
& $python -m agent.cli chat
Exit-Ui
