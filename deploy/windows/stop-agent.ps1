<#
.SYNOPSIS
  优雅停止 agentbox：控制平面 → AI 服务 → 平台 VM。

.DESCRIPTION
  先让 VM 干净关机（sudo poweroff，QEMU 会随之退出，qcow2 不会脏），
  而不是直接掐掉 QEMU 进程。加 -Force 才强杀。

  显示沿用 deploy\windows\ui.ps1：进度条 + 计时表，输出被重定向时自动降级成整行文本。
  本次运行的输出会写进 var\logs\stop-<yyyyMMdd-HHmmss>.log。

  收尾一定会复核：QEMU 进程、8090/8091/8099/2222 端口都查一遍，
  只有都空了才说“已停止”——以前出现过报了没在跑、VM 却还占着 3 GB 的情况。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\stop-agent.ps1
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\stop-agent.ps1 -Force
#>
[CmdletBinding()]
param(
    [string]$VmDir,
    [int]$SshPort = 2222,
    [int]$AiPort = 8090,
    [int]$ControlPort = 8091,
    [int]$SeedPort = 8099,
    [int]$ShutdownTimeoutSec = 60,
    [switch]$Force,
    [switch]$KeepControlPlane
)

$ErrorActionPreference = 'Continue'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if (-not $VmDir) { $VmDir = Join-Path $repoRoot 'var\platform' }
$VmDir = [System.IO.Path]::GetFullPath($VmDir)

. (Join-Path $PSScriptRoot 'ui.ps1')

function Note($t, [switch]$Live) { Write-UiNote -Text $t -Live:$Live }
function Ok($t) { Write-UiOk -Text $t }
function Warn($t) { Write-UiWarn -Text $t }
function Bad($t) { Write-UiBad -Text $t }

function Invoke-Native([scriptblock]$Block) {
    # 原生命令(ssh 等)往 stderr 写东西时，$ErrorActionPreference='Stop' 会抛
    # NativeCommandError 直接中断脚本；这里临时放开并合并 stderr。
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { return (& $Block 2>&1 | Out-String) } finally { $ErrorActionPreference = $previous }
}

function Get-ListeningPort([int]$port) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
}

$logPath = Initialize-UiRun -LogName 'stop' -RepoRoot $repoRoot -Title '停止 agentbox'
if ($KeepControlPlane) {
    Set-UiSteps @('AI 服务 + 平台 VM 关机', '收尾复核')
} else {
    Set-UiSteps @('停控制平面', 'AI 服务 + 平台 VM 关机', '收尾复核')
}
Show-UiBanner -Title '一键停止' -Subtitle '控制平面 → AI 服务 → 平台 VM（优雅关机；-Force 才强杀）' `
    -LogPath $logPath -Version 'v1'

$stepCount = [int]$script:UiSteps.Count
$step = 0

# ------------------------------------------------------------ 1. control plane
$cpStopped = @()
if (-not $KeepControlPlane) {
    $step++
    Start-UiStep -Index $step -Title '停控制平面' -Total $stepCount
    Update-UiStep -Percent 20 -Status '查找 agent.cli serve control 进程…'
    $cpStopped = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*agent.cli*serve*control*' })
    foreach ($p in $cpStopped) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Ok "stopped pid $($p.ProcessId)"
    }
    if ($cpStopped.Count -eq 0) { Note '控制平面本来就没在跑' }
    Update-UiStep -Percent 80 -Status "确认 :$ControlPort 已释放…"
    $left = Get-ListeningPort $ControlPort
    if ($left) { Warn "端口 $ControlPort 还有监听（pid $($left.OwningProcess)），再看一眼" }
    Complete-UiStep -Detail $(if ($cpStopped.Count -gt 0) { "停了 $($cpStopped.Count) 个进程" } else { '本来就没在跑' })
}

# ------------------------------------------------------------- 2. platform VM
$step++
Start-UiStep -Index $step -Title 'AI 服务 + 平台 VM 关机' -Total $stepCount
$sshExe = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$sshKey = Join-Path $repoRoot 'var\vm_key'
$sshArgs = @('-i', $sshKey, '-p', "$SshPort", '-o', 'StrictHostKeyChecking=no',
             '-o', 'UserKnownHostsFile=NUL', '-o', 'LogLevel=ERROR', '-o', 'ConnectTimeout=8',
             'agent@127.0.0.1')

Update-UiStep -Percent 10 -Status '查找平台 VM 的 QEMU 进程…'
$allQemu = @(Get-CimInstance Win32_Process -Filter "Name='qemu-system-x86_64.exe'" -ErrorAction SilentlyContinue)
# The platform VM is the only QEMU that forwards host ports (hostfwd): the sandbox never
# does, which tests/unit/test_vm_argv.py pins.  Matching on the disk *file name* missed a
# manually started VM, so the script reported "not running" while that VM -- and the AI
# service inside it -- kept holding gigabytes of RAM.
$vm = $allQemu | Where-Object { $_.CommandLine -like '*hostfwd*' } | Select-Object -First 1
if (-not $vm) {
    $vm = $allQemu | Where-Object { $_.CommandLine -like "*$VmDir*platform.qcow2*" } | Select-Object -First 1
}
if (-not $vm) {
    Note '平台 VM 本来就没在跑'
    Skip-UiStep -Detail '平台 VM 本来就没在跑'
} else {
    $vmPid = [int]$vm.ProcessId
    $gone = $false
    if (-not $Force) {
        $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("sudo systemctl stop agentbox-ai; sudo poweroff"))
        Note '发送 sudo poweroff，等 QEMU 退出（最多 60 秒）'
        Update-UiStep -Percent 15 -Status '发送 sudo systemctl stop agentbox-ai; sudo poweroff …（串口可能要十几秒）'
        Invoke-Native { & $sshExe @sshArgs "echo $b64 | base64 -d | bash -s" } | Out-Null
        $stepStart = Get-Date
        $deadline = $stepStart.AddSeconds($ShutdownTimeoutSec)
        while ((Get-Date) -lt $deadline) {
            $elapsedSec = ((Get-Date) - $stepStart).TotalSeconds
            $leftSec = [int][math]::Max(0, $ShutdownTimeoutSec - $elapsedSec)
            if (-not (Get-Process -Id $vmPid -ErrorAction SilentlyContinue)) { $gone = $true; break }
            $pct = [int][math]::Min(99, 25 + [math]::Floor($elapsedSec * 60 / $ShutdownTimeoutSec))
            Update-UiStep -Percent $pct -Status "等待 VM 干净关机… 还剩 ${leftSec}s · 已用 $(Get-UiElapsedText $elapsedSec) · QEMU pid $vmPid 还在"
            Start-Sleep -Seconds 2
        }
        if ($gone) {
            Ok '平台 VM 已干净关机'
        } else {
            Note '60 秒没退出，改用强制结束'
        }
    }
    if (-not $gone) {
        Update-UiStep -Percent 90 -Status "强制结束 QEMU pid $vmPid …"
        Stop-Process -Id $vmPid -Force -ErrorAction SilentlyContinue
        Ok "已强制结束 QEMU pid $vmPid（qcow2 会在下次启动时自行恢复）"
    }
    Complete-UiStep -Detail $(if ($gone) { '干净关机' } else { '强制结束 QEMU' })
}

# ------------------------------------------------------------ 3. verify idle
$step++
Start-UiStep -Index $step -Title '收尾复核' -Total $stepCount
Update-UiStep -Percent 20 -Status '确认 qemu / 端口都空了…'
Start-Sleep -Seconds 2
$leftQemu = @(Get-CimInstance Win32_Process -Filter "Name='qemu-system-x86_64.exe'" -ErrorAction SilentlyContinue)
$leftSandbox = @($leftQemu | Where-Object { $_.CommandLine -notlike '*hostfwd*' })
$busyPorts = @()
foreach ($port in @($AiPort, $ControlPort, $SeedPort, $SshPort)) {
    $c = Get-ListeningPort $port
    if ($c) { $busyPorts += "$port (pid $($c.OwningProcess))" }
}

if ($leftQemu.Count -eq 0) {
    Ok 'qemu-system-x86_64: 没有进程'
} else {
    foreach ($q in $leftQemu) {
        Warn "还有 QEMU pid $($q.ProcessId)（$([math]::Round($q.WorkingSetSize / 1MB)) MB）"
    }
}
if ($leftSandbox.Count -gt 0) {
    Warn "其中 $($leftSandbox.Count) 个看起来是沙箱 VM（没有 hostfwd）——控制平面已经停了，它们不会自己退"
    foreach ($q in $leftSandbox) {
        Stop-Process -Id $q.ProcessId -Force -ErrorAction SilentlyContinue
        Ok "顺手结束了沙箱 VM pid $($q.ProcessId)"
    }
    $leftQemu = @(Get-CimInstance Win32_Process -Filter "Name='qemu-system-x86_64.exe'" -ErrorAction SilentlyContinue)
}
if ($busyPorts.Count -eq 0) {
    Ok "端口 ${AiPort}/${ControlPort}/${SeedPort}/${SshPort}: 都没在监听"
} else {
    Warn "这些端口还在监听: $($busyPorts -join ', ')"
}

$healthy = ($leftQemu.Count -eq 0 -and $busyPorts.Count -eq 0)
if ($healthy) {
    Complete-UiStep -Detail 'QEMU 与端口都已清空'
    Write-UiRaw ''
    Ok 'agentbox 已完全停止（qemu 进程与 8090/8091/8099/2222 都为空）'
} else {
    Fail-UiStep -Detail '还有残留，看上面的 Warn'
    Bad '还有东西没停干净，见上面 Warn 行'
    Stop-UiAgent -Text '' -Hint '再跑一次 stop-agent.ps1（或加 -Force），把上面的 pid 贴出来'
}
Complete-UiRun
Exit-Ui
if ($healthy) { exit 0 } else { exit 1 }
