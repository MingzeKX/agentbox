<#
.SYNOPSIS
    agentbox 安装器：在另一台 Windows 机器上把解压出来的包变成"能跑"的状态。

.DESCRIPTION
    做四件事：
      1. 前置检查（Windows / PowerShell / Python / 磁盘 / QEMU / 镜像 / .env / venv），
         每项打印 ✔ 或 ✘，失败项给出"下一步该敲什么"（中文）。
      2. 建 .venv 并装依赖：有 wheelhouse\（或开发机的 packaging\wheelhouse\）就完全离线装，
         没有就联网装（并明确告诉你这一点）。
      3. 生成 .env（从 .env.example），写入**新随机** AGENT_CONTROL_SECRET，
         API key 留占位符等你填；并尽力收紧 .env 的文件权限。
      4. 打印下一步清单 + 最后给一个"已就绪 / 未就绪"结论。

    -Check   只体检、不改任何东西（可放心在现有环境上跑）。
    -WhatIf  干跑：打印将要做什么，然后什么都不做（与 -DryRun 等价）。
    -Force   删掉 .venv 重建。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\packaging\install.ps1
    powershell -ExecutionPolicy Bypass -File .\packaging\install.ps1 -Check
    powershell -ExecutionPolicy Bypass -File .\packaging\install.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # 只体检，不做任何修改
    [switch]$Check,
    # 干跑（等价于 -WhatIf）
    [switch]$DryRun,
    # 重建 .venv
    [switch]$Force,
    # 不碰 .env（不生成、不修改）
    [switch]$SkipEnvFile,
    # 指定 Python 解释器（默认自动找 .venv -> py -3.13 -> py -3 -> python）
    [string]$Python,
    # 覆盖仓库根目录（默认脚本所在目录的上一级）
    [string]$RepoRoot
)

$ErrorActionPreference = 'Stop'
$dryRun = [bool]($DryRun -or $WhatIfPreference)
$script:Checks = New-Object System.Collections.ArrayList
$script:InstallFailed = $false
$script:SeededEnvFile = $false

# ==================================================================== 小工具
function Die([string]$msg, [string]$hint) {
    $full = $msg
    if ($hint) { $full = "$msg`n    下一步: $hint" }
    throw $full
}

function Format-Size([long]$bytes) {
    if ($bytes -lt 0) { return '不存在' }
    if ($bytes -ge 1GB) { return ('{0:N2} GB' -f ($bytes / 1GB)) }
    if ($bytes -ge 1MB) { return ('{0:N1} MB' -f ($bytes / 1MB)) }
    if ($bytes -ge 1KB) { return ('{0:N1} KB' -f ($bytes / 1KB)) }
    return ('{0} B' -f $bytes)
}

function Add-Check([string]$name, [string]$state, [string]$detail, [string]$fix) {
    $mark = [char]0x2714      # ✔
    $color = 'Green'
    if ($state -eq 'warn') { $mark = '!'; $color = 'Yellow' }
    elseif ($state -eq 'bad') { $mark = [char]0x2718; $color = 'Red' }   # ✘
    Write-Host ("  {0} {1}" -f $mark, $name) -ForegroundColor $color -NoNewline
    Write-Host ("  {0}" -f $detail)
    if ($state -ne 'ok' -and $fix) { Write-Host ("      修复: {0}" -f $fix) -ForegroundColor Yellow }
    [void]$script:Checks.Add([pscustomobject]@{ Name = $name; State = $state; Detail = $detail; Fix = $fix })
}

function Invoke-Step([string]$desc, [scriptblock]$action) {
    if ($dryRun) {
        Write-Host ("  [DRY-RUN] 会做: {0}" -f $desc) -ForegroundColor DarkGray
        return
    }
    Write-Host ("  -> {0}" -f $desc) -ForegroundColor DarkGray
    # 先清零：纯 PowerShell 的步骤不会设置 $LASTEXITCODE，别把上一条命令的残留当失败
    $global:LASTEXITCODE = 0
    & $action
    if ($global:LASTEXITCODE -ne 0) {
        throw ("命令失败（exit {0}）: {1}" -f $global:LASTEXITCODE, $desc)
    }
}

# 找一个真正能跑的解释器；返回 $null 表示没有
function Find-PythonCandidate([string]$explicitPath, [string]$repo, [switch]$IncludeVenv) {
    $cands = New-Object System.Collections.ArrayList
    if ($explicitPath) { [void]$cands.Add([pscustomobject]@{ Exe = $explicitPath; Pre = @() }) }
    if ($IncludeVenv) {
        $v = Join-Path $repo '.venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $v) { [void]$cands.Add([pscustomobject]@{ Exe = $v; Pre = @() }) }
    }
    $py = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($py) {
        [void]$cands.Add([pscustomobject]@{ Exe = $py.Source; Pre = @('-3.13') })
        [void]$cands.Add([pscustomobject]@{ Exe = $py.Source; Pre = @('-3') })
    }
    $p = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($p) { [void]$cands.Add([pscustomobject]@{ Exe = $p.Source; Pre = @() }) }
    foreach ($c in $cands) {
        $exe = $c.Exe
        if (-not (Test-Path -LiteralPath $exe)) {
            Write-Host ("      -Python 指定的路径不存在: {0}" -f $exe) -ForegroundColor Yellow
            continue
        }
        try {
            $v = & $exe @($c.Pre) -c "import sys;print(sys.version.split()[0])" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$v" -match '^\d+\.\d+\.\d+') {
                return [pscustomobject]@{ Exe = $exe; Pre = $c.Pre; Ver = "$v".Trim(); Path = $exe }
            }
            if ($exe -match 'WindowsApps') {
                Write-Host "      PATH 上的 python.exe 是 Microsoft Store 别名（执行返回 $LASTEXITCODE，不是真解释器）" -ForegroundColor Yellow
            }
        } catch { }
    }
    return $null
}

function Get-EnvFacts([string]$envPath) {
    $facts = [pscustomobject]@{ Exists = $false; SecretSet = $false; ApiKeySet = $false; ApiKeyPlaceholder = $false; RpcTokenSet = $false }
    if (-not (Test-Path -LiteralPath $envPath)) { return $facts }
    $facts.Exists = $true
    $text = Get-Content -LiteralPath $envPath -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
    if (-not $text) { return $facts }
    # 只判断"有没有值"，从不打印值本身
    $m = [regex]::Match($text, '(?m)^[ \t]*AGENT_CONTROL_SECRET[ \t]*=[ \t]*(\S+)')
    if ($m.Success) { $facts.SecretSet = $true }
    $m = [regex]::Match($text, '(?m)^[ \t]*AGENT_LLM_API_KEY[ \t]*=[ \t]*(\S+)')
    if ($m.Success) {
        $facts.ApiKeySet = $true
        if ($m.Groups[1].Value -match '(?i)^sk-(replace|your|xxx)|replace-me|changeme') { $facts.ApiKeyPlaceholder = $true }
    }
    $m = [regex]::Match($text, '(?m)^[ \t]*AGENT_RPC_TOKEN[ \t]*=[ \t]*(\S+)')
    if ($m.Success) { $facts.RpcTokenSet = $true }
    return $facts
}

function New-RandomHex([int]$bytes) {
    $buf = New-Object byte[] $bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($buf)
    $rng.Dispose()
    return (($buf | ForEach-Object { $_.ToString('x2') }) -join '')
}

# ==================================================================== 定位仓库
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
# wheelhouse 可能在两个地方：解压出来的包里是 <仓库>\wheelhouse\（make-bundle 放的），
# 开发机上则是 <仓库>\packaging\wheelhouse\。两个都认，优先包里的那个。
$wheelhouse = Join-Path $RepoRoot 'wheelhouse'
if (-not (Test-Path -LiteralPath $wheelhouse)) {
    $whAlt = Join-Path $RepoRoot 'packaging\wheelhouse'
    if (Test-Path -LiteralPath $whAlt) { $wheelhouse = $whAlt }
}
$whRel = 'wheelhouse'
if ($wheelhouse.StartsWith($RepoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    $whRel = $wheelhouse.Substring($RepoRoot.Length).TrimStart('\')
}

$exitCode = 0
try {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'pyproject.toml'))) {
        Die "这里不是 agentbox 仓库根目录: $RepoRoot（没有 pyproject.toml）" '把 zip 解压后用 -File <解压目录>\packaging\install.ps1 运行，或用 -RepoRoot 指定'
    }
    Push-Location -LiteralPath $RepoRoot
    try {
        Write-Host ''
        Write-Host '================================================================' -ForegroundColor Cyan
        Write-Host ' agentbox 安装器' -ForegroundColor Cyan
        Write-Host (" 仓库: {0}" -f $RepoRoot)
        Write-Host (" 模式: {0}" -f $(if ($Check) { '体检 (-Check)：不改任何东西' } elseif ($dryRun) { '干跑 (-WhatIf/-DryRun)' } else { '安装' }))
        Write-Host '================================================================' -ForegroundColor Cyan

        # ---------------------------------------------------------- 安装（非 -Check）
        if (-not $Check) {
            Write-Host ''
            Write-Host '[1/4] 环境准备' -ForegroundColor Cyan

            $py = Find-PythonCandidate $Python $RepoRoot -IncludeVenv
            if (-not $py) {
                Die '找不到可用的 Python 解释器' '装 Python 3.13+ x64（勾选 py launcher），或用 -Python C:\path\to\python.exe 指定；注意 Microsoft Store 的 python.exe 别名不可用'
            }
            Write-Host ("  Python: {0}  ({1})" -f $py.Ver, $py.Path) -ForegroundColor Green

            $haveVenv = Test-Path -LiteralPath $venvPy
            if ($haveVenv -and $Force) {
                Invoke-Step "删除旧的 .venv（-Force）" { Remove-Item -LiteralPath $venvDir -Recurse -Force }
                $haveVenv = $false
            }
            if (-not $haveVenv) {
                Invoke-Step ("创建虚拟环境 .venv（{0}）" -f $py.Path) {
                    & $py.Exe @($py.Pre) -m venv $venvDir
                }
                $haveVenv = Test-Path -LiteralPath $venvPy
                if (-not $haveVenv -and -not $dryRun) { Die 'python -m venv 没有产出 .venv\Scripts\python.exe' '看看上面的报错；venv 需要能写 <仓库>\.venv' }
            } else {
                Write-Host '  .venv 已存在，沿用（要重建加 -Force）' -ForegroundColor DarkGray
            }

            Write-Host ''
            Write-Host '[2/4] 安装依赖' -ForegroundColor Cyan
            $whWhl = @(Get-ChildItem -LiteralPath $wheelhouse -Filter *.whl -File -ErrorAction SilentlyContinue)
            if ($whWhl.Count -gt 0) {
                Write-Host ("  wheelhouse: {0} 个 wheel（离线安装）" -f $whWhl.Count) -ForegroundColor Green
                $offlineOk = $true
                try {
                    Invoke-Step ("pip install --no-index --find-links " + $whRel + " -e .") {
                        & $venvPy -m pip install --no-index --find-links $wheelhouse -e $RepoRoot --disable-pip-version-check
                    }
                } catch {
                    $offlineOk = $false
                    Write-Host ("  第一次离线编辑安装失败: " + $_.Exception.Message) -ForegroundColor Yellow
                    Write-Host '  改用 wheelhouse 里的构建后端重试（--no-build-isolation）：老 wheelhouse 少 editables 时会走到这里' -ForegroundColor Yellow
                }
                if (-not $offlineOk -and -not $dryRun) {
                    Invoke-Step 'pip install（离线，--no-build-isolation）' {
                        & $venvPy -m pip install --no-index --find-links $wheelhouse 'hatchling>=1.24' --disable-pip-version-check | Out-Null
                        & $venvPy -m pip install --no-index --find-links $wheelhouse 'editables>=0.3' --disable-pip-version-check | Out-Null
                        & $venvPy -m pip install --no-index --find-links $wheelhouse --no-build-isolation -e $RepoRoot --disable-pip-version-check
                    }
                }
                # 开发工具是可选加分项：缺了不算失败
                if ($dryRun) {
                    Write-Host '  [DRY-RUN] 会尝试离线补装 pytest / pytest-asyncio / ruff' -ForegroundColor DarkGray
                } else {
                    $devOk = $true
                    try {
                        & $venvPy -m pip install --no-index --find-links $wheelhouse 'pytest>=8.2' 'pytest-asyncio>=0.23' 'ruff>=0.5' --disable-pip-version-check 2>$null | Out-Null
                        if ($LASTEXITCODE -ne 0) { $devOk = $false }
                    } catch { $devOk = $false }
                    if ($devOk) { Write-Host '  开发工具（pytest/ruff）也已离线装上' -ForegroundColor DarkGray }
                    else { Write-Host '  （可选）开发工具没装上：wheelhouse 里可能没有，跳过' -ForegroundColor DarkGray }
                }
            } else {
                Write-Host ("  没有 {0}\：需要联网装依赖" -f $whRel) -ForegroundColor Yellow
                Write-Host '  若要离线安装，请在能上网的机器上重新打包：.\packaging\make-bundle.ps1 -IncludeWheelhouse' -ForegroundColor Yellow
                Invoke-Step 'pip install -e .（联网）' {
                    & $venvPy -m pip install -e $RepoRoot --disable-pip-version-check
                }
            }

            Write-Host ''
            Write-Host '[3/4] 配置文件 .env' -ForegroundColor Cyan
            if ($SkipEnvFile) {
                Write-Host '  -SkipEnvFile：跳过' -ForegroundColor DarkGray
            } elseif (Test-Path -LiteralPath $envFile) {
                Write-Host '  .env 已存在，不动它（要重新生成请自己删掉 .env）' -ForegroundColor Green
            } elseif (-not (Test-Path -LiteralPath $envExample)) {
                Write-Host '  找不到 .env.example，无法生成 .env' -ForegroundColor Red
            } else {
                Invoke-Step '从 .env.example 生成 .env 并写入随机 AGENT_CONTROL_SECRET' {
                    $text = [System.IO.File]::ReadAllText($envExample)
                    $secret = New-RandomHex 32
                    if ($text -match '(?m)^[ \t]*AGENT_CONTROL_SECRET[ \t]*=[^\r\n]*') {
                        $text = [regex]::Replace($text, '(?m)^[ \t]*AGENT_CONTROL_SECRET[ \t]*=[^\r\n]*', "AGENT_CONTROL_SECRET=$secret")
                    } else {
                        $text = $text.TrimEnd() + "`r`nAGENT_CONTROL_SECRET=$secret`r`n"
                    }
                    if ($text -notmatch '(?m)^[ \t]*AGENT_LLM_API_KEY[ \t]*=') {
                        $text = $text.TrimEnd() + "`r`nAGENT_LLM_API_KEY=sk-replace-me`r`n"
                    }
                    [System.IO.File]::WriteAllText($envFile, $text, (New-Object System.Text.UTF8Encoding($false)))
                    $script:SeededEnvFile = $true
                }
                if ($script:SeededEnvFile) {
                    Write-Host '  .env 已生成；AGENT_CONTROL_SECRET 是新的随机值（不打印，避免泄漏）' -ForegroundColor Green
                    Write-Host '  AGENT_LLM_API_KEY 仍是占位符 sk-replace-me —— 必须自己填' -ForegroundColor Yellow
                    # 尽力收紧权限：只留当前用户 + SYSTEM
                    try {
                        $me = "$env:USERDOMAIN\$env:USERNAME"
                        & icacls.exe $envFile /inheritance:r /grant:r "${me}:(F)" 'SYSTEM:(F)' 2>$null | Out-Null
                        if ($LASTEXITCODE -eq 0) { Write-Host '  已收紧 .env 权限（继承去掉，只留当前用户 + SYSTEM）' -ForegroundColor DarkGray }
                        else { Write-Host '  收紧 .env 权限失败（icacls 返回非 0），请手工检查' -ForegroundColor Yellow }
                    } catch {
                        Write-Host '  收紧 .env 权限失败（不影响使用），请手工检查' -ForegroundColor Yellow
                    }
                }
            }

            Write-Host ''
            Write-Host '[4/4] 运行时目录' -ForegroundColor Cyan
            $varDirs = @('var', 'var\logs', 'var\sandbox', 'var\platform', 'var\run', 'var\tmp', 'var\pytest-tmp')
            $made = 0
            foreach ($d in $varDirs) {
                $p = Join-Path $RepoRoot $d
                if (-not (Test-Path -LiteralPath $p)) {
                    if (-not $dryRun) { New-Item -ItemType Directory -Force -Path $p | Out-Null }
                    $made++
                }
            }
            if ($dryRun) { Write-Host ("  会创建/确认 {0} 个运行时目录" -f $varDirs.Count) -ForegroundColor DarkGray }
            else { Write-Host ("  var\ 运行时目录就绪（新建 {0} 个）" -f $made) -ForegroundColor Green }
        }

        # ---------------------------------------------------------- 状态报告
        Write-Host ''
        if ($Check) { Write-Host '=== 体检报告 ===' -ForegroundColor Cyan }
        else { Write-Host '=== 安装结果 / 就绪状态 ===' -ForegroundColor Cyan }

        # 1) Windows
        $cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue
        $osName = '未知'
        if ($cv) { $osName = ("{0} {1} (build {2})" -f $cv.ProductName, $cv.DisplayVersion, $cv.CurrentBuild) }
        $osVer = [System.Environment]::OSVersion.Version
        $osOk = ($osVer.Major -ge 10 -and [Environment]::Is64BitOperatingSystem)
        Add-Check 'Windows 版本' $(if ($osOk) { 'ok' } else { 'bad' }) $osName $(
            if ($osOk) { '' } else { 'agentbox 需要 Windows 10/11 x64（WHPX 加速）。换机器，或在 Windows 里开 Hyper-V/WHPX。' })

        # 2) PowerShell
        $psv = $PSVersionTable.PSVersion
        $psOk = ($psv.Major -gt 5 -or ($psv.Major -eq 5 -and $psv.Minor -ge 1))
        Add-Check 'PowerShell 版本' $(if ($psOk) { 'ok' } else { 'bad' }) ("{0}（需要 5.1+，脚本按 5.1 写）" -f $psv) $(
            if ($psOk) { '' } else { '用 Windows 自带的 powershell.exe（5.1）运行，不要用别的宿主。' })

        # 3) Python
        $pyFound = Find-PythonCandidate $Python $RepoRoot -IncludeVenv
        $pyState = 'bad'
        $pyDetail = '没找到可用的 Python'
        $pyFix = '装 Python 3.13+ x64（https://www.python.org/downloads/windows/ ，勾选 py launcher），或用 -Python 指定解释器；PATH 上的 Microsoft Store python.exe 别名不能用。'
        $pyMinor = -1
        if ($pyFound) {
            $pyMinor = [int](($pyFound.Ver -split '\.')[1])
            if ($pyMinor -ge 11) {
                $pyState = 'ok'
                if ($pyMinor -lt 13) { $pyState = 'warn' }
                $pyDetail = ("{0}  路径: {1}" -f $pyFound.Ver, $pyFound.Path)
                $pyFix = '建议 3.13+；本机 pyproject 要求 >=3.11。'
            } else {
                $pyDetail = ("{0}（太老）  路径: {1}" -f $pyFound.Ver, $pyFound.Path)
            }
        }
        Add-Check 'Python 解释器' $pyState $pyDetail $(if ($pyState -eq 'ok') { '' } else { $pyFix })

        # 4) 磁盘
        $drv = (Get-Item -LiteralPath $RepoRoot).PSDrive.Name
        $free = (Get-PSDrive -Name $drv -ErrorAction SilentlyContinue).Free
        $diskState = 'bad'
        $diskFix = '清出至少 2 GB（建 .venv + 依赖约 200 MB；如果还要放 VM 镜像，再要 15 GB+），或换到空间大的盘。'
        $diskDetail = '读不到剩余空间'
        if ($null -ne $free) {
            $diskDetail = ("{0}: 剩余 {1}（最低要求 2 GB；要放镜像另需 ~14 GB）" -f $drv, (Format-Size $free))
            if ($free -ge 20GB) { $diskState = 'ok' }
            elseif ($free -ge 2GB) { $diskState = 'warn' }
        }
        Add-Check '磁盘剩余空间' $diskState $diskDetail $(if ($diskState -eq 'ok') { '' } else { $diskFix })

        # 5) QEMU
        $qemuExe = $null
        foreach ($cand in @($env:AGENT_QEMU_DIR, (Join-Path $RepoRoot 'qemu'))) {
            if ($cand) {
                $p = Join-Path $cand 'qemu-system-x86_64.exe'
                if (Test-Path -LiteralPath $p) { $qemuExe = $p; break }
            }
        }
        if (-not $qemuExe) {
            $g = Get-Command 'qemu-system-x86_64.exe' -ErrorAction SilentlyContinue
            if ($g) { $qemuExe = $g.Source }
        }
        $qemuFix = '如果这份包是 -Lean/-Fat 打的，<仓库>\qemu\ 里应该已经有（精简白名单，约 210 MB）；否则自己准备：到 https://qemu.weilnetz.de/w64/ 装一份、或从旧机器 robocopy qemu\ 整个目录过来、或设 AGENT_QEMU_DIR 指向它。'
        if ($qemuExe) {
            $qemuImg = Join-Path (Split-Path -Parent $qemuExe) 'qemu-img.exe'
            $hasImg = Test-Path -LiteralPath $qemuImg
            Add-Check 'QEMU for Windows' $(if ($hasImg) { 'ok' } else { 'warn' }) ("{0}（qemu-img.exe: {1}）" -f $qemuExe, $(if ($hasImg) { '有' } else { '缺' })) $(if ($hasImg) { '' } else { 'qemu-img.exe 也要在同一个目录里（QEMU 官方 Windows 包自带）。' })
        } else {
            Add-Check 'QEMU for Windows' 'bad' '没找到 qemu-system-x86_64.exe' $qemuFix
        }

        # 6) 沙箱镜像
        $sbDir = Join-Path $RepoRoot 'var\sandbox'
        $sbNeed = @('rootfs.img', 'vmlinuz', 'initrd.img', 'workspace-blank.qcow2')
        $sbMissing = @()
        $sbFound = @()
        foreach ($n in $sbNeed) {
            $p = Join-Path $sbDir $n
            if (Test-Path -LiteralPath $p) { $sbFound += ("{0} {1}" -f $n, (Format-Size (Get-Item -LiteralPath $p).Length)) }
            else { $sbMissing += $n }
        }
        $sbFix = '沙箱镜像只能在平台 VM 里重建：先把仓库推进 VM（deploy\windows\push-repo-to-vm.ps1），在 VM 内 sudo bash deploy/sandbox/build-sandbox-image.sh，再在 Windows 上跑 deploy\windows\fetch-sandbox-image.ps1 把 4 个文件拉回来。'
        if ($sbMissing.Count -eq 0) {
            Add-Check '沙箱镜像 (var\sandbox)' 'ok' ($sbFound -join ' / ') ''
        } else {
            Add-Check '沙箱镜像 (var\sandbox)' 'bad' ("缺: {0}" -f ($sbMissing -join ', ')) $sbFix
        }

        # 7) 平台 VM
        $platDisk = Join-Path $RepoRoot 'var\platform\platform.qcow2'
        if (Test-Path -LiteralPath $platDisk) {
            Add-Check '平台 VM 磁盘' 'ok' ("platform.qcow2 {0}" -f (Format-Size (Get-Item -LiteralPath $platDisk).Length)) ''
        } else {
            Add-Check '平台 VM 磁盘' 'bad' '没有 var\platform\platform.qcow2' '先跑 deploy\windows\fetch-platform-image.ps1（下 Debian 13 云镜像，约 330 MB），再跑 deploy\windows\provision-cloud-vm.ps1（cloud-init 自动装 PostgreSQL/pgvector/python，安装期需要联网，10-40 分钟）。也可以从旧机器把 platform.qcow2 拷过来（-Fat 包里带），但那样还得带上配对的 var\vm_key。'
        }

        # 8) SSH 密钥
        $keyOk = (Test-Path -LiteralPath (Join-Path $RepoRoot 'var\vm_key')) -and (Test-Path -LiteralPath (Join-Path $RepoRoot 'var\vm_key.pub'))
        Add-Check 'SSH 私钥 (var\vm_key)' $(if ($keyOk) { 'ok' } else { 'bad' }) $(if ($keyOk) { '存在（内容不打印）' } else { '缺 var\vm_key / var\vm_key.pub' }) 'start-agent.ps1 要它登录平台 VM。从旧机器把 var\vm_key + var\vm_key.pub 拷过来（私钥属凭据，绝不进安装包）；或者重新跑 provision-cloud-vm.ps1，但那会重建平台 VM。'

        # 9) .env
        $envFacts = Get-EnvFacts $envFile
        if (-not $envFacts.Exists) {
            Add-Check '.env 配置文件' 'bad' '没有 .env' '运行本脚本（不要加 -Check）会自动从 .env.example 生成 .env 并写入随机 AGENT_CONTROL_SECRET。'
        } elseif (-not $envFacts.SecretSet) {
            Add-Check '.env / AGENT_CONTROL_SECRET' 'bad' '.env 存在但 AGENT_CONTROL_SECRET 为空' '填一个随机值：python -c "import secrets;print(secrets.token_hex(32))"（平台 VM 里的 .env 必须一致）。'
        } elseif (-not $envFacts.ApiKeySet -or $envFacts.ApiKeyPlaceholder) {
            Add-Check '.env / AGENT_LLM_API_KEY' 'bad' ("AGENT_CONTROL_SECRET 已设置；API key {0}" -f $(if ($envFacts.ApiKeyPlaceholder) { '还是占位符 sk-replace-me' } else { '没设置' })) '把你的 DeepSeek API key 填进 .env 的 AGENT_LLM_API_KEY=...（这是唯一必须手工做的一步）。'
        } else {
            Add-Check '.env 配置文件' 'ok' 'AGENT_CONTROL_SECRET 已设置，AGENT_LLM_API_KEY 已填（值不打印）' ''
        }

        # 10) venv + 依赖
        $venvOk = Test-Path -LiteralPath $venvPy
        if (-not $venvOk) {
            Add-Check 'Python 虚拟环境 .venv' 'bad' '没有 .venv' '运行本脚本（不要加 -Check）会创建 .venv 并装依赖；有 wheelhouse\ 时全程离线。'
        } else {
            $verOut = & $venvPy -c "import sys;print(sys.version.split()[0])" 2>$null
            # 这段代码只准用单引号：PS 5.1 传给原生 exe 时会吃掉双引号
            $depCode = @'
import importlib.util as u
mods = ['fastapi', 'uvicorn', 'httpx', 'pydantic', 'pydantic_settings',
        'sqlalchemy', 'asyncpg', 'pgvector', 'jsonschema', 'rich',
        'prompt_toolkit', 'agent']
print(','.join(m for m in mods if u.find_spec(m) is None))
'@
            $missing = (& $venvPy -c $depCode 2>$null)
            if ("$missing".Trim()) {
                Add-Check 'Python 依赖' 'bad' ("venv Python {0}；缺: {1}" -f $verOut, "$missing".Trim()) ("重建依赖：.\.venv\Scripts\python -m pip install --no-index --find-links " + $whRel + " -e .（有 wheelhouse 时）。")
            } else {
                $agentExe = Join-Path $venvDir 'Scripts\agent.exe'
                Add-Check 'Python 依赖' 'ok' ("venv Python {0}；11 个依赖 + agent 都在（agent.exe: {1}）" -f $verOut, $(if (Test-Path -LiteralPath $agentExe) { '有' } else { '缺' })) ''
            }
        }

        # 11) wheelhouse（离线能力）
        $whList = @(Get-ChildItem -LiteralPath $wheelhouse -Filter *.whl -File -ErrorAction SilentlyContinue)
        if ($whList.Count -gt 0) {
            $whBytes = ($whList | Measure-Object -Property Length -Sum).Sum
            # 版本特定的 wheel（cp314-cp314）只认精确版本；abi3 wheel（cp310-abi3）对 >=3.10 都可用；
            # py3-none-any 谁都能用。据此判断"这份 wheelhouse 能不能喂给当前解释器"。
            $specTags = New-Object System.Collections.ArrayList
            $abi3Min = -1
            foreach ($w in $whList) {
                $m = [regex]::Match($w.Name, '-cp3(\d+)-cp3\d+')
                if ($m.Success) { [void]$specTags.Add([int]$m.Groups[1].Value) }
                $m = [regex]::Match($w.Name, '-cp3(\d+)-abi3')
                if ($m.Success) {
                    $v = [int]$m.Groups[1].Value
                    if ($abi3Min -lt 0 -or $v -lt $abi3Min) { $abi3Min = $v }
                }
            }
            $specTags = @($specTags | Sort-Object -Unique)
            $tagText = '纯 Python (py3-none-any)'
            if ($specTags.Count -gt 0 -and $abi3Min -ge 0) { $tagText = ("cp3{0} + cp3{1}-abi3+" -f ($specTags -join '/cp3'), $abi3Min) }
            elseif ($specTags.Count -gt 0) { $tagText = ("cp3{0}" -f ($specTags -join '/cp3')) }
            elseif ($abi3Min -ge 0) { $tagText = ("cp3{0}-abi3+" -f $abi3Min) }
            $whDetail = ("{0} 个 wheel，{1}，{2}" -f $whList.Count, (Format-Size $whBytes), $tagText)
            $whState = 'ok'
            $whFix = ''
            if ($pyFound -and $pyMinor -ge 0) {
                $compatible = (($specTags.Count -eq 0) -and ($abi3Min -lt 0)) -or
                              ($specTags -contains $pyMinor) -or
                              ($abi3Min -ge 0 -and $pyMinor -ge $abi3Min)
                if (-not $compatible) {
                    $whState = 'warn'
                    $whDetail += ("；与当前 Python 3.{0} 不匹配" -f $pyMinor)
                    $whFix = ("这份 wheelhouse 是给 {0} 的，当前解释器 3.{1} 用不了，离线安装会失败：换用匹配的解释器，或在有网的机器上按 packaging\README.md 里的 pip download 命令重新生成。" -f $tagText, $pyMinor)
                }
            }
            Add-Check '离线 wheelhouse' $whState $whDetail $whFix
        } else {
            Add-Check '离线 wheelhouse' 'warn' ("没有 {0}\" -f $whRel) '要完全离线安装依赖，请在能上网的机器上跑 packaging\make-bundle.ps1 -IncludeWheelhouse，用产出的 zip（wheelhouse\ 会出现在解压目录根部）。'
        }

        # 12) 安装 ISO（只在重建平台 VM 时需要）
        $isos = @(Get-ChildItem -LiteralPath $RepoRoot -Filter '*.iso' -File -ErrorAction SilentlyContinue)
        if ($isos.Count -gt 0) {
            Add-Check 'Debian 安装 ISO' 'ok' (($isos | ForEach-Object { "{0} {1}" -f $_.Name, (Format-Size $_.Length) }) -join ' / ') ''
        } else {
            Add-Check 'Debian 安装 ISO' 'warn' '没有 *.iso（只在重建平台 VM / 重装时用到，已经跑起来的环境不需要）' '需要时下载 debian-13.x-amd64-netinst.iso 放到仓库根目录。'
        }

        # ---------------------------------------------------------- 下一步 + 结论
        $bad = @($script:Checks | Where-Object { $_.State -eq 'bad' })
        $warn = @($script:Checks | Where-Object { $_.State -eq 'warn' })

        if (-not $Check) {
            Write-Host ''
            Write-Host '=== 下一步（按顺序） ===' -ForegroundColor Cyan
            Write-Host '  0) 懒人版：powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Yes' -ForegroundColor White
            Write-Host '     （预检 + 建 venv + .env + 重建平台 VM + 下模型 + 起服务自检，幂等，可反复跑）'
            Write-Host '  1) 把 DeepSeek API key 填进 .env：' -ForegroundColor White
            Write-Host '     AGENT_LLM_API_KEY=sk-你的key        （文件：' -NoNewline; Write-Host $envFile -NoNewline; Write-Host '）'
            Write-Host '  2) QEMU：<仓库>\qemu\ 里要有 qemu-system-x86_64.exe 和 qemu-img.exe' -ForegroundColor White
            Write-Host '     （-Lean/-Fat 包里已经带精简版 ~210 MB；不是这两种包就自己准备完整版 ~1.2 GB，'
            Write-Host '      或设 AGENT_QEMU_DIR 指向已有安装）'
            Write-Host '  3) 平台 VM：有 var\platform\platform.qcow2 就跳过；' -ForegroundColor White
            Write-Host '     没有就先 .\deploy\windows\fetch-platform-image.ps1（下 Debian 云镜像），'
            Write-Host '     再 .\deploy\windows\provision-cloud-vm.ps1（cloud-init 自动配；安装期需要联网）'
            Write-Host '  4) 沙箱镜像：var\sandbox\ 里要有 rootfs.img / vmlinuz / initrd.img / workspace-blank.qcow2；' -ForegroundColor White
            Write-Host '     -Lean 包里已带；否则在平台 VM 里 bash deploy/sandbox/build-sandbox-image.sh，'
            Write-Host '     再 .\deploy\windows\fetch-sandbox-image.ps1'
            Write-Host '  5) 准备 SSH 私钥：var\vm_key + var\vm_key.pub（从旧机器拷，或 provision-cloud-vm.ps1 生成）' -ForegroundColor White
            Write-Host '  6) 启动：powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1' -ForegroundColor White
            Write-Host '  7) 复检：powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Check' -ForegroundColor White
        }

        Write-Host ''
        if ($bad.Count -eq 0) {
            Write-Host '================================================================' -ForegroundColor Green
            Write-Host (' 结论: 已就绪    (✔ {0} 项通过 / ! {1} 项提醒)' -f @($script:Checks | Where-Object { $_.State -eq 'ok' }).Count, $warn.Count) -ForegroundColor Green
            Write-Host '================================================================' -ForegroundColor Green
            if ($warn.Count -gt 0) {
                Write-Host ' 提醒（不挡路，但值得处理）:' -ForegroundColor Yellow
                $warn | ForEach-Object { Write-Host ("   ! {0}：{1}" -f $_.Name, $_.Detail) -ForegroundColor Yellow }
            }
        } else {
            $exitCode = 1
            Write-Host '================================================================' -ForegroundColor Red
            Write-Host (' 结论: 未就绪    还有 {0} 项要修：' -f $bad.Count) -ForegroundColor Red
            Write-Host '================================================================' -ForegroundColor Red
            $i = 0
            foreach ($b in $bad) {
                $i++
                Write-Host ("  {0}. {1} —— {2}" -f $i, $b.Name, $b.Detail) -ForegroundColor Red
                if ($b.Fix) { Write-Host ("     修复: {0}" -f $b.Fix) -ForegroundColor Yellow }
            }
            if ($warn.Count -gt 0) {
                Write-Host ''
                Write-Host (' 另有 {0} 项提醒：{1}' -f $warn.Count, (($warn | ForEach-Object { $_.Name }) -join '、')) -ForegroundColor Yellow
            }
        }
        if ($dryRun) {
            Write-Host ''
            Write-Host ' DRY-RUN：上面只打印了"会做什么"，没有修改任何文件。' -ForegroundColor DarkGray
        }
        Write-Host ''
    } finally {
        Pop-Location
    }
} catch {
    Write-Host ''
    Write-Host ("✘ 安装失败: {0}" -f $_.Exception.Message) -ForegroundColor Red
    $exitCode = 1
}
exit $exitCode
