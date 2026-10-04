<#
.SYNOPSIS
  用 Debian netinst ISO 安装平台 VM（备选路径；推荐用 provision-cloud-vm.ps1）。

.DESCRIPTION
  全自动安装：从 ISO 抽出安装器内核 -> -kernel/-initrd 直启（只有这样才能传内核参数）
  -> 安装器从宿主上的 HTTP 服务拉取渲染好的 preseed，自动分区、装包。

  apt 源默认换成清华 TUNA：同一台机器实测 deb.debian.org 0 KiB/s（超时重试），
  TUNA 19 MiB/s —— 这就是之前安装"卡在下载"的原因。

  -Mode manual    从 ISO 图形启动、手动安装。
  -Gui            无头 preseed 安装也能看见 QEMU 窗口（会把 d-i 的界面放到 VGA 上）。
  -Follow         另开窗口实时 tail 串口日志。
  -InstallTimeoutMinutes  安装总超时，到点强制结束 QEMU 并打印日志尾部。

  脚本可以在任何目录执行，所有路径相对脚本自身解析。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\new-platform-vm.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\new-platform-vm.ps1 -Follow
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\new-platform-vm.ps1 -Gui
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\new-platform-vm.ps1 -Mirror aliyun
#>
[CmdletBinding()]
param(
    [ValidateSet('preseed', 'manual')]
    [string]$Mode = 'preseed',
    [ValidateSet('tuna', 'aliyun', 'official', 'custom')]
    [string]$Mirror = 'tuna',
    [string]$MirrorUri,
    [string]$VmDir,
    [string]$Iso,
    [string]$QemuDir,
    [int]$MemoryMb = 4096,
    [int]$Cpus = 4,
    [int]$DiskGb = 40,
    [int]$HttpPort = 8899,
    # WHPX 上唯一稳定的选择：'host' 和 'max' 都会让 guest 立刻停住（串口 0 字节）。
    # 实测（QEMU 11.1.0 / Windows / WHPX，同一内核同一参数）：
    #   -cpu qemu64 -> 正常启动，串口输出 47 KB
    #   -cpu max    -> guest halt，串口输出 0 字节
    # 日志里那条 "host doesn't support requested feature: CPUID[...].ECX.svm" 只是警告。
    [string]$CpuModel = 'Nehalem',
    # 真想试 host/max 就加这个开关（默认会自动降级回 qemu64 并警告）。
    [switch]$ForceCpu,
    [int]$InstallTimeoutMinutes = 90,
    [switch]$Gui,
    [switch]$Follow,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# 全局持有子进程句柄，任何退出路径（成功/失败/超时）都能清理干净，
# 避免 python.exe / qemu 残留把端口或磁盘占住。
$script:httpJob = $null
$script:tailJob = $null
$script:qemuProc = $null

# qemu64 保守但没有 SSE4.2：最新 numpy 等 x86-64-v2 wheel 会拒绝加载。
# Nehalem 实测在 WHPX 下可用且带 SSE4.2，所以默认它（host/max 仍然不可用）。
function Info($message) { Write-Host "[platform-vm] $message" -ForegroundColor Cyan }
function Warn($message) { Write-Host "[platform-vm] $message" -ForegroundColor Yellow }

function Stop-AuxProcesses {
    if ($script:httpJob) {
        & taskkill /PID $script:httpJob.Id /T /F 2>$null | Out-Null
        $script:httpJob = $null
    }
    if ($script:tailJob) {
        & taskkill /PID $script:tailJob.Id /T /F 2>$null | Out-Null
        $script:tailJob = $null
    }
    if ($script:qemuProc -and -not $script:qemuProc.HasExited) {
        Stop-Process -Id $script:qemuProc.Id -Force -ErrorAction SilentlyContinue
        $script:qemuProc = $null
    }
}

function Fail($message) {
    Stop-AuxProcesses
    Write-Host "[platform-vm] $message" -ForegroundColor Red
    exit 1
}

# --------------------------------------------------------------------------
# 0. resolve the repository layout
#    ($PSScriptRoot is not reliable inside param() defaults on Windows
#     PowerShell 5.1, so every default is computed here instead)
# --------------------------------------------------------------------------
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if (-not $VmDir) { $VmDir = Join-Path $repoRoot 'var\platform' }
if (-not $Iso) { $Iso = Join-Path $repoRoot 'debian-13.7.0-amd64-netinst.iso' }
if (-not $QemuDir) { $QemuDir = Join-Path $repoRoot 'qemu' }
$VmDir = [System.IO.Path]::GetFullPath($VmDir)

# --------------------------------------------------------------------------
# 1. resolve the tools we need
# --------------------------------------------------------------------------
$qemu = Join-Path $QemuDir 'qemu-system-x86_64.exe'
if (-not (Test-Path -LiteralPath $qemu)) {
    $found = Get-Command 'qemu-system-x86_64.exe' -ErrorAction SilentlyContinue
    if ($found) { $qemu = $found.Source }
}
if (-not (Test-Path -LiteralPath $qemu)) {
    Fail "找不到 qemu-system-x86_64.exe；用 -QemuDir 指定目录（当前找的是 $QemuDir）"
}

$qemuImg = Join-Path $QemuDir 'qemu-img.exe'
if (-not (Test-Path -LiteralPath $qemuImg)) {
    $found = Get-Command 'qemu-img.exe' -ErrorAction SilentlyContinue
    if ($found) { $qemuImg = $found.Source }
}
if (-not (Test-Path -LiteralPath $qemuImg)) { Fail "找不到 qemu-img.exe；用 -QemuDir 指定目录" }

if (-not (Test-Path -LiteralPath $Iso)) {
    Fail "找不到 ISO: $Iso`n工作区里应有 debian-13.7.0-amd64-netinst.iso，或用 -Iso 指定"
}

$python = Get-Command 'py.exe' -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command 'python.exe' -ErrorAction SilentlyContinue }
if (-not $python) { Fail "需要一个 Python 解释器（py 或 python）来提供 preseed 并抽取安装器内核" }

# py.exe 只是启动器：用真正的解释器，这样 Start-Process 返回的就是 http.server 本体，
# 杀掉它不会留下子进程（早期版本因此泄漏过服务，把端口占住导致后续静默失败）。
$pythonReal = $python.Source
try {
    $resolved = & $python.Source -c "import sys; print(sys.executable)" 2>$null
    if ($resolved -and (Test-Path -LiteralPath $resolved.Trim())) { $pythonReal = $resolved.Trim() }
} catch { }

function Wait-SeedFile($url, $timeoutSec) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -eq 200) { return $true }
        } catch { Start-Sleep -Milliseconds 400 }
    }
    return $false
}

function Assert-PortFree([int[]]$ports) {
    foreach ($port in $ports) {
        $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($listener) {
            $owner = (Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue).ProcessName
            Fail "端口 $port 已被占用（PID $($listener.OwningProcess) / $owner）。`n先清理（需要管理员权限）：`n  Stop-Process -Id $($listener.OwningProcess) -Force`n或者换端口： -HttpPort 8901"
        }
    }
}

$qemu = (Resolve-Path -LiteralPath $qemu).Path
$qemuImg = (Resolve-Path -LiteralPath $qemuImg).Path
$Iso = (Resolve-Path -LiteralPath $Iso).Path
$disk = Join-Path $VmDir 'platform.qcow2'
$installLog = Join-Path $VmDir 'install-console.log'

Info "repo root    : $repoRoot"
Info "shell        : Windows PowerShell $($PSVersionTable.PSVersion)"
Info "qemu-system  : $qemu"
Info "qemu-img     : $qemuImg"
Info "python       : $pythonReal"
Info "installer ISO: $Iso"
Info "VM directory : $VmDir"
Info "mode         : $Mode"
if ($Gui) { Info "gui          : 开（会弹出 QEMU 窗口）" } else { Info "gui          : 关（无头，看串口日志）" }

# --------------------------------------------------------------------------
# 2. pick an accelerator (WHPX cannot virtualise -cpu host/max)
# --------------------------------------------------------------------------
$accel = 'whpx'

# CPU 型号护栏放在探测之前，这样 -DryRun 也能验证到：
# WHPX + host/max = guest 立刻停住（串口零输出）。没有 -ForceCpu 时自动降级，
# 免得踩同一个坑却只看到"安装器没反应"。
if ($CpuModel -eq 'host' -or $CpuModel -eq 'max') {
    if ($ForceCpu) {
        Warn "-ForceCpu 已指定：仍然使用 -cpu $CpuModel。若串口日志 0 字节，换 -CpuModel qemu64"
    } else {
        Warn "-cpu $CpuModel 在 WHPX 上会让 guest 停住（实测串口输出 0 字节），自动改用 Nehalem"
        Warn "（确实想试就加 -ForceCpu；更稳的替代是 Nehalem）"
        $CpuModel = 'Nehalem'
    }
}

if ($DryRun) {
    $accel = '(dry-run: 会先探测 whpx，失败则 tcg)'
} else {
    $probe = Start-Job -ScriptBlock { param($q) & $q -accel whpx -machine q35 -m 128 -display none -nodefaults -S 2>&1 } -ArgumentList $qemu
    if (Wait-Job $probe -Timeout 6) {
        $probeOutput = Receive-Job $probe
        Warn "WHPX 不可用，退回 tcg（纯软件模拟，安装会慢很多）: $($probeOutput -join ' ')"
        $accel = 'tcg'
    }
    Stop-Job $probe -ErrorAction SilentlyContinue
    Remove-Job $probe -Force -ErrorAction SilentlyContinue
}
Info "accelerator  : $accel"
Info "cpu model    : $CpuModel"

# --------------------------------------------------------------------------
# 3. build the QEMU command line
# --------------------------------------------------------------------------
$bootArgs = @()
$serveDir = Join-Path $VmDir 'seed'
$mirrorHost = 'mirrors.tuna.tsinghua.edu.cn'

if ($Mode -eq 'preseed') {
    # Render the preseed with a mirror that is actually reachable.
    switch ($Mirror) {
        'aliyun' { $mirrorHost = 'mirrors.aliyun.com'; $mirrorDir = '/debian'; $secHost = 'mirrors.aliyun.com'; $secDir = '/debian-security' }
        'official' { $mirrorHost = 'deb.debian.org'; $mirrorDir = '/debian'; $secHost = 'security.debian.org'; $secDir = '/debian-security' }
        default { $mirrorHost = 'mirrors.tuna.tsinghua.edu.cn'; $mirrorDir = '/debian'; $secHost = 'mirrors.tuna.tsinghua.edu.cn'; $secDir = '/debian-security' }
    }
    if ($MirrorUri) {
        $stripped = $MirrorUri -replace '^https?://', ''
        $mirrorHost = ($stripped -split '/')[0]
        $mirrorDir = '/' + (($stripped -split '/', 2)[1])
        $secHost = $mirrorHost
        $secDir = "$mirrorDir-security"
    }

    $template = Join-Path $PSScriptRoot 'preseed.cfg.tpl'
    if (-not (Test-Path -LiteralPath $template)) { Fail "找不到 preseed.cfg.tpl: $template" }
    $rendered = (Get-Content -Raw -LiteralPath $template).
        Replace('@MIRROR_HOST@', $mirrorHost).
        Replace('@MIRROR_DIR@', $mirrorDir).
        Replace('@SECURITY_HOST@', $secHost).
        Replace('@SECURITY_DIR@', $secDir)

    Info "apt mirror   : $mirrorHost$mirrorDir"
    Info "security     : $secHost$secDir"

    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $serveDir | Out-Null
        # BOM-less UTF-8：PS 5.1 的 Set-Content -Encoding UTF8 会写 BOM，
        # 而安装器/cloud-init 解析时会被 BOM 干扰，所以显式写成无 BOM。
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText((Join-Path $serveDir 'preseed.cfg'), $rendered, $utf8NoBom)
        $tar = (Get-Command 'tar.exe' -ErrorAction SilentlyContinue).Source
        if ($tar) {
            $tarball = Join-Path $serveDir 'agentbox.tar.gz'
            Remove-Item -LiteralPath $tarball -ErrorAction SilentlyContinue
            & $tar -czf $tarball --exclude=.venv --exclude=var --exclude=qemu --exclude=*.iso `
                --exclude=__pycache__ --exclude=.git --exclude=.pytest_cache --exclude=.ruff_cache `
                -C $repoRoot src deploy tests pyproject.toml README.md .env.example .gitignore
            if (Test-Path -LiteralPath $tarball) {
                Info "repo tarball : $([math]::Round((Get-Item -LiteralPath $tarball).Length / 1KB, 0)) KiB (装完自动解到 /opt/agentbox/app)"
            }
        }
    }

    # The installer kernel must be started with -kernel/-initrd: only then does
    # QEMU honour -append, which is the only way to hand d-i its preseed URL.
    $extractor = Join-Path $PSScriptRoot 'iso_extract.py'
    if (-not (Test-Path -LiteralPath $extractor)) { Fail "找不到 iso_extract.py: $extractor" }
    $kernelPath = Join-Path $VmDir 'vmlinuz'
    $initrdPath = Join-Path $VmDir 'initrd.gz'

    if (-not (Test-Path -LiteralPath $kernelPath) -or -not (Test-Path -LiteralPath $initrdPath)) {
        if ($DryRun) {
            Info "(dry-run) 将执行: $pythonReal `"$extractor`" `"$Iso`" `"$VmDir`"   （抽出 vmlinuz + initrd.gz）"
        } else {
            Info "extracting   : 从 ISO 抽安装器内核到 $VmDir"
            New-Item -ItemType Directory -Force -Path $VmDir | Out-Null
            & $pythonReal $extractor $Iso $VmDir
            if ($LASTEXITCODE -ne 0) { Fail "从 ISO 抽取安装器内核失败" }
        }
    } else {
        Info "reusing      : $kernelPath / $initrdPath"
    }

    # 如果宿主设了 HTTP_PROXY，顺手传给 d-i（企业网里很有用）。
    $proxyVal = $env:HTTP_PROXY
    if (-not $proxyVal) { $proxyVal = $env:http_proxy }
    if ($proxyVal) { Info "http proxy   : $proxyVal （会传给 d-i）" } else { $proxyVal = '' }

    # 关键点：
    #   earlyprintk/ignore_loglevel  让最早期内核日志也落到串口，便于诊断
    #   mirror/* 内核参数是双保险：即使 preseed 没拉到，也不会跑去连官方源
    #   console 顺序有讲究 —— 最后一个 console= 才是 /dev/console：
    #     -Gui 时最后放 tty0，d-i 的界面才会出现在 QEMU 窗口里；
    #     无头时最后放 ttyS0，界面和日志都进串口文件。
    $appendParts = @(
        'auto=true',
        'priority=critical',
        "preseed/url=http://10.0.2.2:$HttpPort/preseed.cfg",
        'netcfg/choose_interface=auto',
        'netcfg/dhcp_timeout=60',
        'netcfg/confirm_static=false',
        "mirror/http/hostname=$mirrorHost",
        "mirror/http/directory=$mirrorDir",
        "mirror/http/proxy=$proxyVal",
        "apt-setup/security_host=$secHost",
        "apt-setup/security_path=$secDir",
        'apt-setup/services-select=security,updates',
        'apt-setup/use_mirror=true',
        'anna/retry=true',
        'earlyprintk=serial,ttyS0,115200n8',
        'ignore_loglevel'
    )
    if ($Gui) {
        $appendParts += @('console=ttyS0,115200n8', 'console=tty0')
    } else {
        $appendParts += @('console=ttyS0,115200n8')
    }
    $appendParts += '---'
    $appendLine = $appendParts -join ' '

    Info "preseed URL  : http://10.0.2.2:$HttpPort/preseed.cfg  (仅安装期间提供)"
    Info "append args  : $appendLine"
    $bootArgs = @('-kernel', $kernelPath, '-initrd', $initrdPath, '-append', $appendLine)
} else {
    Info "manual mode  : 会打开 QEMU 图形窗口，从 ISO 启动，按 Debian 安装器提示操作"
    $bootArgs = @('-cdrom', $Iso, '-boot', 'd')
}

# 显示与串口：-Gui 时开图形窗口，否则无头；两种模式都把串口写到文件。
if ($Mode -eq 'manual' -or $Gui) {
    $display = @('-display', 'sdl', '-serial', "file:$installLog")
} else {
    $display = @('-display', 'none', '-serial', "file:$installLog")
}

$qemuArgs = @(
    '-machine', 'q35', '-accel', $accel, '-cpu', $CpuModel,
    '-smp', "$Cpus", '-m', "$MemoryMb",
    '-drive', "file=$disk,if=virtio,format=qcow2,discard=unmap",
    '-nic', "user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:8090-:8090",
    '-device', 'virtio-rng-pci',
    '-rtc', 'base=utc',
    # -no-reboot: the installer is started with -kernel, so a guest reboot would
    # re-enter the installer instead of the freshly installed system.
    '-no-reboot'
) + $bootArgs + $display

if ($Mode -eq 'preseed') {
    # keep the ISO attached so d-i can also use it as a local package source
    $qemuArgs = $qemuArgs + @('-cdrom', $Iso)
}

# --------------------------------------------------------------------------
# 4. dry run: show everything, change nothing
# --------------------------------------------------------------------------
if ($DryRun) {
    Info "(dry-run) 将要执行的命令:"
    Write-Host ""
    Write-Host "  $qemu $($qemuArgs -join ' ')"
    Write-Host ""
    if (Test-Path -LiteralPath $disk) {
        Info "(dry-run) 磁盘已存在，会直接复用: $disk"
    } else {
        Info "(dry-run) 将创建磁盘: $qemuImg create -f qcow2 `"$disk`" ${DiskGb}G"
    }
    if ($Mode -eq 'preseed') {
        Info "(dry-run) 将临时启动: $pythonReal -m http.server $HttpPort --bind 0.0.0.0 --directory `"$serveDir`""
        Info "(dry-run) 安装串口日志: $installLog"
        Info "(dry-run) 装完 late_command 会把仓库解到 VM 的 /opt/agentbox/app"
        Info "(dry-run) 安装总超时: $InstallTimeoutMinutes 分钟"
        if ($Gui) { Info "(dry-run) -Gui: d-i 界面会显示在 QEMU 窗口（串口只有内核日志）" }
    }
    Info "(dry-run) 没有改动任何东西；去掉 -DryRun 即真正开始安装"
    exit 0
}

# --------------------------------------------------------------------------
# 5. create the disk, serve the preseed, boot the installer
# --------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $VmDir | Out-Null
if (-not (Test-Path -LiteralPath $disk)) {
    Info "creating     : ${DiskGb} GiB qcow2 at $disk"
    & $qemuImg create -f qcow2 $disk "${DiskGb}G" | Out-Null
} else {
    Info "reusing      : $disk"
}

if ($Mode -eq 'preseed') {
    # preseed 服务端口 + hostfwd 的两个宿主机端口一起检查
    Assert-PortFree @($HttpPort, 8090, 8091)
    $script:httpJob = Start-Process -PassThru -WindowStyle Hidden -FilePath $pythonReal `
        -ArgumentList @('-m', 'http.server', "$HttpPort", '--bind', '0.0.0.0', '--directory', $serveDir)
    Start-Sleep -Seconds 2
    Info "preseed host : pid $($script:httpJob.Id) on port $HttpPort"
    # preseed 以注释开头是合法的，这里只校验"取得到"（内容校验交给 d-i）
    if (-not (Wait-SeedFile "http://127.0.0.1:$HttpPort/preseed.cfg" 10)) {
        Fail "preseed 服务校验失败：http://127.0.0.1:$HttpPort/preseed.cfg 取不到`n检查 $serveDir 与端口占用（可能是上次残留的 http.server）"
    }
    Info "preseed 校验 : preseed.cfg 可访问"
    if ($Follow) {
        $script:tailJob = Start-Process -PassThru -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile', '-Command', "Get-Content -LiteralPath '$installLog' -Wait -Tail 30")
        Info "console tail : 已另开窗口实时显示安装日志"
    }
}

Info "booting the installer（源已换成 $mirrorHost，日志：$installLog）"
$startTime = Get-Date
$timedOut = $false
try {
    # Start-Process 不会替含空格的参数加引号，所以这里手工加引号，
    # 保证 "-append <带空格的长字符串>" 作为单个参数传给 QEMU。
    $quotedArgs = $qemuArgs | ForEach-Object {
        if ($_ -match '\s') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }
    $script:qemuProc = Start-Process -PassThru -FilePath $qemu `
        -ArgumentList ($quotedArgs -join ' ') -NoNewWindow

    while (-not $script:qemuProc.HasExited) {
        Start-Sleep -Seconds 10
        $elapsed = ((Get-Date) - $startTime).TotalMinutes
        if ($elapsed -gt $InstallTimeoutMinutes) {
            $timedOut = $true
            Warn "安装超过 $InstallTimeoutMinutes 分钟（已运行 $([math]::Round($elapsed, 1)) 分钟），强制结束 QEMU"
            Stop-Process -Id $script:qemuProc.Id -Force -ErrorAction SilentlyContinue
            try { $script:qemuProc.WaitForExit(15000) | Out-Null } catch { }
            break
        }
    }
} finally {
    $elapsedMin = [math]::Round(((Get-Date) - $startTime).TotalMinutes, 1)
    if ($script:qemuProc -and -not $script:qemuProc.HasExited) {
        Stop-Process -Id $script:qemuProc.Id -Force -ErrorAction SilentlyContinue
    }
    $exitCode = $null
    if ($script:qemuProc) { try { $exitCode = $script:qemuProc.ExitCode } catch { } }
    Stop-AuxProcesses

    Info "QEMU 已退出（用时 $elapsedMin 分钟；退出码 $(if ($null -ne $exitCode) { $exitCode } else { '不适用' })）"

    # ---- 安装结果诊断 ----
    if (Test-Path -LiteralPath $installLog) {
        $logSize = (Get-Item -LiteralPath $installLog).Length
        if ($logSize -eq 0) {
            Warn "安装日志为空 —— 安装器内核没有任何串口输出。常见原因："
            Warn "  1) CPU 型号与 WHPX 不兼容：用 -CpuModel qemu64（默认值，实测唯一稳定项）"
            Warn "  2) 内核/initrd 抽错了：删掉 $VmDir\vmlinuz 和 initrd.gz 重新跑"
            Warn "  3) Windows Hypervisor Platform 未启用：换 -ForceTcg 试试（很慢但能跑）"
        } elseif ($timedOut) {
            Warn "超时结束。日志最后 20 行："
            Get-Content -LiteralPath $installLog -Tail 20 | ForEach-Object { Write-Host "    $_" }
        } else {
            Info "日志大小 $([math]::Round($logSize / 1KB, 0)) KiB；最后 10 行："
            Get-Content -LiteralPath $installLog -Tail 10 | ForEach-Object { Write-Host "    $_" }
        }
    } else {
        Warn "安装日志文件不存在: $installLog"
    }
}

if ($timedOut) {
    Fail "安装超时（$InstallTimeoutMinutes 分钟），QEMU 已结束。日志：$installLog"
}

Info "安装结束。若成功，现在启动装好的系统:"
Info "  powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-platform-vm.ps1"
Info "登录 agent / agentbox，然后在 VM 里执行: sudo /opt/agentbox/app/deploy/platform/install-platform.sh"
