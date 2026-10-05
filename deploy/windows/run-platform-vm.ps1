<#
.SYNOPSIS
  启动已经装好的 Debian 平台 VM（AI 服务 + PostgreSQL）。

.DESCRIPTION
  用户态网络 + 端口转发（一律只绑宿主 127.0.0.1）：
    127.0.0.1:8090 -> guest 8090   AI 服务（Windows 侧的 CLI 通过它对话）
    127.0.0.1:8091 -> guest 8091   预留（也可以 ssh -p 8091）
    127.0.0.1:8099 -> guest 8099   在 VM 里起 http.server 8099，宿主用它拉沙箱镜像
  平台 VM 里的 AI 服务通过 10.0.2.2:8091 访问跑在 Windows 宿主上的控制平面。

  额外转发：仓库根 .env 里的 AGENT_PLATFORM_PORTS（逗号分隔的单个端口或闭区间，
  默认空 = 关闭，例如 2121,30000-30010）会各加一条 127.0.0.1:<port> -> guest <port>；
  显式绑定地址一律拒绝，445/139 也拒绝（Windows 宿主自己占着 SMB）。

  脚本放在任何目录都能执行，路径相对脚本自身解析。

  注意：WHPX 不能虚拟化 -cpu host / -cpu max（guest 会停住、串口零输出），
  所以默认 -cpu qemu64（实测 qemu64 正常启动、max 零输出）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-platform-vm.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-platform-vm.ps1
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-platform-vm.ps1 -Headless
#>
[CmdletBinding()]
param(
    [string]$VmDir,
    [string]$QemuDir,
    [int]$MemoryMb = 6144,
    [int]$Cpus = 6,
    [int]$SshPort = 2222,
    [string]$CpuModel = 'Nehalem',
    [switch]$Headless,
    # 交互式串口控制台端口（SSH 不通时的救命通道；0 = 关闭）
    [int]$SerialTcp = 0,
    [switch]$ForceTcg,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
# qemu64 保守但没有 SSE4.2：最新 numpy 等 x86-64-v2 wheel 会拒绝加载。
# Nehalem 实测在 WHPX 下可用且带 SSE4.2，所以默认它（host/max 仍然不可用）。
function Info($message) { Write-Host "[platform-vm] $message" -ForegroundColor Cyan }
function Fail($message) { Write-Host "[platform-vm] $message" -ForegroundColor Red; exit 1 }

# ------------------------------------------------- 额外端口转发（只绑宿主回环）
# AGENT_PLATFORM_PORTS 里每一项是一个端口或一个闭区间 a-b，逗号分隔，空白随意。
# 硬规矩：每项都只转发到宿主 127.0.0.1 —— 显式绑定地址（0.0.0.0、局域网地址）一律拒绝，
# 445/139 也拒绝（Windows 宿主自己占着 SMB）。
function ConvertFrom-PlatformPortSpec {
    [CmdletBinding()]
    param([string]$Spec)

    if ([string]::IsNullOrWhiteSpace($Spec)) { return @() }
    $ports = New-Object 'System.Collections.Generic.List[int]'
    foreach ($raw in ($Spec -split ',')) {
        $token = $raw.Trim()
        if (-not $token) { continue }
        if ($token.Contains(':')) {
            throw ("AGENT_PLATFORM_PORTS: '$token' 带了绑定地址。这里只允许宿主回环转发" +
                   "（hostfwd=tcp:127.0.0.1:<port>-:<port>），0.0.0.0 和局域网地址都不行；" +
                   "确实要绑别处，请自己改 deploy\windows\run-platform-vm.ps1。")
        }
        $token = $token -replace '\s', ''
        $lo = 0
        $hi = 0
        if ($token -match '^(\d+)-(\d+)$') {
            $okLo = [int]::TryParse($Matches[1], [ref]$lo)
            $okHi = [int]::TryParse($Matches[2], [ref]$hi)
            if (-not $okLo -or -not $okHi -or $lo -lt 1 -or $hi -gt 65535 -or $lo -gt $hi) {
                throw "AGENT_PLATFORM_PORTS: '$raw' 不是有效端口区间；要 1-65535 之间、从左到右升序，例如 30000-30010"
            }
        } elseif ($token -match '^\d+$') {
            if (-not [int]::TryParse($token, [ref]$lo) -or $lo -lt 1 -or $lo -gt 65535) {
                throw "AGENT_PLATFORM_PORTS: '$raw' 不是有效端口；要 1-65535 之间，例如 2121"
            }
            $hi = $lo
        } else {
            throw "AGENT_PLATFORM_PORTS: 看不懂 '$raw'；只接受逗号分隔的单个端口或闭区间，例如 2121,30000-30010,8080"
        }
        for ($p = $lo; $p -le $hi; $p++) {
            if ($p -eq 139 -or $p -eq 445) {
                throw ("AGENT_PLATFORM_PORTS: 拒绝 $p（来自 '$raw'）—— Windows 宿主自己占着 SMB（445/139），转发不上去。" +
                       "换一个宿主端口，例如用 8445 转发到 guest 的 445：hostfwd=tcp:127.0.0.1:8445-:445" +
                       "（宿主端口和目标端口不同，这一条要你自己在脚本里加）。")
            }
            $ports.Add($p)
        }
    }
    return @($ports | Select-Object -Unique)
}

function Get-PlatformForwardArgs {
    # -> @('hostfwd=tcp:127.0.0.1:2121-:2121', ...)；设置空 = 空数组
    [CmdletBinding()]
    param([string]$Spec)

    $forwards = @()
    foreach ($port in @(ConvertFrom-PlatformPortSpec -Spec $Spec)) {
        $forwards += "hostfwd=tcp:127.0.0.1:$port-:$port"
    }
    return @($forwards)
}

function Get-PlatformNicArg {
    # 平台 VM 的 -nic 参数：固定三条 + AGENT_PLATFORM_PORTS 的额外转发
    [CmdletBinding()]
    param([string]$Spec, [int]$SshPort = 2222)

    $parts = @(
        'user', 'model=virtio-net-pci',
        'hostfwd=tcp:127.0.0.1:8090-:8090',
        'hostfwd=tcp:127.0.0.1:8099-:8099',
        "hostfwd=tcp:127.0.0.1:$SshPort-:22"
    )
    foreach ($forward in @(Get-PlatformForwardArgs -Spec $Spec)) { $parts += $forward }
    return ($parts -join ',')
}

# 被点源（`. .\run-platform-vm.ps1`，测试这么调用）时只定义函数，不检查磁盘、不启动任何东西
if ($MyInvocation.InvocationName -eq '.') { return }

# $PSScriptRoot is not reliable inside param() defaults on Windows PowerShell 5.1
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if (-not $VmDir) { $VmDir = Join-Path $repoRoot 'var\platform' }
if (-not $QemuDir) { $QemuDir = Join-Path $repoRoot 'qemu' }
$VmDir = [System.IO.Path]::GetFullPath($VmDir)

$qemu = Join-Path $QemuDir 'qemu-system-x86_64.exe'
if (-not (Test-Path -LiteralPath $qemu)) {
    $found = Get-Command 'qemu-system-x86_64.exe' -ErrorAction SilentlyContinue
    if ($found) { $qemu = $found.Source }
}
if (-not (Test-Path -LiteralPath $qemu)) { Fail "找不到 qemu-system-x86_64.exe；用 -QemuDir 指定" }
$qemu = (Resolve-Path -LiteralPath $qemu).Path

$disk = Join-Path $VmDir 'platform.qcow2'
if (-not (Test-Path -LiteralPath $disk)) {
    Fail "没有找到磁盘 $disk`n先做其中一步：`n  powershell -ExecutionPolicy Bypass -File .\deploy\windows\provision-cloud-vm.ps1   （推荐，几分钟）`n  powershell -ExecutionPolicy Bypass -File .\deploy\windows\new-platform-vm.ps1       （用安装器，较慢）"
}

# WHPX + host/max = guest 停住（实测串口零输出），这里自动降级
if ($CpuModel -eq 'host' -or $CpuModel -eq 'max') {
    Info "cpu $CpuModel 在 WHPX 上不可用，改用 Nehalem"
    $CpuModel = 'Nehalem'
}

$accel = 'whpx'
if ($ForceTcg) { $accel = 'tcg' }
$display = @('-display', 'sdl')
$displayNote = 'sdl 图形窗口（可以直接登录 agent / agentbox）'
$serialNote = ''
if ($Headless) {
    $display = @('-display', 'none', '-serial', "file:$(Join-Path $VmDir 'console.log')")
    $displayNote = "none（串口日志: $(Join-Path $VmDir 'console.log')）"
}
if ($SerialTcp -gt 0) {
    # TCP 串口：可以用 vm-console.ps1 登进去敲命令（guest 被打满、sshd 无响应时）
    $display = @('-display', $(if ($Headless) { 'none' } else { 'sdl' }),
                 '-serial', "tcp:127.0.0.1:$SerialTcp,server=on,wait=off")
    $serialNote = "tcp:127.0.0.1:$SerialTcp（用 .\deploy\windows\vm-console.ps1 连）"
}

# 额外转发：环境变量优先，其次仓库根 .env（键名与 agent.config.Settings 一致）
$platformPorts = $env:AGENT_PLATFORM_PORTS
if (-not $platformPorts) {
    $envFile = Join-Path $repoRoot '.env'
    if (Test-Path -LiteralPath $envFile) {
        $hit = Select-String -Path $envFile -Pattern '^\s*AGENT_PLATFORM_PORTS=(.*)$' | Select-Object -First 1
        if ($hit) { $platformPorts = $hit.Matches[0].Groups[1].Value.Trim() }
    }
}
try {
    $extraForwards = @(Get-PlatformForwardArgs -Spec $platformPorts)
    $nicArg = Get-PlatformNicArg -Spec $platformPorts -SshPort $SshPort
} catch {
    Fail $_.Exception.Message
}

$qemuArgs = @(
    '-machine', 'q35', '-accel', $accel, '-cpu', $CpuModel,
    '-smp', "$Cpus", '-m', "$MemoryMb",
    '-drive', "file=$disk,if=virtio,format=qcow2,discard=unmap",
    '-nic', $nicArg,
    '-device', 'virtio-rng-pci',
    '-rtc', 'base=utc'
) + $display

# Warn before Windows starts swapping: the guest is much slower than a smaller
# VM that fits in RAM.
try {
    $availableGb = [math]::Round((Get-Counter '\Memory\Available MBytes' -ErrorAction Stop).CounterSamples[0].CookedValue / 1024, 1)
    if ($availableGb -lt [math]::Round($MemoryMb / 1024, 1)) {
        $top = (Get-Process | Sort-Object WS -Descending | Select-Object -First 3 |
                ForEach-Object { "$($_.ProcessName) $([math]::Round($_.WS / 1GB, 1))G" }) -join ', '
        Warn "宿主可用内存 $availableGb GB < 要给 VM 的 $([math]::Round($MemoryMb / 1024, 1)) GB"
        Warn "Windows 会开始用交换文件，VM 明显变慢。占用最多的进程: $top"
        Warn "先关掉一些程序，或改用 -MemoryMb 4096"
    }
} catch { }

Info "repo root    : $repoRoot"
Info "shell        : Windows PowerShell $($PSVersionTable.PSVersion)"
Info "qemu-system  : $qemu"
Info "disk         : $disk"
Info "accelerator  : $accel"
Info "cpu model    : $CpuModel"
Info "display      : $displayNote"
if ($serialNote) { Info "serial       : $serialNote" }
Info "ssh          : ssh -i var\vm_key -p $SshPort -o StrictHostKeyChecking=no agent@127.0.0.1"
if ($extraForwards.Count -gt 0) {
    # 映射写在屏幕上，免得"转发到底通没通"靠猜
    Info "额外转发     : 来自 AGENT_PLATFORM_PORTS，只绑宿主回环"
    Info ("  {0,-9} {1}" -f 'VM port', '-> host')
    foreach ($forward in $extraForwards) {
        $null = $forward -match '^hostfwd=tcp:127\.0\.0\.1:(\d+)-:(\d+)$'
        Info ("  {0,-9} {1}" -f $Matches[2], "-> host 127.0.0.1:$($Matches[1])")
    }
}

if ($DryRun) {
    Info "(dry-run) 将要执行的命令:"
    Write-Host ""
    Write-Host "  $qemu $($qemuArgs -join ' ')"
    Write-Host ""
    Info "(dry-run) 没有启动任何东西"
    exit 0
}

Info "启动中。VM 里可用: systemctl status agentbox-ai / .venv/bin/python -m agent.cli doctor"
& $qemu @qemuArgs
