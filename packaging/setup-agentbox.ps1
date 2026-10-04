<#
.SYNOPSIS
    agentbox 一键安装：解压 -Lean 包之后，只跑这一个脚本。

.DESCRIPTION
    把 install.ps1（建 .venv / 装依赖 / 生成 .env）和平台 VM、模型权重、启动自检串成一条线。
    每一步都幂等：重复运行只补缺的那部分。

    顺序：
      1. 预检（Windows / PowerShell / Python / 磁盘 / qemu\ / 沙箱镜像 / ssh.exe / wheelhouse）
      2. .venv + 依赖（委托 packaging\install.ps1：有 wheelhouse\ 就完全离线）
      3. .env（install.ps1 生成 + 这里补 AGENT_SANDBOX_CPU=Nehalem，否则 guest 里 numpy 拒绝加载）
      4. 平台 VM：没有可用 platform.qcow2 就 fetch-platform-image.ps1 -> provision-cloud-vm.ps1
         （或 -UseIso 时走 new-platform-vm.ps1）-> run-platform-vm.ps1 -> 轮询
         VM 内 /opt/agentbox/PROVISIONED
      5. 模型权重：VM 内下载 BAAI/bge-m3 + faster-whisper（HF_ENDPOINT=hf-mirror.com、
         HF_HUB_DISABLE_XET=1、不使用 download_root=），再修 owner/权限
      6. 沙箱镜像：随包带了就直接用，没带就给出在 VM 里重建的命令
      7. 启动 + 自检：start-agent.ps1 -NoChat -> :8091/health、:8090/health -> sandbox status
      8. 成功/失败清单 + 已就绪 / 未就绪

    长任务（下 ISO/云镜像、装 VM、下模型）需要显式同意：-Yes（无人值守）
    或交互式回答 y。非交互环境里不给 -Yes 就会跳过并告诉你要加什么参数。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Check
    powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Yes
    powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -SkipVm -SkipModels
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # 只体检，不做任何修改
    [switch]$Check,
    # 跳过预检（预检只报告，不阻断，除非是硬缺失）
    [switch]$SkipChecks,
    # 不碰平台 VM（不下载镜像、不 provision）
    [switch]$SkipVm,
    # 不下模型权重
    [switch]$SkipModels,
    # 无人值守：长任务不再询问
    [switch]$Yes,
    # 平台 VM 走 ISO 安装器路径（需要仓库根有 debian-*.iso）
    [switch]$UseIso,
    # 干跑（等价于 -WhatIf）
    [switch]$DryRun,
    [int]$VmTimeoutMinutes = 45,
    [int]$ModelTimeoutMinutes = 40,
    [int]$StartTimeoutMinutes = 20,
    # 覆盖仓库根目录（默认脚本所在目录的上一级）
    [string]$RepoRoot
)

$ErrorActionPreference = 'Stop'
$dryRun = [bool]($DryRun -or $WhatIfPreference)
$script:Fail = New-Object System.Collections.ArrayList
$script:Soft = New-Object System.Collections.ArrayList
$script:Pass = New-Object System.Collections.ArrayList
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

# ==================================================================== 输出
function Write-Head([string]$t) { Write-Host ''; Write-Host $t -ForegroundColor Cyan }
function Step([string]$t) { Write-Host ''; Write-Host ("== " + $t) -ForegroundColor Cyan }
function Ok([string]$t) { Write-Host ("   ✔ " + $t) -ForegroundColor Green; [void]$script:Pass.Add($t) }
function Warn([string]$t) { Write-Host ("   ! " + $t) -ForegroundColor Yellow; [void]$script:Soft.Add($t) }
function Bad([string]$t) { Write-Host ("   ✘ " + $t) -ForegroundColor Red; [void]$script:Fail.Add($t) }
function Note([string]$t) { Write-Host ("   - " + $t) -ForegroundColor DarkGray }
function Hint([string]$t) { Write-Host ("     修复: " + $t) -ForegroundColor Yellow }

function Die([string]$msg, [string]$hint) {
    Write-Host ''
    Write-Host ("✘ 停止: " + $msg) -ForegroundColor Red
    if ($hint) { Hint $hint }
    exit 1
}

function Format-Size([long]$b) {
    if ($b -lt 0) { return '不存在' }
    if ($b -ge 1GB) { return ('{0:N2} GB' -f ($b / 1GB)) }
    if ($b -ge 1MB) { return ('{0:N1} MB' -f ($b / 1MB)) }
    if ($b -ge 1KB) { return ('{0:N1} KB' -f ($b / 1KB)) }
    return ("$b B")
}

function Get-PathSize([string]$p) {
    if (-not (Test-Path -LiteralPath $p)) { return [long](-1) }
    $i = Get-Item -LiteralPath $p -Force
    if (-not $i.PSIsContainer) { return [long]$i.Length }
    $s = (Get-ChildItem -LiteralPath $p -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
    if ($null -eq $s) { return [long]0 }
    return [long]$s
}

function Ask-Yes([string]$what) {
    if ($Yes) { return $true }
    if ($dryRun) { Note ("[DRY-RUN] 会做: " + $what); return $false }
    if ($Host.Name -ne 'ConsoleHost' -or -not [Environment]::UserInteractive) {
        Warn ("需要确认才能做: " + $what + " —— 当前不是交互式会话，已跳过；确认后加 -Yes 重跑")
        return $false
    }
    $a = Read-Host ("   " + $what + "  继续吗？(y/N)")
    return ($a -match '^(y|yes|是)$')
}

# ==================================================================== 定位
if (-not $RepoRoot) {
    $parent = Split-Path -Parent $PSScriptRoot
    if (Test-Path -LiteralPath (Join-Path $parent 'pyproject.toml')) { $RepoRoot = $parent }
    elseif (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'pyproject.toml')) { $RepoRoot = $PSScriptRoot }
    else { $RepoRoot = $parent }
}
$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
$venvDir = Join-Path $RepoRoot '.venv'
$venvPy = Join-Path $venvDir 'Scripts\python.exe'
$envFile = Join-Path $RepoRoot '.env'
$envExample = Join-Path $RepoRoot '.env.example'
# wheelhouse 可能在 <仓库>\wheelhouse\（包里的位置）或 <仓库>\packaging\wheelhouse\（开发机）
$wheelhouse = Join-Path $RepoRoot 'wheelhouse'
if (-not (Test-Path -LiteralPath $wheelhouse)) {
    $whAlt = Join-Path $RepoRoot 'packaging\wheelhouse'
    if (Test-Path -LiteralPath $whAlt) { $wheelhouse = $whAlt }
}
$whRel = 'wheelhouse'
if ($wheelhouse.StartsWith($RepoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    $whRel = $wheelhouse.Substring($RepoRoot.Length).TrimStart('\')
}
$qemuDir = Join-Path $RepoRoot 'qemu'
$qemuExe = Join-Path $qemuDir 'qemu-system-x86_64.exe'
$sandboxDir = Join-Path $RepoRoot 'var\sandbox'
$platDir = Join-Path $RepoRoot 'var\platform'
$platDisk = Join-Path $platDir 'platform.qcow2'
$sshKey = Join-Path $RepoRoot 'var\vm_key'
$sshExe = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$SshPort = 2222
$installer = Join-Path $RepoRoot 'packaging\install.ps1'
$logDir = Join-Path $RepoRoot 'var\logs'
$sandboxNeed = @('rootfs.img', 'vmlinuz', 'initrd.img', 'workspace-blank.qcow2')

function Resolve-Python {
    $cands = New-Object System.Collections.ArrayList
    if (Test-Path -LiteralPath $venvPy) { [void]$cands.Add([pscustomobject]@{ Exe = $venvPy; Pre = @() }) }
    foreach ($pre in @(@('-3.13'), @('-3'))) {
        $py = Get-Command 'py.exe' -ErrorAction SilentlyContinue
        if ($py) { [void]$cands.Add([pscustomobject]@{ Exe = $py.Source; Pre = $pre }) }
    }
    $p = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($p) { [void]$cands.Add([pscustomobject]@{ Exe = $p.Source; Pre = @() }) }
    foreach ($c in $cands) {
        if (-not (Test-Path -LiteralPath $c.Exe)) { continue }
        try {
            $v = & $c.Exe @($c.Pre) -c "import sys;print(sys.version.split()[0])" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$v" -match '^\d+\.\d+\.\d+') {
                return [pscustomobject]@{ Exe = $c.Exe; Pre = $c.Pre; Ver = "$v".Trim() }
            }
        } catch { }
    }
    return $null
}

# 依赖自检：返回缺失的模块名（空串 = 齐全）。
# 这段 python -c 的代码只准用单引号：PS 5.1 把参数交给原生 exe 时会把双引号吃掉。
$script:DepCode = @'
import importlib.util as u
mods = ['fastapi', 'uvicorn', 'httpx', 'pydantic', 'pydantic_settings',
        'sqlalchemy', 'asyncpg', 'pgvector', 'jsonschema', 'rich',
        'prompt_toolkit', 'agent']
print(','.join(m for m in mods if u.find_spec(m) is None))
'@
function Get-MissingDeps([string]$py) {
    return "$(& $py -c $script:DepCode 2>$null)".Trim()
}

# --- 平台 VM 用的 ssh 辅助（和 deploy\windows\start-agent.ps1 同一套写法）---
$sshArgs = @('-i', $sshKey, '-p', "$SshPort", '-o', 'StrictHostKeyChecking=no',
             '-o', 'UserKnownHostsFile=NUL', '-o', 'LogLevel=ERROR', '-o', 'ConnectTimeout=8',
             'agent@127.0.0.1')

function RunRemote([string]$script, [int]$timeoutSec = 120) {
    if (-not (Test-Path -LiteralPath $sshExe)) { return '(没有 ssh.exe)' }
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($script -replace "`r`n", "`n")))
    $job = Start-Job -ScriptBlock {
        param($exe, $args_, $payload)
        $ErrorActionPreference = 'Continue'
        & $exe @args_ "echo $payload | base64 -d | bash -s" 2>&1
    } -ArgumentList $sshExe, $sshArgs, $b64
    if (Wait-Job $job -Timeout $timeoutSec) { $out = Receive-Job $job }
    else { Stop-Job $job -ErrorAction SilentlyContinue; $out = "(ssh 超时 ${timeoutSec}s)" }
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    return ($out | Out-String)
}

function Test-VmSsh {
    return ((RunRemote 'echo __AGENTBOX_SSH_OK__' 20) -match '__AGENTBOX_SSH_OK__')
}

function Test-VmProvisioned {
    return ((RunRemote 'test -f /opt/agentbox/PROVISIONED && echo __PROV__' 25) -match '__PROV__')
}

function Get-VmLogTail([int]$lines = 40) {
    $out = RunRemote ("sudo tail -n $lines /var/log/agentbox-install.log 2>/dev/null || tail -n $lines /var/log/agentbox-install.log 2>/dev/null || echo '(取不到 /var/log/agentbox-install.log)'") 60
    return $out.Trim()
}

function Get-PlatformQemuProcess {
    # 只看属于"这份仓库/这个解压副本"的那台平台 VM：命令行走 run-platform-vm.ps1 传的
    # -drive file=<RepoRoot>\var\platform\platform.qcow2，所以按 $platDisk 精确匹配。
    return @(Get-CimInstance Win32_Process -Filter "Name='qemu-system-x86_64.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -and $_.CommandLine -like "*$platDisk*" })
}

function Wait-VmSsh([int]$timeoutSec = 240) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-VmSsh) { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

function Start-PlatformVm {
    $runner = Join-Path $RepoRoot 'deploy\windows\run-platform-vm.ps1'
    if (-not (Test-Path -LiteralPath $runner)) { return $false }
    Start-Process -WindowStyle Hidden -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $runner, '-Headless', '-SshPort', "$SshPort"
    ) | Out-Null
    return $true
}

# 把 deploy 里的长脚本放到后台跑，同时轮询"成功探针"，成功就把它的 QEMU 收掉
function Invoke-ProvisionScript([string]$scriptPath, [string[]]$extraArgs, [int]$timeoutMin, [scriptblock]$probe, [string]$desc) {
    $name = [System.IO.Path]::GetFileNameWithoutExtension($scriptPath)
    $outLog = Join-Path $logDir ("setup-$name-$stamp.log")
    $errLog = "$outLog.err"
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $scriptPath) + $extraArgs
    Note ("启动: powershell -File $scriptPath " + ($extraArgs -join ' '))
    Note ("日志: $outLog")
    $proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $deadline = (Get-Date).AddMinutes($timeoutMin)
    $lastShown = ''
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 12
        $tail = ''
        if (Test-Path -LiteralPath $outLog) {
            $last = @(Get-Content -LiteralPath $outLog -Tail 1 -ErrorAction SilentlyContinue)
            if ($last.Count -gt 0) { $tail = "$($last[0])".Trim() }
        }
        if ($tail -and $tail -ne $lastShown) { Note $tail; $lastShown = $tail }
        if (& $probe) {
            Note ("$desc 达成，收掉前台的 QEMU")
            foreach ($q in Get-PlatformQemuProcess) { & taskkill.exe /PID $q.ProcessId /T /F 2>$null | Out-Null }
            $wait = 0
            while (-not $proc.HasExited -and $wait -lt 30) { Start-Sleep -Seconds 2; $wait += 2 }
            if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
            return $true
        }
        if ($proc.HasExited) { Note ("$name 已退出（exit " + $proc.ExitCode + "）"); break }
    }
    foreach ($q in Get-PlatformQemuProcess) { & taskkill.exe /PID $q.ProcessId /T /F 2>$null | Out-Null }
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    Warn ("$desc 没有在 $timeoutMin 分钟内达成；日志尾部（$outLog）：")
    if (Test-Path -LiteralPath $outLog) {
        Get-Content -LiteralPath $outLog -Tail 15 -ErrorAction SilentlyContinue | ForEach-Object { Note $_ }
    }
    return $false
}

function HttpOk([string]$url, [int]$timeoutSec = 5) {
    try {
        $r = Invoke-WebRequest -Uri $url -TimeoutSec $timeoutSec -UseBasicParsing
        return [int]$r.StatusCode
    } catch {
        return 0
    }
}

function Get-EnvValue([string]$path, [string]$key) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $text = [System.IO.File]::ReadAllText($path)
    $m = [regex]::Match($text, ('(?m)^[ \t]*' + [regex]::Escape($key) + '[ \t]*=[ \t]*([^\r\n]*)'))
    if (-not $m.Success) { return $null }
    return $m.Groups[1].Value.Trim()
}

# 幂等地确保 .env 里有 key=value（空值会被填上；已有不同值就只提醒，不覆盖）
function Ensure-EnvKey([string]$path, [string]$key, [string]$value) {
    if (-not (Test-Path -LiteralPath $path)) { return 'missing-file' }
    $text = [System.IO.File]::ReadAllText($path)
    $pattern = ('(?m)^[ \t]*' + [regex]::Escape($key) + '[ \t]*=[ \t]*([^\r\n]*)')
    $m = [regex]::Match($text, $pattern)
    if ($m.Success) {
        $cur = $m.Groups[1].Value.Trim()
        if ($cur -eq $value) { return 'already' }
        if ($cur -ne '') { return "kept:$cur" }
        if ($dryRun) { return 'would-fill' }
        $text = [regex]::Replace($text, $pattern, ("$key=$value"))
    } else {
        if ($dryRun) { return 'would-append' }
        $text = $text.TrimEnd() + "`r`n$key=$value`r`n"
    }
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
    return 'set'
}

# ==================================================================== 1. 预检
function Invoke-Preflight {
    Step '1/8 预检'

    $cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue
    $osName = '未知'
    if ($cv) { $osName = ("{0} {1} (build {2})" -f $cv.ProductName, $cv.DisplayVersion, $cv.CurrentBuild) }
    $os = [System.Environment]::OSVersion.Version
    if ($os.Major -ge 10 -and [Environment]::Is64BitOperatingSystem) { Ok "Windows: $osName" }
    else { Bad "Windows: $osName（需要 Windows 10/11 x64）"; Hint 'agentbox 依赖 WHPX（Windows 10 1803+ / Windows 11）。换机器。' }

    $psv = $PSVersionTable.PSVersion
    if ($psv.Major -gt 5 -or ($psv.Major -eq 5 -and $psv.Minor -ge 1)) { Ok "PowerShell: $psv" }
    else { Bad "PowerShell: $psv（需要 5.1+）"; Hint '用 Windows 自带的 powershell.exe（5.1）运行本脚本。' }

    $py = Resolve-Python
    if ($py) {
        $minor = [int](($py.Ver -split '\.')[1])
        if ($minor -ge 11) { Ok "Python: $($py.Ver)  ($($py.Exe))" }
        else { Bad "Python: $($py.Ver) 太老"; Hint '装 Python 3.13+ x64（勾 py launcher），或用 -Python 指定。' }
        if ($minor -ne 14) {
            Warn "Python 是 3.$minor；这份包的 wheelhouse 是按 Python 3.14 下的（换解释器就装不上，见 packaging\README.md）"
        }
    } else {
        Bad 'Python: 找不到可用解释器'
        Hint '装 Python 3.13+ x64（https://www.python.org/downloads/windows/ ，勾 py launcher）。注意 Microsoft Store 的 python.exe 别名不能用（执行返回 9009）。'
    }

    $drv = (Get-Item -LiteralPath $RepoRoot).PSDrive.Name
    $free = (Get-PSDrive -Name $drv -ErrorAction SilentlyContinue).Free
    if ($null -ne $free) {
        if ($free -ge 3GB) { Ok ("磁盘 {0}: 剩余 {1}" -f $drv, (Format-Size $free)) }
        elseif ($free -ge 1GB) { Warn ("磁盘 {0}: 只剩 {1}（建 .venv 够，放平台磁盘/模型不够）" -f $drv, (Format-Size $free)) }
        else { Bad ("磁盘 {0}: 只剩 {1}" -f $drv, (Format-Size $free)); Hint '至少空出 3 GB；要重建平台 VM 再准备 30 GB。' }
    }

    if (Test-Path -LiteralPath $qemuExe) {
        $img = Test-Path -LiteralPath (Join-Path $qemuDir 'qemu-img.exe')
        if ($img) { Ok ("QEMU: $qemuExe（{0}）" -f (Format-Size (Get-PathSize $qemuDir))) }
        else { Bad 'QEMU: 有 qemu-system-x86_64.exe 但缺 qemu-img.exe'; Hint 'QEMU for Windows 包里两个都要；从 https://qemu.weilnetz.de/w64/ 重新装一份。' }
    } else {
        Bad "QEMU: 没有 $qemuExe"
        Hint 'QEMU 不在 -Lean 之外的包里。到 https://qemu.weilnetz.de/w64/ 装一份，或把 qemu\ 整个目录拷到仓库根，或设 AGENT_QEMU_DIR。'
    }

    $sbMissing = @($sandboxNeed | Where-Object { -not (Test-Path -LiteralPath (Join-Path $sandboxDir $_)) })
    if ($sbMissing.Count -eq 0) { Ok ("沙箱镜像: 4 个文件齐全（{0}）" -f (Format-Size (Get-PathSize $sandboxDir))) }
    else { Bad ("沙箱镜像: var\sandbox\ 缺 " + ($sbMissing -join ', ')); Hint '在平台 VM 里重建：见第 6 步。' }

    if (Test-Path -LiteralPath $sshExe) { Ok "ssh.exe: $sshExe" }
    else { Bad 'ssh.exe: 找不到'; Hint '装 Windows 可选功能 "OpenSSH Client"（设置 -> 系统 -> 可选功能）。' }

    $wh = @(Get-ChildItem -LiteralPath $wheelhouse -Filter *.whl -File -ErrorAction SilentlyContinue)
    if ($wh.Count -gt 0) {
        $b = ($wh | Measure-Object -Property Length -Sum).Sum
        if ($wh[0].Name -match 'cp3(\d+)' -and [int]$Matches[1] -ne 14) {
            Warn ("wheelhouse: {0} 个 wheel（{1}），但标签是 cp3{2}，和本机 Python 对不上" -f $wh.Count, (Format-Size $b), $Matches[1])
        } else { Ok ("wheelhouse: {0} 个 wheel（{1}）—— 可以离线装依赖" -f $wh.Count, (Format-Size $b)) }
    } else {
        Warn 'wheelhouse: 没有（装依赖需要联网）'
    }

    if (Test-Path -LiteralPath $platDisk) { Ok ("平台磁盘: 已存在（{0}）" -f (Format-Size (Get-PathSize $platDisk))) }
    else { Warn '平台磁盘: 没有（第 4 步会重建）' }

    $iso = @(Get-ChildItem -LiteralPath $RepoRoot -Filter '*.iso' -File -ErrorAction SilentlyContinue)
    if ($iso.Count -gt 0) { Ok ("ISO: " + (($iso | ForEach-Object { $_.Name }) -join ', ')) }
    else { Note 'ISO: 没有（不需要；-UseIso 那条路才要）' }
}

# ==================================================================== 2. venv + 依赖
function Invoke-VenvStep {
    Step '2/8 .venv + 依赖（委托 packaging\install.ps1）'
    if (-not (Test-Path -LiteralPath $installer)) { Die "缺少 $installer" '这个包不完整：重新解压，或重新打包。' }
    if ($dryRun) { Note '[DRY-RUN] 会调用 packaging\install.ps1 建 .venv 并装依赖'; return }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
    $rc = $LASTEXITCODE
    Note "install.ps1 退出码: $rc（1 通常只是『API key 还没填』这类未就绪，不代表装坏了）"
    if (-not (Test-Path -LiteralPath $venvPy)) { Die '没有生成 .venv\Scripts\python.exe' '看上面 install.ps1 的报错；也可以手动：py -3.13 -m venv .venv' }
    # 单引号：PS 5.1 传给原生 exe 时会吃掉双引号
    $missing = Get-MissingDeps $venvPy
    if ($missing) { Bad ("依赖还是缺: $missing"); Hint (".\.venv\Scripts\python -m pip install --no-index --find-links " + $whRel + " -e .") }
    else { Ok '依赖检查通过（11 个包 + agent 都在）' }
}

# ==================================================================== 3. .env
function Invoke-EnvStep {
    Step '3/8 .env'
    if (-not (Test-Path -LiteralPath $envFile)) {
        Bad '没有 .env'
        Hint 'install.ps1 会生成；也可以从 .env.example 复制。'
        return
    }
    $secret = Get-EnvValue $envFile 'AGENT_CONTROL_SECRET'
    if ($secret) { Ok 'AGENT_CONTROL_SECRET 已设置（值不打印）' } else { Bad 'AGENT_CONTROL_SECRET 为空'; Hint 'python -c "import secrets;print(secrets.token_hex(32))" 生成一个填进去（平台 VM 里必须一致）。' }
    $key = Get-EnvValue $envFile 'AGENT_LLM_API_KEY'
    if ($key -and $key -notmatch '(?i)replace|your|xxx|changeme') { Ok 'AGENT_LLM_API_KEY 已填（值不打印）' }
    else { Warn 'AGENT_LLM_API_KEY 还是占位符 —— 最后一步要自己填（唯一必须人工做的一步）' }

    # 这条必须写：WHPX + 老 CPU 型号会让 guest 里现代 wheel（numpy 等 x86-64-v2）拒绝加载
    $r = Ensure-EnvKey $envFile 'AGENT_SANDBOX_CPU' 'Nehalem'
    switch -Regex ($r) {
        '^already$' { Ok 'AGENT_SANDBOX_CPU=Nehalem 已在 .env 里' }
        '^set$' { Ok 'AGENT_SANDBOX_CPU=Nehalem 已写入 .env（guest 里 numpy 等 wheel 需要 SSE4.2）' }
        '^would-' { Note "[DRY-RUN] 会写 AGENT_SANDBOX_CPU=Nehalem（$r）" }
        '^kept:' { Warn ("AGENT_SANDBOX_CPU 已被设成别的值（" + $r.Substring(5) + "）—— 如果不是 Nehalem/host，guest 里 numpy 可能拒绝加载") }
        default { Warn '没能写 AGENT_SANDBOX_CPU（.env 不可写？）' }
    }
}

# ==================================================================== 4. 平台 VM
function Invoke-PlatformVmStep {
    Step '4/8 平台 VM'
    if ($SkipVm) { Note '-SkipVm：跳过'; return }
    if (Test-Path -LiteralPath $platDisk) {
        Ok ("platform.qcow2 已存在（{0}），不重建" -f (Format-Size (Get-PathSize $platDisk)))
        if (-not (Test-Path -LiteralPath $sshKey) -or -not (Test-Path -LiteralPath "$sshKey.pub")) {
            Bad 'var\vm_key 不存在：登录不了这台 VM'
            Hint '从制作这份磁盘的机器把 var\vm_key + var\vm_key.pub 拷过来（私钥不进包）；或者删掉 platform.qcow2 让本脚本重建（会换新密钥）。'
        } else { Ok 'var\vm_key 已存在（值不打印）' }
        return
    }
    if (-not (Test-Path -LiteralPath $qemuExe)) { Bad '没有 QEMU，没法建平台 VM'; Hint '先把 qemu\ 备好（见第 1 步），再用 -SkipVm=$false 重跑。'; return }

    $what = '重建平台 VM（下载 Debian 云镜像/fetch + provision，10-40 分钟，需要联网）'
    if (-not (Ask-Yes $what)) { Warn '已跳过平台 VM 重建'; return }
    if ($dryRun) { return }

    $fetch = Join-Path $RepoRoot 'deploy\windows\fetch-platform-image.ps1'
    if (-not (Test-Path -LiteralPath $fetch)) { Die "缺少 $fetch" '包不完整，重新解压。' }

    if ($UseIso) {
        $iso = @(Get-ChildItem -LiteralPath $RepoRoot -Filter '*.iso' -File -ErrorAction SilentlyContinue)
        if ($iso.Count -eq 0) { Die '-UseIso 需要仓库根有 debian-*.iso' '下载 debian-13.x-amd64-netinst.iso 放到仓库根，或去掉 -UseIso（走云镜像，快得多）。' }
        Note 'ISO 路径：装完还要在 VM 里跑 install-platform.sh（本脚本会代跑）'
        $newVm = Join-Path $RepoRoot 'deploy\windows\new-platform-vm.ps1'
        $okIso = Invoke-ProvisionScript $newVm @() $VmTimeoutMinutes {
            # ISO 路径装完把 QEMU 关掉，provisioned 的判定放到后面
            -not (Get-PlatformQemuProcess) -and (Test-Path -LiteralPath $platDisk)
        } 'ISO 安装完成'
        if (-not $okIso) { Bad 'ISO 安装没完成'; return }
    } else {
        Note '先下 Debian 13 云镜像（约 330 MB，断点续传）'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $fetch
        if ($LASTEXITCODE -ne 0) { Bad 'fetch-platform-image.ps1 失败（多半是没有网/代理不通）'; Hint '检查网络后重跑；或手动下载 debian-13-genericcloud-amd64.qcow2 放进 var\images\。'; return }
        $provision = Join-Path $RepoRoot 'deploy\windows\provision-cloud-vm.ps1'
        $ok = Invoke-ProvisionScript $provision @('-Display', 'none') $VmTimeoutMinutes { Test-VmProvisioned } 'VM 内出现 /opt/agentbox/PROVISIONED'
        if (-not $ok) {
            Bad '平台 VM 没有配好'
            $tail = Get-VmLogTail 40
            if ($tail) { Note 'install-platform.log 尾部（最后 40 行）：'; $tail -split "`n" | ForEach-Object { Note $_ } }
            Hint '串口日志在 var\platform\console.log；apt/pip 都需要网。修好后重跑本脚本（幂等）。'
            return
        }
        Ok 'VM 内出现 /opt/agentbox/PROVISIONED（平台配好了）'
    }

    if (-not (Get-PlatformQemuProcess)) {
        if (Start-PlatformVm) { Note '启动 run-platform-vm.ps1 -Headless，等 SSH' } else { Bad '缺少 run-platform-vm.ps1'; return }
        if (Wait-VmSsh 240) { Ok 'SSH 已通' } else { Bad '等不到 SSH'; Hint '看 var\platform\console.log；或手动跑 deploy\windows\run-platform-vm.ps1。'; return }
    } else { Ok '平台 VM 已在运行（QEMU 进程在）' }

    if ($UseIso) {
        $r = RunRemote 'sudo /opt/agentbox/app/deploy/platform/install-platform.sh > /tmp/ipi.log 2>&1; echo IPI_EXIT=$?; sudo touch /opt/agentbox/PROVISIONED; sudo systemctl restart agentbox-ai' 1800
        if ($r -match 'IPI_EXIT=0') { Ok 'ISO 路径：install-platform.sh 跑完并打上 PROVISIONED' }
        else { Bad 'ISO 路径：install-platform.sh 失败'; $r -split "`n" | Select-Object -Last 20 | ForEach-Object { Note $_ }; Hint 'VM 里看 /tmp/ipi.log。' }
    }
}

# ==================================================================== 5. 模型
function Invoke-ModelsStep {
    Step '5/8 模型权重（bge-m3 + faster-whisper）'
    if ($SkipModels) { Note '-SkipModels：跳过（检索会退化成关键词匹配、语音不可用）'; return }
    if (-not (Test-Path -LiteralPath $platDisk)) { Warn '没有平台 VM，跳过模型下载'; return }
    if ($dryRun) { Note '[DRY-RUN] 会在 VM 里下载 BAAI/bge-m3 与 faster-whisper 模型'; return }
    if (-not (Get-PlatformQemuProcess)) {
        if (Start-PlatformVm) { Note '先把平台 VM 拉起来' }
        if (-not (Wait-VmSsh 240)) { Warn '连不上平台 VM（SSH），跳过模型下载'; Hint '先让 deploy\windows\start-agent.ps1 把 VM 跑起来，再重跑本脚本。'; return }
    }
    if (-not (Test-VmSsh)) { Warn 'SSH 不通，跳过模型下载'; Hint '如果这台 VM 的磁盘来自别的机器，它的 authorized_keys 是那把旧公钥 —— 换私钥或重建 VM。'; return }

    $asrModel = (RunRemote 'sudo grep -h "^AGENT_ASR_MODEL=" /opt/agentbox/app/.env 2>/dev/null | tail -1 | cut -d= -f2' 30).Trim()
    if (-not $asrModel) { $asrModel = 'base' }
    $extraNote = ''
    if ($asrModel -ne 'small') { $extraNote = "（顺带把 .env 里配的 $asrModel 也下了，避免运行时要联网）" }
    Note "VM 里 AGENT_ASR_MODEL=$asrModel；按需求下 small$extraNote"

    $what = "在平台 VM 里下载模型权重（bge-m3 ~2.3 GB + whisper，走 hf-mirror）"
    if (-not (Ask-Yes $what)) { Warn '已跳过模型下载'; return }

    $modelEnv = 'export HF_HOME=/opt/agentbox/models HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1'
    $bge = "cd /opt/agentbox/app && $modelEnv && .venv/bin/python -c `"from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')`" 2>&1 | tail -5"
    Note ("下载 bge-m3（走 hf-mirror；上限 $ModelTimeoutMinutes 分钟）")
    $r1 = RunRemote $bge ($ModelTimeoutMinutes * 60)
    $last1 = (@($r1 -split "`r?`n" | Where-Object { "$_".Trim() } | Select-Object -Last 1) -join '')
    if ($r1 -match '(?i)error|traceback|timed out|超时') { Warn ("bge-m3 下载可能失败：" + $last1) }
    else { Ok 'bge-m3 已就绪（或已缓存）' }

    $models = @('small')
    if ($asrModel -ne 'small') { $models += $asrModel }
    foreach ($m in $models) {
        Note "下载 faster-whisper 模型 $m"
        $wh = "cd /opt/agentbox/app && $modelEnv && .venv/bin/python -c `"from faster_whisper import WhisperModel; WhisperModel('$m', device='cpu', compute_type='int8')`" 2>&1 | tail -5"
        $r2 = RunRemote $wh ($ModelTimeoutMinutes * 60)
        $last2 = (@($r2 -split "`r?`n" | Where-Object { "$_".Trim() } | Select-Object -Last 1) -join '')
        if ($r2 -match '(?i)error|traceback|timed out|超时') { Warn ("whisper $m 下载可能失败：" + $last2) }
        else { Ok "faster-whisper $m 已就绪" }
    }

    $fix = RunRemote 'sudo chown -R agent:agent /opt/agentbox/models && sudo chmod -R a+rX /opt/agentbox/models && sudo du -sh /opt/agentbox/models | cut -f1' 120
    Note ("HF_HOME 现状: " + $fix.Trim())
    $restart = RunRemote 'sudo systemctl restart agentbox-ai; sleep 2; systemctl is-active agentbox-ai' 120
    if ($restart -match 'active') { Ok 'agentbox-ai 已重启（带上新模型）' } else { Warn "agentbox-ai 状态: $($restart.Trim())" }
}

# ==================================================================== 6. 沙箱镜像
function Invoke-SandboxImageStep {
    Step '6/8 沙箱镜像'
    $missing = @($sandboxNeed | Where-Object { -not (Test-Path -LiteralPath (Join-Path $sandboxDir $_)) })
    if ($missing.Count -eq 0) {
        Ok ("随包带来的沙箱镜像可用（{0}，4 个文件）" -f (Format-Size (Get-PathSize $sandboxDir)))
        return
    }
    Warn ("var\sandbox\ 缺: " + ($missing -join ', '))
    Hint '必须在 Linux 里构建（debootstrap + ext4）。命令：'
    Note 'powershell -File .\deploy\windows\push-repo-to-vm.ps1'
    Note 'ssh -i var\vm_key -p 2222 agent@127.0.0.1 "sudo bash /opt/agentbox/app/deploy/sandbox/build-sandbox-image.sh"'
    Note 'powershell -File .\deploy\windows\fetch-sandbox-image.ps1     # 把 4 个文件拉回 Windows'
    Note 'start-agent.ps1 也会尝试从 VM 的 8099 端口自动拉一次。'
}

# ==================================================================== 7. 启动 + 自检
function Invoke-StartStep {
    Step '7/8 启动 + 自检（start-agent.ps1 -NoChat）'
    $starter = Join-Path $RepoRoot 'deploy\windows\start-agent.ps1'
    if (-not (Test-Path -LiteralPath $starter)) { Bad '缺少 deploy\windows\start-agent.ps1'; return }
    if ($dryRun) { Note '[DRY-RUN] 会跑 start-agent.ps1 -NoChat 并检查 :8090/:8091'; return }

    $outLog = Join-Path $logDir ("setup-start-$stamp.log")
    $proc = Start-Process -FilePath 'powershell.exe' -PassThru -WindowStyle Hidden `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $starter, '-NoChat') `
        -RedirectStandardOutput $outLog -RedirectStandardError "$outLog.err"
    $deadline = (Get-Date).AddMinutes($StartTimeoutMinutes)
    while ((Get-Date) -lt $deadline -and -not $proc.HasExited) { Start-Sleep -Seconds 8 }
    if (-not $proc.HasExited) { Warn "start-agent.ps1 超过 $StartTimeoutMinutes 分钟还没结束（继续自检）" }
    else { Note ("start-agent.ps1 退出码: " + $proc.ExitCode) }
    if (Test-Path -LiteralPath $outLog) {
        Get-Content -LiteralPath $outLog -Tail 8 -ErrorAction SilentlyContinue | ForEach-Object { Note $_ }
    }

    $c = HttpOk 'http://127.0.0.1:8091/health'
    if ($c -eq 200) { Ok '控制平面 :8091/health = 200' } else { Bad "控制平面 :8091/health = $c"; Hint '看 var\control.log / var\control.err.log；或手动 .\.venv\Scripts\python -m agent.cli serve control' }
    $a = HttpOk 'http://127.0.0.1:8090/health' 10
    if ($a -eq 200) { Ok 'AI 服务 :8090/health = 200' } else { Bad "AI 服务 :8090/health = $a"; Hint '平台 VM 里的 systemd 单元：ssh 进去 systemctl status agentbox-ai；日志 /var/log/agentbox-install.log' }

    if (Test-Path -LiteralPath $venvPy) {
        $st = "$(& $venvPy -m agent.cli sandbox status 2>&1)"
        if ($st -match 'warm=(\d+)' -and [int]$Matches[1] -ge 1) { Ok ("沙箱池有热 VM（" + (($st -split "`n" | Where-Object { $_ -match 'sandbox pool' }) -join '') + "）") }
        elseif ($st -match 'warm=0') { Warn '沙箱池目前 warm=0（开机预热还没完成，或镜像缺失）' }
        else { Warn ("sandbox status 没读到池状态: " + (($st -split "`n" | Select-Object -Last 1))) }
    }
}

# ==================================================================== 8. 结论
function Show-Verdict {
    Step '8/8 结论'
    if ($script:Fail.Count -eq 0) {
        Write-Host ''
        Write-Host '================================================================' -ForegroundColor Green
        Write-Host (" 已就绪：✔ {0} 项通过 / ! {1} 项提醒 / ✘ 0 项" -f $script:Pass.Count, $script:Soft.Count) -ForegroundColor Green
        Write-Host '================================================================' -ForegroundColor Green
        Write-Host ' 下一步：' -ForegroundColor White
        Write-Host '   1) 把 DeepSeek API key 填进 .env 的 AGENT_LLM_API_KEY=' -ForegroundColor White
        Write-Host '   2) 开始对话：powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1' -ForegroundColor White
        Write-Host '   3) 复检：powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Check' -ForegroundColor White
        Write-Host ''
        return 0
    }
    Write-Host ''
    Write-Host '================================================================' -ForegroundColor Red
    Write-Host (" 未就绪：✘ {0} 项要修" -f $script:Fail.Count) -ForegroundColor Red
    Write-Host '================================================================' -ForegroundColor Red
    $i = 0
    foreach ($f in $script:Fail) { $i++; Write-Host ("  {0}. {1}" -f $i, $f) -ForegroundColor Red }
    Write-Host ''
    Write-Host (" 通过 {0} 项 / 提醒 {1} 项" -f $script:Pass.Count, $script:Soft.Count) -ForegroundColor Yellow
    if ($script:Soft.Count -gt 0) {
        Write-Host ' 提醒（不挡路）：' -ForegroundColor Yellow
        $script:Soft | Select-Object -Unique | ForEach-Object { Write-Host ("   ! " + $_) -ForegroundColor Yellow }
    }
    Write-Host ''
    return 1
}

# ==================================================================== 体检
function Invoke-HealthCheck {
    Step '体检（-Check：不改任何东西）'
    if (Test-Path -LiteralPath $venvPy) {
        $ver = "$(& $venvPy -c 'import sys;print(sys.version.split()[0])' 2>$null)".Trim()
        $missing = Get-MissingDeps $venvPy
        if ($missing) { Bad "venv 存在（Python $ver）但缺依赖: $missing"; Hint (".\.venv\Scripts\python -m pip install --no-index --find-links " + $whRel + " -e .") }
        else { Ok "venv 就绪（Python $ver，依赖齐全）" }
    } else { Bad '没有 .venv'; Hint '跑一次（不加 -Check）：powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1' }

    if (Test-Path -LiteralPath $envFile) {
        if (Get-EnvValue $envFile 'AGENT_CONTROL_SECRET') { Ok '.env 有 AGENT_CONTROL_SECRET' } else { Bad '.env 缺 AGENT_CONTROL_SECRET' }
        $k = Get-EnvValue $envFile 'AGENT_LLM_API_KEY'
        if ($k -and $k -notmatch '(?i)replace|your|xxx|changeme') { Ok '.env 的 AGENT_LLM_API_KEY 已填' } else { Bad '.env 的 AGENT_LLM_API_KEY 还是占位符' }
        $cpu = Get-EnvValue $envFile 'AGENT_SANDBOX_CPU'
        if ($cpu -eq 'Nehalem') { Ok '.env 的 AGENT_SANDBOX_CPU=Nehalem' }
        elseif ($cpu) { Warn "AGENT_SANDBOX_CPU=$cpu（不是 Nehalem；guest 里 numpy 可能需要 SSE4.2）" }
        else { Warn 'AGENT_SANDBOX_CPU 是空的（建议 Nehalem）' }
    } else { Bad '没有 .env' }

    if (Test-Path -LiteralPath $platDisk) { Ok ("平台磁盘存在（{0}）" -f (Format-Size (Get-PathSize $platDisk))) } else { Bad '没有平台磁盘（第 4 步会重建，需要联网 + 10-40 分钟）' }
    if ((Test-Path -LiteralPath $sshKey) -and (Test-Path -LiteralPath "$sshKey.pub")) { Ok 'var\vm_key 存在（值不打印）' } else { Bad 'var\vm_key / var\vm_key.pub 缺失（登录平台 VM 用；私钥不进包）' }
    if (Get-PlatformQemuProcess) { Ok '平台 VM 的 QEMU 正在运行' } else { Warn '平台 VM 没在运行（start-agent.ps1 会拉起它）' }
    if (Get-PlatformQemuProcess) {
        if (Test-VmSsh) { Ok 'SSH 到平台 VM 通' } else { Bad 'SSH 到平台 VM 不通'; Hint '如果这磁盘来自别的机器：它的 authorized_keys 是旧公钥，换 var\vm_key 或重建 VM。' }
        if (Test-VmProvisioned) { Ok 'VM 内 /opt/agentbox/PROVISIONED 存在（平台配好了）' } else { Warn 'VM 内还没有 /opt/agentbox/PROVISIONED' }
    }
    $c = HttpOk 'http://127.0.0.1:8091/health'
    if ($c -eq 200) { Ok '控制平面 :8091/health = 200' } else { Warn "控制平面 :8091/health = $c（没在跑）" }
    $a = HttpOk 'http://127.0.0.1:8090/health' 10
    if ($a -eq 200) { Ok 'AI 服务 :8090/health = 200' } else { Warn "AI 服务 :8090/health = $a（没在跑）" }
}

# ==================================================================== 主流程
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'pyproject.toml'))) {
    Die "这里不是 agentbox 仓库根目录: $RepoRoot" '用 -File <解压目录>\packaging\setup-agentbox.ps1 运行，或加 -RepoRoot 指定。'
}
Push-Location -LiteralPath $RepoRoot
if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
Write-Host ''
Write-Host '################################################################' -ForegroundColor Cyan
Write-Host ' agentbox 一键安装（setup-agentbox.ps1）' -ForegroundColor Cyan
Write-Host (" 仓库: {0}" -f $RepoRoot)
Write-Host (" 模式: {0}" -f $(if ($Check) { '体检 (-Check)：不改任何东西' } elseif ($dryRun) { '干跑 (-WhatIf/-DryRun)' } else { '安装' }))
Write-Host (" 选项: SkipVm={0} SkipModels={1} Yes={2} UseIso={3}" -f [bool]$SkipVm, [bool]$SkipModels, [bool]$Yes, [bool]$UseIso)
Write-Host '################################################################' -ForegroundColor Cyan

try {
    if (-not $SkipChecks) { Invoke-Preflight } else { Step '1/8 预检（-SkipChecks：跳过）' }

    if ($Check) {
        Invoke-HealthCheck
    } else {
        Invoke-VenvStep
        Invoke-EnvStep
        Invoke-PlatformVmStep
        Invoke-ModelsStep
        Invoke-SandboxImageStep
        Invoke-StartStep
    }
} catch {
    Write-Host ''
    Write-Host ("✘ 出错了: " + $_.Exception.Message) -ForegroundColor Red
    [void]$script:Fail.Add("异常中断: " + $_.Exception.Message)
} finally {
    Pop-Location
}

$code = Show-Verdict
if ($null -eq $code) { $code = 1 }
exit $code
