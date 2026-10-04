<#
.SYNOPSIS
    把 agentbox 打成一个可以搬到另一台 Windows 机器上的 zip（离线安装包）。

.DESCRIPTION
    文件清单以 `git ls-files` 为准，所以天然不含 var\、.env、.venv\、qemu\、*.iso；
    再补上 deploy\、packaging\install.ps1、BUNDLE-README.md 和 MANIFEST.sha256。

    三个预设（推荐 -Lean）：
      -Lean  源码 + deploy\ + packaging\ + wheelhouse\ + 精简 QEMU + 沙箱镜像     ≈ 1.7 GB
      -Fat   -Lean + 平台磁盘 platform.qcow2 + Debian ISO                        ≈ 13 GB
      （都不给）只打代码包（+ wheelhouse），镜像/QEMU 由对方自己准备

    精简 QEMU 白名单（详见 packaging\README.md）：
      保留 根目录全部 *.dll（114 个）+ qemu-system-x86_64.exe / qemu-system-x86_64w.exe /
           qemu-img.exe + 许可文件 + share\ 里 x86 用得到的固件
           （bios*.bin / vgabios*.bin / kvmvapic / linuxboot_dma / multiboot_dma / pvh /
            efi-*.rom / pxe-*.rom / qboot.rom / edk2-i386*.fd / edk2-x86_64*.fd /
            qemu_vga.ndrv / keymaps\ / firmware\）+ lib\
      丢弃 其它架构的 qemu-system-*.exe、qemu-io/nbd/storage-daemon/ga/edid/uninstall、
           share\ 里 arm/aarch64/riscv/loongarch 的 *.fd（~287 MB）、doc\ / icons\（~29 MB）、
           dtb\ / man\ / locale\ / applications\
      -FullQemu 则原样复制整个 qemu\（多 ~988 MB，其中大半是本用不到的东西）。

    打包协议（硬要求：包必须能对应到一个 commit）：
      * 只从 **git 提交态** 收源码 —— 工作树必须干净：`git status --porcelain` 里不能有
        tracked 改动，src/tests/deploy 下也不能有未跟踪文件。脏树**直接中止并列出问题**。
        原因（真事）：另一个代理曾同时改 src/tests，打出来的包里那份源码和仓库对不上，
        对方遇到问题我们无法复现。
      * 确实要打"脏树快照"就显式加 -AllowDirty：包名会带 `-dirty`，
        SOURCE-COMMIT.txt 里写明 worktree=DIRTY（这种包**不要**当正式交付）。
      * 包内记录来源 commit：根目录 `SOURCE-COMMIT.txt`（完整 hash + describe + branch +
        打包时间 + 脏项清单）、`BUNDLE-README.md` 顶部、`MANIFEST.sha256` 头部注释。
      * `-IncludePlatform` 时如果平台磁盘正被 QEMU 占用，会拒绝拷贝（拷正在写的 qcow2 会得到坏镜像）。
      * `-DryRun`：跑完检查和元数据但**不产出 zip**（staging 留在原地供复核），用来验证打包协议。

    永远不进包（会打印被跳过的文件名）：
      .env / .env.*（密钥）、var\vm_key（SSH 私钥）、*.pem / *.key / id_rsa*、
      以及任何文件内容里出现"真实"密钥赋值（AGENT_LLM_API_KEY=sk-xxxx…）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\packaging\make-bundle.ps1 -Lean
    powershell -ExecutionPolicy Bypass -File .\packaging\make-bundle.ps1 -Fat -OutDir D:\ship
    powershell -ExecutionPolicy Bypass -File .\packaging\make-bundle.ps1 -Lean -DryRun      # 只复核，不产出 zip
    powershell -ExecutionPolicy Bypass -File .\packaging\make-bundle.ps1 -AllowDirty -DryRun # 看脏树会被记成什么
#>
[CmdletBinding()]
param(
    # 推荐预设：源码 + wheelhouse + 精简 QEMU + 沙箱镜像（不含平台磁盘/ISO）
    [switch]$Lean,
    # 全量预设：-Lean + 平台磁盘 platform.qcow2 + Debian ISO
    [switch]$Fat,
    # --- 以下是细粒度开关，可以单独组合使用 ---
    # 把 packaging\wheelhouse\ 也打进包（离线建 venv 用）
    [switch]$IncludeWheelhouse,
    # 旧名字：= 沙箱镜像 + 平台磁盘 + ISO（不含 QEMU）
    [switch]$IncludeImages,
    # 沙箱镜像 var\sandbox\ 的 4 个文件（1.5 GB）
    [switch]$IncludeSandbox,
    # 平台磁盘 var\platform\platform.qcow2（10.6 GB）
    [switch]$IncludePlatform,
    # 仓库根的 Debian 安装 ISO（0.74 GB）
    [switch]$IncludeIso,
    # QEMU for Windows（默认走精简白名单）
    [switch]$IncludeQemu,
    # 复制整个 qemu\（+~988 MB）
    [switch]$FullQemu,
    # 顺带下载的可选 extras（逗号分隔，例如 'voice,asr'）；默认只带基础依赖 + 开发工具
    [string]$WheelExtras = '',
    # 输出目录，默认 <仓库>\dist
    [string]$OutDir,
    # 已存在同名 zip 时覆盖
    [switch]$Force,
    # 覆盖版本号（默认取 pyproject.toml 的 project.version）
    [string]$Version,
    # 覆盖仓库根目录（默认脚本的上一级）
    [string]$RepoRoot,
    # 允许在"脏"工作树上打包。默认是**拒绝**的：曾经发生过"另一个代理正在改
    # src/tests，包里的源码和仓库对不上"，所以现在要求工作树干净（见脚本头部说明）。
    [switch]$AllowDirty,
    # 只跑检查 + 准备 staging + 写 SOURCE-COMMIT/MANIFEST/BUNDLE-README，**不产出 zip**，
    # 并把 staging 目录留在原地供检查（用于复核打包协议，不会误发）。
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# 精简 QEMU 白名单（改这里就改了打包规则，README 里有对应说明）
$QemuKeepExeNames = @('qemu-system-x86_64.exe', 'qemu-system-x86_64w.exe', 'qemu-img.exe')
$QemuKeepDocNames = @('COPYING', 'COPYING.LIB', 'README.rst', 'VERSION')
$QemuKeepSharePatterns = @(
    'bios*.bin', 'vgabios*.bin', 'kvmvapic.bin', 'linuxboot_dma.bin', 'multiboot_dma.bin',
    'pvh.bin', 'qemu_vga.ndrv', 'efi-*.rom', 'pxe-*.rom', 'qboot.rom',
    'edk2-i386*.fd', 'edk2-x86_64*.fd'
)
$QemuKeepShareDirs = @('keymaps', 'firmware')

# ------------------------------------------------------------------ 小工具
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

function Get-PathSize([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return [long](-1) }
    $item = Get-Item -LiteralPath $path -Force
    if (-not $item.PSIsContainer) { return [long]$item.Length }
    $sum = (Get-ChildItem -LiteralPath $path -Recurse -Force -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { return [long]0 }
    return [long]$sum
}

function Copy-Relative([System.IO.FileInfo]$file, [string]$srcRoot, [string]$dstRoot) {
    $rel = $file.FullName.Substring($srcRoot.Length).TrimStart('\')
    $dst = Join-Path $dstRoot $rel
    $dstDir = Split-Path -Parent $dst
    if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
    Copy-Item -LiteralPath $file.FullName -Destination $dst -Force
}

# 文件名级密钥黑名单
function Test-ForbiddenName([string]$name) {
    if ($name -ieq '.env.example') { return $false }   # 模板，必须进包
    if ($name -ieq '.env') { return $true }
    if ($name -match '^\.env\.') { return $true }      # .env.local / .env.prod ...
    if ($name -match '(?i)^(vm_key|id_rsa|id_dsa|id_ecdsa|id_ed25519)') { return $true }
    if ($name -match '(?i)\.(pem|key|pfx|p12|kdbx|jks|ppk)$') { return $true }
    if ($name -match '(?i)(credential|secret)') { return $true }
    return $false
}

# 内容级密钥扫描：只看高置信度的"真密钥赋值"，占位符放过
$SecretVars = 'AGENT_LLM_API_KEY|AGENT_CONTROL_SECRET|AGENT_RPC_TOKEN|AGENT_DASHSCOPE_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|HF_TOKEN'
function Test-ForbiddenContent([string]$full, [string]$rel) {
    if ([System.IO.Path]::GetFileName($rel) -ieq '.env.example') { return $false }
    $ext = [System.IO.Path]::GetExtension($rel).ToLower()
    $textExt = @('', '.py', '.ps1', '.sh', '.tpl', '.md', '.toml', '.cfg', '.ini', '.json', '.yaml', '.yml', '.txt', '.service', '.example')
    if ($textExt -notcontains $ext) { return $false }
    if ((Get-Item -LiteralPath $full -Force).Length -gt 1MB) { return $false }
    $text = Get-Content -LiteralPath $full -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
    if (-not $text) { return $false }
    $pattern = '(?im)^\s*(?:export\s+|set\s+)?(?:' + $SecretVars + ')\s*=\s*["'']?([^\s"''#]+)'
    foreach ($m in [regex]::Matches($text, $pattern)) {
        $val = $m.Groups[1].Value
        if ($val -match '(?i)^(sk-(replace|your|xxx)|replace|your|xxx|changeme|placeholder|todo|none|null|empty|\.\.\.|<.*>|\$\{.*\}|\$env:.*)$') { continue }
        if ($val -match '(?i)replace|your-|yourkey|<.*>') { continue }
        return $true
    }
    return $false
}

function Resolve-Python {
    $cands = New-Object System.Collections.ArrayList
    $venv = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { [void]$cands.Add([pscustomobject]@{ Exe = $venv; Pre = @() }) }
    $py = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($py) { [void]$cands.Add([pscustomobject]@{ Exe = $py.Source; Pre = @('-3') }) }
    $p = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($p) { [void]$cands.Add([pscustomobject]@{ Exe = $p.Source; Pre = @() }) }
    foreach ($c in $cands) {
        if (-not (Test-Path -LiteralPath $c.Exe)) { continue }
        try {
            $v = & $c.Exe @($c.Pre) -c "import sys;print('%d.%d'%(sys.version_info[0],sys.version_info[1]))" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$v" -match '^\d+\.\d+$') {
                return [pscustomobject]@{ Exe = $c.Exe; Pre = $c.Pre; Ver = "$v" }
            }
        } catch { }
    }
    return $null
}

function Get-RequirementList([string]$pyproject, [string[]]$extras) {
    # 注意：这段代码只准用单引号。Windows PowerShell 5.1 把参数交给原生 exe 时会把
    # 双引号吃掉（python -c 收到的是没有引号的代码 -> SyntaxError）。
    $code = @'
import json, sys, tomllib
with open(sys.argv[1], 'rb') as fh:
    data = tomllib.load(fh)
reqs = list(data['project']['dependencies'])
opt = data['project'].get('optional-dependencies', {})
for name in sys.argv[2:]:
    if name not in opt:
        raise SystemExit('unknown extra: ' + name)
    reqs += opt[name]
print(json.dumps(reqs))
'@
    $argv = @($pyproject) + $extras
    $out = & $script:Py.Exe @($script:Py.Pre) -c $code @argv
    if ($LASTEXITCODE -ne 0) { Die "解析 pyproject.toml 失败（extras: $($extras -join ',')）" '检查 -WheelExtras 拼写' }
    return @($out | ConvertFrom-Json)
}

# ------------------------------------------------------------------ 主流程
$script:Py = $null
$script:Staging = $null
$script:KeepStaging = $false
$exitCode = 0

try {
    if (-not $RepoRoot) { $RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path }
    $RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'pyproject.toml'))) {
        Die "看起来不是 agentbox 仓库: $RepoRoot（没有 pyproject.toml）" '用 -RepoRoot 指定仓库根目录'
    }

    # --- 预设展开 --------------------------------------------------------
    if ($Lean -and $Fat) { Die '-Lean 和 -Fat 只能选一个' '要全量就用 -Fat，要推荐的那份就用 -Lean' }
    $preset = 'custom'
    if ($Lean) {
        $preset = 'lean'
        $IncludeWheelhouse = $true
        $IncludeSandbox = $true
        $IncludeQemu = $true
    } elseif ($Fat) {
        $preset = 'fat'
        $IncludeWheelhouse = $true
        $IncludeSandbox = $true
        $IncludePlatform = $true
        $IncludeIso = $true
        $IncludeQemu = $true
    }
    if ($IncludeImages) { $IncludeSandbox = $true; $IncludePlatform = $true; $IncludeIso = $true }
    if ($FullQemu) { $IncludeQemu = $true }

    Write-Host ''
    Write-Host '=== agentbox 打包 ===' -ForegroundColor Cyan
    Write-Host "仓库  : $RepoRoot"
    Write-Host ("预设  : {0}" -f $(if ($preset -eq 'lean') { '-Lean（推荐）' } elseif ($preset -eq 'fat') { '-Fat（全量）' } else { '自定义' }))

    # --- 0. git 清单 ---------------------------------------------------
    $git = Get-Command 'git.exe' -ErrorAction SilentlyContinue
    if (-not $git) { Die '找不到 git.exe' '安装 Git for Windows 后重试（打包依赖 git ls-files 来取源码清单）' }
    $inside = (& git -C $RepoRoot rev-parse --is-inside-work-tree 2>$null)
    if ("$inside".Trim() -ne 'true') { Die "$RepoRoot 不是 git 工作区" '在仓库里执行 git init/commit 后重试' }
    $tracked = @(& git -C $RepoRoot ls-files)
    if ($LASTEXITCODE -ne 0) { Die 'git ls-files 失败' '检查 .git 是否损坏' }
    if ($tracked.Count -lt 20) { Die "git ls-files 只返回 $($tracked.Count) 个文件，太少" '确认已经 commit 了源码快照（打包只收 tracked 文件）' }

    # --- 0b. 源码冻结检查（打出来的包必须能对应到一个 commit）-------------
    # 为什么要有这一步：工作树是"活的"，别的代理可能正在改 src/tests。
    # 从脏树里收源码 = 交付的包和仓库对不上，出问题无法复现。
    $porcelain = @(& git -C $RepoRoot status --porcelain)
    $trackedDirty = @($porcelain | Where-Object { "$_" -notmatch '^\?\?' })
    $untrackedAll = @($porcelain | Where-Object { "$_" -match '^\?\?' } | ForEach-Object { "$_".Substring(3).Trim().Trim('"') })
    # 未跟踪的源码（src/tests/deploy 下的新文件）同样危险；packaging\ / dist\ 之类是我们自己的产物，容忍
    $untrackedSource = @($untrackedAll | Where-Object { $_ -match '^(src|tests|deploy)[\\/]' })
    $untrackedExtra = @($untrackedAll | Where-Object { $_ -notmatch '^(src|tests|deploy)[\\/]' })
    $isDirty = ($trackedDirty.Count -gt 0 -or $untrackedSource.Count -gt 0)

    $commit = "$(& git -C $RepoRoot rev-parse HEAD 2>$null)".Trim()
    if (-not $commit) { Die '拿不到 HEAD commit' '仓库没有提交？先 git commit' }
    $branch = "$(& git -C $RepoRoot rev-parse --abbrev-ref HEAD 2>$null)".Trim()
    $describe = "$(& git -C $RepoRoot describe --tags --always --dirty 2>$null)".Trim()
    if (-not $describe) { $describe = $commit.Substring(0, 12) }
    $packagedAt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')

    if ($isDirty) {
        if (-not $AllowDirty) {
            Write-Host ''
            Write-Host '工作树不干净，拒绝打包：包里那份源码会和仓库对不上，出问题无法复现。' -ForegroundColor Red
            if ($trackedDirty.Count -gt 0) {
                Write-Host ("  tracked 文件已改动/暂存（{0} 项）:" -f $trackedDirty.Count) -ForegroundColor Yellow
                $trackedDirty | Select-Object -First 20 | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
            }
            if ($untrackedSource.Count -gt 0) {
                Write-Host ("  src/tests/deploy 下有未跟踪文件（{0} 项）:" -f $untrackedSource.Count) -ForegroundColor Yellow
                $untrackedSource | Select-Object -First 20 | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
            }
            Die "工作树脏（tracked 改动 $($trackedDirty.Count) 项 + 未跟踪源码 $($untrackedSource.Count) 项），已中止" '等源码冻结/提交后重跑；确实要打"脏树快照"就加 -AllowDirty（包名和 SOURCE-COMMIT.txt 会标成 dirty）'
        }
        Write-Host ''
        Write-Host '⚠ -AllowDirty：工作树是脏的，这个包只是快照，不对应任何 commit。' -ForegroundColor Yellow
        $trackedDirty | Select-Object -First 10 | ForEach-Object { Write-Host "    tracked: $_" -ForegroundColor DarkYellow }
        $untrackedSource | Select-Object -First 10 | ForEach-Object { Write-Host "    untracked: $_" -ForegroundColor DarkYellow }
    }
    Write-Host ("源码  : {0} ({1})" -f $describe, $commit)
    Write-Host ("工作树: {0}" -f $(if ($isDirty) { '脏（-AllowDirty）' } else { '干净 ✔' }))

    # --- 1. 版本 / 输出路径 --------------------------------------------
    if (-not $Version) {        $pj = Get-Content -LiteralPath (Join-Path $RepoRoot 'pyproject.toml') -Raw -Encoding UTF8
        $m = [regex]::Match($pj, '(?m)^\s*version\s*=\s*"([^"]+)"')
        if (-not $m.Success) { Die 'pyproject.toml 里找不到 project.version' '用 -Version 指定版本号' }
        $Version = $m.Groups[1].Value
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmm'
    $suffix = ''
    if ($preset -ne 'custom') { $suffix = "-$preset" }
    if ($isDirty) { $suffix = "$suffix-dirty" }
    $bundleName = "agentbox-$Version-$stamp$suffix"
    if (-not $OutDir) { $OutDir = Join-Path $RepoRoot 'dist' }
    elseif (-not [System.IO.Path]::IsPathRooted($OutDir)) { $OutDir = Join-Path $RepoRoot $OutDir }
    $OutDir = [System.IO.Path]::GetFullPath($OutDir)
    if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
    # 让输出目录自己忽略自己：产物不该把 git status 弄脏（根 .gitignore 不归打包脚本管）
    $outIgnore = Join-Path $OutDir '.gitignore'
    if (-not (Test-Path -LiteralPath $outIgnore)) {
        [System.IO.File]::WriteAllText($outIgnore, "*`r`n", (New-Object System.Text.UTF8Encoding($false)))
    }
    $zipPath = Join-Path $OutDir "$bundleName.zip"
    if ((Test-Path -LiteralPath $zipPath) -and -not $Force) {
        Die "已存在 $zipPath" '加 -Force 覆盖'
    }

    $script:Staging = Join-Path $OutDir "_staging-$bundleName"
    if (Test-Path -LiteralPath $script:Staging) { Remove-Item -LiteralPath $script:Staging -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $script:Staging | Out-Null

    # --- 2. tracked 源码 -------------------------------------------------
    $copied = 0
    $skipped = New-Object System.Collections.ArrayList
    $srcBytes = [long]0
    foreach ($rel in $tracked) {
        $rel = "$rel".Trim()
        if (-not $rel) { continue }
        $relNative = $rel -replace '/', '\'
        $src = Join-Path $RepoRoot $relNative
        if (-not (Test-Path -LiteralPath $src)) { Die "tracked 文件不存在: $rel" 'git status 看一眼，或 git checkout -- . 恢复' }
        $name = [System.IO.Path]::GetFileName($rel)
        if (Test-ForbiddenName $name) { [void]$skipped.Add("$rel  (文件名匹配密钥规则)"); continue }
        if (Test-ForbiddenContent $src $rel) { [void]$skipped.Add("$rel  (内容里有真实密钥赋值)"); continue }
        $dst = Join-Path $script:Staging $relNative
        $dstDir = Split-Path -Parent $dst
        if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
        Copy-Item -LiteralPath $src -Destination $dst -Force
        $copied++
        $srcBytes += (Get-Item -LiteralPath $src -Force).Length
    }
    if ($copied -eq 0) { Die '一个文件都没复制成功' '检查仓库权限' }
    Write-Host ("[1/7] 源码 {0} 个文件，{1}" -f $copied, (Format-Size $srcBytes)) -ForegroundColor Green

    # deploy\ 必须完整（tracked 之外若有文件也提示出来，别装作打进去了）
    $deployDst = Join-Path $script:Staging 'deploy'
    $deployFiles = @(Get-ChildItem -LiteralPath $deployDst -Recurse -Force -File -ErrorAction SilentlyContinue)
    if ($deployFiles.Count -eq 0) { Die 'deploy\ 没进包' '确认 deploy\ 已被 git 跟踪' }
    Write-Host ("[2/7] deploy\ {0} 个文件" -f $deployFiles.Count) -ForegroundColor Green
    $untrackedDeploy = @(& git -C $RepoRoot status --porcelain --untracked-files=all -- deploy)
    if ($untrackedDeploy.Count -gt 0) {
        Write-Host '      注意：deploy\ 下有未跟踪/未提交的改动，这些内容不会进包：' -ForegroundColor Yellow
        $untrackedDeploy | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkYellow }
    }

    # --- 3. packaging\ 的脚本 + 说明（install.ps1 / setup-agentbox.ps1 / README.md ...）
    $installSrc = Join-Path $RepoRoot 'packaging\install.ps1'
    $setupSrc = Join-Path $RepoRoot 'packaging\setup-agentbox.ps1'
    if (-not (Test-Path -LiteralPath $installSrc)) { Die "缺少 $installSrc" '打包必须带上安装器' }
    if (-not (Test-Path -LiteralPath $setupSrc)) { Die "缺少 $setupSrc" '打包必须带上一键脚本（BUNDLE-README 的第一条用法就是它）' }
    $pkgDst = Join-Path $script:Staging 'packaging'
    New-Item -ItemType Directory -Force -Path $pkgDst | Out-Null
    $pkgFiles = @(Get-ChildItem -LiteralPath (Join-Path $RepoRoot 'packaging') -File | Where-Object {
        $_.Extension -ieq '.ps1' -or $_.Extension -ieq '.md'
    })
    foreach ($f in $pkgFiles) { Copy-Item -LiteralPath $f.FullName -Destination (Join-Path $pkgDst $f.Name) -Force }
    Write-Host ("[3/7] packaging\ {0} 个文件已加入（含一键脚本 setup-agentbox.ps1）" -f $pkgFiles.Count) -ForegroundColor Green

    # --- 4. wheelhouse ---------------------------------------------------
    $whSrc = Join-Path $RepoRoot 'packaging\wheelhouse'
    $whWhl = @()
    if ($IncludeWheelhouse) {
        if (-not (Test-Path -LiteralPath $whSrc)) { New-Item -ItemType Directory -Force -Path $whSrc | Out-Null }
        $script:Py = Resolve-Python
        if (-not $script:Py) { Die '找不到可用的 Python 解释器（试过 .venv\Scripts\python.exe、py -3、python）' '先装 Python 3.13+ 并让 py 启动器可用' }
        $extras = @()
        if ($WheelExtras) { $extras = @($WheelExtras -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
        $reqs = Get-RequirementList (Join-Path $RepoRoot 'pyproject.toml') $extras
        # 离线建 venv 还需要构建后端（hatchling + 它做 editable 构建时要用的 editables），
        # 以及跑测试要用的开发工具。少了 editables，pip install -e . 会在
        # "Installing backend dependencies" 这一步离线失败（实测踩过）。
        $reqs = @($reqs) + @('hatchling>=1.24', 'editables>=0.3', 'pip', 'setuptools', 'wheel', 'pytest>=8.2', 'pytest-asyncio>=0.23', 'ruff>=0.5')
        $reqs = @($reqs | Select-Object -Unique)
        Write-Host ("[4/7] pip download -> packaging\wheelhouse  (Python {0}, {1} 个依赖)" -f $script:Py.Ver, $reqs.Count) -ForegroundColor Green
        $dlArgs = @($script:Py.Pre) + @('-m', 'pip', 'download', '--dest', $whSrc, '--disable-pip-version-check', '--progress-bar', 'off') + $reqs
        & $script:Py.Exe @dlArgs
        if ($LASTEXITCODE -ne 0) {
            Die "pip download 失败（exit $LASTEXITCODE）" '检查网络/代理，或去掉 -IncludeWheelhouse 只打代码包'
        }
        $whWhl = @(Get-ChildItem -LiteralPath $whSrc -Filter *.whl -File -ErrorAction SilentlyContinue)
        if ($whWhl.Count -eq 0) { Die "wheelhouse 里没有任何 .whl" 'pip download 没有产出，检查 pip 版本/网络' }
        $whBytes = (Get-ChildItem -LiteralPath $whSrc -Recurse -Force -File | Measure-Object -Property Length -Sum).Sum
        Write-Host ("      {0} 个 wheel，{1}；这些 wheel 只适用于 Python {2} + Windows x64" -f $whWhl.Count, (Format-Size $whBytes), $script:Py.Ver) -ForegroundColor Yellow
        Copy-Item -LiteralPath $whSrc -Destination (Join-Path $script:Staging 'wheelhouse') -Recurse -Force
    } else {
        $whMissing = ''
        if (Test-Path -LiteralPath $whSrc) {
            $n = @(Get-ChildItem -LiteralPath $whSrc -Filter *.whl -File -ErrorAction SilentlyContinue).Count
            if ($n -gt 0) { $whMissing = "（packaging\wheelhouse 里已有 $n 个 wheel，加 -IncludeWheelhouse 才会打进包）" }
        }
        Write-Host "[4/7] 跳过 wheelhouse$whMissing" -ForegroundColor DarkGray
    }

    # --- 5. QEMU for Windows（可选）---------------------------------------
    $qemuDst = Join-Path $script:Staging 'qemu'
    $qemuSrc = Join-Path $RepoRoot 'qemu'
    $qemuIncluded = $false
    $qemuKeptCount = 0
    $qemuKeptBytes = [long]0
    $qemuDroppedCount = 0
    $qemuDroppedBytes = [long]0
    if ($IncludeQemu) {
        $qemuExe = Join-Path $qemuSrc 'qemu-system-x86_64.exe'
        if (-not (Test-Path -LiteralPath $qemuExe)) {
            Die "缺少 $qemuExe" 'QEMU for Windows 不在包里也打不出来：到 https://qemu.weilnetz.de/w64/ 装一份，或从旧机器拷 qemu\ 过来'
        }
        New-Item -ItemType Directory -Force -Path $qemuDst | Out-Null
        if ($FullQemu) {
            Copy-Item -LiteralPath $qemuSrc -Destination $qemuDst -Recurse -Force
            $qemuKeptCount = @(Get-ChildItem -LiteralPath $qemuDst -Recurse -Force -File).Count
            $qemuKeptBytes = Get-PathSize $qemuDst
            Write-Host ("[5/7] QEMU 全量已加入（{0} 个文件，{1}；没有瘦身）" -f $qemuKeptCount, (Format-Size $qemuKeptBytes)) -ForegroundColor Yellow
        } else {
            $keepList = New-Object System.Collections.ArrayList
            foreach ($f in @(Get-ChildItem -LiteralPath $qemuSrc -File)) {
                if ($f.Extension -ieq '.dll' -or $QemuKeepExeNames -contains $f.Name -or $QemuKeepDocNames -contains $f.Name) {
                    [void]$keepList.Add($f)
                }
            }
            $shareSrc = Join-Path $qemuSrc 'share'
            if (Test-Path -LiteralPath $shareSrc) {
                foreach ($f in @(Get-ChildItem -LiteralPath $shareSrc -File)) {
                    $n = $f.Name
                    if (@($QemuKeepSharePatterns | Where-Object { $n -like $_ }).Count -gt 0) { [void]$keepList.Add($f) }
                }
                foreach ($d in $QemuKeepShareDirs) {
                    $p = Join-Path $shareSrc $d
                    if (Test-Path -LiteralPath $p) { foreach ($f in @(Get-ChildItem -LiteralPath $p -Recurse -Force -File)) { [void]$keepList.Add($f) } }
                }
            }
            $libSrc = Join-Path $qemuSrc 'lib'
            if (Test-Path -LiteralPath $libSrc) { foreach ($f in @(Get-ChildItem -LiteralPath $libSrc -Recurse -Force -File)) { [void]$keepList.Add($f) } }
            $keepNames = @($keepList | ForEach-Object { $_.FullName })
            foreach ($f in $keepList) {
                Copy-Relative $f $qemuSrc $qemuDst
                $qemuKeptCount++
                $qemuKeptBytes += $f.Length
            }
            $allQemu = @(Get-ChildItem -LiteralPath $qemuSrc -Recurse -Force -File)
            foreach ($f in $allQemu) {
                if ($keepNames -notcontains $f.FullName) { $qemuDroppedCount++; $qemuDroppedBytes += $f.Length }
            }
            $qemuIncluded = $true
            Write-Host ("[5/7] QEMU 精简白名单已加入：{0} 个文件，{1}（丢掉 {2} 个 / {3}）" -f `
                $qemuKeptCount, (Format-Size $qemuKeptBytes), $qemuDroppedCount, (Format-Size $qemuDroppedBytes)) -ForegroundColor Green
        }
        $qemuIncluded = $true
    } else {
        Write-Host '[5/7] 跳过 QEMU（未指定 -IncludeQemu/-Lean/-Fat）' -ForegroundColor DarkGray
    }

    # --- 6. 镜像（可选）---------------------------------------------------
    $isoFound = @()
    $sbIncluded = $false
    $platIncluded = $false
    $sbBytes = [long]0
    $sbNeed = @('rootfs.img', 'vmlinuz', 'initrd.img', 'workspace-blank.qcow2')
    if ($IncludeSandbox) {
        $sandboxSrc = Join-Path $RepoRoot 'var\sandbox'
        $sbDst = Join-Path $script:Staging 'var\sandbox'
        New-Item -ItemType Directory -Force -Path $sbDst | Out-Null
        foreach ($n in $sbNeed) {
            $p = Join-Path $sandboxSrc $n
            if (-not (Test-Path -LiteralPath $p)) {
                Die "缺少沙箱镜像 var\sandbox\$n" '先在平台 VM 里跑 deploy/sandbox/build-sandbox-image.sh，或 deploy\windows\fetch-sandbox-image.ps1'
            }
            Copy-Item -LiteralPath $p -Destination (Join-Path $sbDst $n) -Force
            $sbBytes += (Get-Item -LiteralPath $p).Length
        }
        $sbIncluded = $true
        $sbExtra = @(Get-ChildItem -LiteralPath $sandboxSrc -Force -ErrorAction SilentlyContinue |
                     Where-Object { $sbNeed -notcontains $_.Name })
        if ($sbExtra.Count -gt 0) {
            Write-Host ("      跳过 var\sandbox\ 里 {0} 项运行期垃圾（sessions/console/smoke/tmp）" -f $sbExtra.Count) -ForegroundColor DarkGray
        }
    } else {
        Write-Host '      沙箱镜像：未包含' -ForegroundColor DarkGray
    }

    if ($IncludePlatform) {
        $platDisk = Join-Path $RepoRoot 'var\platform\platform.qcow2'
        if (-not (Test-Path -LiteralPath $platDisk)) {
            Die "缺少平台磁盘 $platDisk" '先跑 deploy\windows\fetch-platform-image.ps1 + provision-cloud-vm.ps1 建平台 VM'
        }
        # 磁盘正在被 QEMU 写的时候拷贝，得到的 qcow2 很可能是不一致的（打出来的包会坏）
        $using = @(Get-CimInstance Win32_Process -Filter "Name='qemu-system-x86_64.exe'" -ErrorAction SilentlyContinue |
                   Where-Object { $_.CommandLine -and $_.CommandLine -like "*$platDisk*" })
        if ($using.Count -gt 0) {
            Die ("平台磁盘正在被 QEMU（pid " + ($using | ForEach-Object { $_.ProcessId }) -join ',' + "）使用，拒绝拷贝") '先关掉平台 VM（deploy\windows\stop-agent.ps1 或 stop-agent.ps1），再打包'
        }
        $platDst = Join-Path $script:Staging 'var\platform'
        New-Item -ItemType Directory -Force -Path $platDst | Out-Null
        Copy-Item -LiteralPath $platDisk -Destination (Join-Path $platDst 'platform.qcow2') -Force
        $platIncluded = $true
        Write-Host '      平台磁盘已复制。注意两点：① 它很大（10.6 GB）；② 磁盘里的 SSH 授权公钥是' -ForegroundColor Yellow
        Write-Host '      原机器的 var\vm_key.pub，而私钥永不进包 —— 要么手工带私钥，要么在目标机器重建平台 VM。' -ForegroundColor Yellow
    }
    if ($IncludeIso) {
        $isoFound = @(Get-ChildItem -LiteralPath $RepoRoot -Filter '*.iso' -File -ErrorAction SilentlyContinue)
        if ($isoFound.Count -eq 0) {
            Die '仓库根目录没有 *.iso（Debian 安装盘）' '下载 debian-13.x-amd64-netinst.iso 放到仓库根，或去掉 -IncludeIso'
        }
        foreach ($iso in $isoFound) {
            Copy-Item -LiteralPath $iso.FullName -Destination (Join-Path $script:Staging $iso.Name) -Force
        }
    }
    Write-Host ("[6/7] 镜像：沙箱 {0}；平台磁盘 {1}；ISO {2} 个" -f `
        $(if ($sbIncluded) { '已包含' } else { '未包含' }), `
        $(if ($platIncluded) { '已包含' } else { '未包含' }), $isoFound.Count) -ForegroundColor Green

    # --- 7. BUNDLE-README + MANIFEST --------------------------------------
    $pyVerText = '(未生成 wheelhouse)'
    if ($script:Py) { $pyVerText = $script:Py.Ver }
    # 下面这些值都在 here-string 外面先算好：here-string 里只用 $var 插值，
    # 避免 $() 里再套 $() 让 Windows PowerShell 5.1 的解析器误判括号。
    $presetText = '自定义'
    if ($preset -eq 'lean') { $presetText = '-Lean（推荐）' }
    elseif ($preset -eq 'fat') { $presetText = '-Fat（全量）' }
    if ($IncludeWheelhouse) { $whText = "已包含（$($whWhl.Count) 个 wheel，Python $pyVerText，Windows x64）" }
    else { $whText = '未包含（安装 .venv 需要联网）' }
    if ($qemuIncluded) {
        if ($FullQemu) { $qemuText = '已包含（全量）' } else { $qemuText = '已包含（精简白名单）' }
    } else { $qemuText = '未包含（需要自己准备）' }
    if ($sbIncluded) { $sbText = '已包含（4 个文件）' } else { $sbText = '未包含（必须在平台 VM 里重建）' }
    if ($platIncluded) { $platText = '已包含（但私钥要自己带）' } else { $platText = '未包含（按第 1 步重建）' }
    if ($IncludeWheelhouse) { $whBullet = "* wheelhouse\ —— 离线 pip 依赖（$($whWhl.Count) 个 wheel）" }
    else { $whBullet = '* （没有 wheelhouse\：装依赖需要联网）' }
    if ($qemuIncluded) {
        if ($FullQemu) { $qemuBullet = '* qemu\ —— QEMU for Windows（全量）' }
        else { $qemuBullet = '* qemu\ —— QEMU for Windows（精简白名单：x86_64 exe + 全部 DLL + x86 固件）' }
    } else { $qemuBullet = '* （没有 qemu\：自己准备 QEMU for Windows）' }
    if ($sbIncluded) { $sbBullet = '* var\sandbox\ —— 沙箱镜像 4 个文件' } else { $sbBullet = '* （没有沙箱镜像：必须在平台 VM 里重建）' }
    if ($platIncluded) { $platBullet = '* var\platform\platform.qcow2 —— 平台磁盘（含 VM 内模型权重）' } else { $platBullet = '* （没有平台磁盘：按第 1 步重建）' }
    if ($isoFound.Count -gt 0) { $isoBullet = '* ' + (($isoFound | ForEach-Object { $_.Name }) -join ', ') + ' —— Debian 安装 ISO' }
    else { $isoBullet = '* （没有 ISO）' }
    if ($platIncluded) { $modelsText = '带了磁盘 = 间接带了权重' } else { $modelsText = '没带磁盘 = 权重按第 2 步自己下' }
    $platNote = ''
    if ($platIncluded) {
        $platNote = @"
## 注意：这个包里带了平台磁盘（platform.qcow2）

磁盘里的 `ssh_authorized_keys` 是**制作那份磁盘时**注入的公钥（原机器的 `var\vm_key.pub`）。
SSH 私钥属于凭据、**永远不会进包**，所以你必须二选一：

* 把原机器的 `var\vm_key` + `var\vm_key.pub` 手工拷到 `<仓库>\var\`（最省事）；
* 或者按下面第 1 步重建平台 VM（会顺带生成全新的 SSH 密钥）。

"@
    } else {
        $platNote = @"
## 注意：这个包里没有平台磁盘

要按下面第 1 步重建平台 VM（走 Debian 官方云镜像 + cloud-init，几分钟，不需要 ISO）。
这一步会顺带在 `var\vm_key` 生成全新的 SSH 密钥，所以**不需要**从别的机器传私钥。

"@
    }
    $bundleReadme = @"
# agentbox 离线安装包（$bundleName）

生成时间 : $packagedAt
生成机器 : $env:COMPUTERNAME  /  PowerShell $($PSVersionTable.PSVersion)
预设     : $presetText
版本     : $Version

## 这份包对应哪份源码（出问题请拿这个 commit 复现）

    commit   : $commit
    describe : $describe
    branch   : $branch
    工作树   : $(if ($isDirty) { "脏（-AllowDirty 打的快照，不对应任何 commit！）" } else { '干净（源码 = git 提交态）' })
    打包时间 : $packagedAt

完整信息见包根目录的 ``SOURCE-COMMIT.txt``；``MANIFEST.sha256`` 头部也有同样的 commit。

wheelhouse: $whText
QEMU     : $qemuText
沙箱镜像 : $sbText
平台磁盘 : $platText

$platNote## 解压 + 一键安装（照抄即可）

    Expand-Archive .\$bundleName.zip -DestinationPath C:\agentbox
    cd C:\agentbox
    powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Check
    powershell -ExecutionPolicy Bypass -File .\packaging\setup-agentbox.ps1 -Yes

``setup-agentbox.ps1`` 是顶层入口：预检 -> 建 .venv 装依赖 -> 生成 .env -> 重建/复用平台 VM
-> 下模型 -> 起服务自检 -> 给"已就绪/未就绪"。所有步骤幂等，可以反复跑。
只想要 Windows 侧（不碰 VM、不下模型）：``-SkipVm -SkipModels``；
只想手动分步：``powershell -ExecutionPolicy Bypass -File .\packaging\install.ps1``。

## 然后三步（每条都能直接粘贴）

### 1) 准备平台 VM（AI 服务 + PostgreSQL 跑在里面）

    powershell -ExecutionPolicy Bypass -File .\deploy\windows\fetch-platform-image.ps1
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\provision-cloud-vm.ps1 -Follow

成功标志：串口日志出现 ``agentbox provisioning OK``，并且 VM 内存在 ``/opt/agentbox/PROVISIONED``。
以后重启这台 VM 用：

    powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-platform-vm.ps1

（这条路径需要安装期联网：apt 源 + pip。备选路径是用 ISO 全自动安装，
 需要 debian-*-netinst.iso —— 它只在 ``-Fat`` 包里，或自己下载放到仓库根：
 ``powershell -ExecutionPolicy Bypass -File .\deploy\windows\new-platform-vm.ps1 -Follow``）

### 2) 在平台 VM 里把模型权重下下来（走 hf-mirror，约 5 GB；不下也能跑，只是降级）

    ssh -i var\vm_key -p 2222 -o StrictHostKeyChecking=no agent@127.0.0.1
    cd /opt/agentbox/app
    export HF_HOME=/opt/agentbox/models HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1
    .venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"
    .venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
    sudo systemctl restart agentbox-ai

要点：一定带上 ``HF_ENDPOINT=https://hf-mirror.com``（否则连不上 huggingface.co，每个文件重试 5 次）
和 ``HF_HUB_DISABLE_XET=1``；**不要**给 faster-whisper 传 ``download_root=``，让它认 ``HF_HOME``。
whisper 的名字跟着 ``.env`` 里的 ``AGENT_ASR_MODEL`` 走（默认 ``base``，这台机器上用的是 ``small``）。
不下 bge-m3 的话，工具检索会自动降级成关键词匹配；不下 whisper 只是语音功能不可用。

### 3) 填 API key，然后启动

    notepad .env
    #   把 AGENT_LLM_API_KEY=sk-replace-me 改成你自己的 DeepSeek key

    powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1

最后复检：

    powershell -ExecutionPolicy Bypass -File .\packaging\install.ps1 -Check

## 包里有什么

* 源码 + 脚本（来自 git ls-files）、deploy\、packaging\（安装器 + 说明）
$whBullet
$qemuBullet
$sbBullet
$platBullet
$isoBullet
* MANIFEST.sha256 —— 每个文件的 SHA256

## 包里没有（离线覆盖不到的部分）

* Python 3.13+ 本体（wheelhouse 里的 wheel 绑定生成时的 Python minor 版本；本包是 $pyVerText）
* var\vm_key / var\vm_key.pub（SSH 私钥，属凭据，永不进包）
* 平台 VM 里的模型权重 /opt/agentbox/models（~4.8 GB，只在 platform.qcow2 内部；本包 $modelsText）
* 平台 VM 的安装期联网（apt + pip 走镜像源；这一步无法离线，除非你带了 qcow2）

## 校验

    # 逐行核对 sha256（PowerShell）
    Get-Content .\MANIFEST.sha256 | ForEach-Object {
        if (`$_ -match '^([0-9a-f]{64})  (.+)$') {
            `$h = (Get-FileHash -Algorithm SHA256 -LiteralPath `$Matches[2]).Hash.ToLower()
            if (`$h -ne `$Matches[1]) { "BAD  " + `$Matches[2] }
        }
    }
    # 或 Linux（跳过 # 开头的注释行，避免 sha256sum 报 improperly formatted）:
    #   grep -v '^#' MANIFEST.sha256 | sha256sum -c -
"@
    [System.IO.File]::WriteAllText((Join-Path $script:Staging 'BUNDLE-README.md'), $bundleReadme, (New-Object System.Text.UTF8Encoding($true)))

    # --- 7b. SOURCE-COMMIT.txt：包和 commit 的绑定关系 ---------------------
    $srcCommitLines = New-Object System.Collections.ArrayList
    [void]$srcCommitLines.Add("bundle      : $bundleName.zip")
    [void]$srcCommitLines.Add("commit      : $commit")
    [void]$srcCommitLines.Add("describe    : $describe")
    [void]$srcCommitLines.Add("branch      : $branch")
    [void]$srcCommitLines.Add("packaged_at : $packagedAt")
    [void]$srcCommitLines.Add("packaged_on : $env:COMPUTERNAME / PowerShell $($PSVersionTable.PSVersion)")
    [void]$srcCommitLines.Add("generator   : packaging\make-bundle.ps1 (preset $preset)")
    [void]$srcCommitLines.Add("wheelhouse  : $(if ($IncludeWheelhouse) { "yes, Python $pyVerText, win_amd64" } else { 'no' })")
    [void]$srcCommitLines.Add("worktree    : $(if ($isDirty) { 'DIRTY (-AllowDirty: this bundle matches no commit)' } else { 'clean (source == the git commit above)' })")
    if ($isDirty) {
        [void]$srcCommitLines.Add("dirty_tracked          : $($trackedDirty.Count)")
        [void]$srcCommitLines.Add("dirty_untracked_source : $($untrackedSource.Count)")
        foreach ($l in $trackedDirty) { [void]$srcCommitLines.Add("  tracked   $l") }
        foreach ($l in $untrackedSource) { [void]$srcCommitLines.Add("  untracked $l") }
    }
    if ($untrackedExtra.Count -gt 0) {
        [void]$srcCommitLines.Add("untracked_extras (packaging/dist etc; do not affect the source<->commit mapping): $($untrackedExtra.Count)")
        foreach ($l in ($untrackedExtra | Select-Object -First 20)) { [void]$srcCommitLines.Add("  extra     $l") }
    }
    # 给人看的文本：带 BOM 写，否则 Windows 上按 GBK 读会乱码
    [System.IO.File]::WriteAllText((Join-Path $script:Staging 'SOURCE-COMMIT.txt'), (($srcCommitLines -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding($true)))

    $allFiles = @(Get-ChildItem -LiteralPath $script:Staging -Recurse -Force -File -ErrorAction SilentlyContinue)
    $manifest = New-Object System.Collections.ArrayList
    # 头部注释：commit 也写在 MANIFEST 里（sha256sum -c 请先 grep -v '^#'）
    [void]$manifest.Add("# agentbox bundle manifest")
    [void]$manifest.Add("# bundle: $bundleName.zip")
    [void]$manifest.Add("# commit: $commit")
    [void]$manifest.Add("# describe: $describe")
    [void]$manifest.Add("# branch: $branch")
    [void]$manifest.Add("# packaged_at: $packagedAt")
    [void]$manifest.Add("# worktree: $(if ($isDirty) { 'DIRTY' } else { 'clean' })")
    [void]$manifest.Add("# verify (linux): grep -v '^#' MANIFEST.sha256 | sha256sum -c -")
    $manifestCount = 0
    foreach ($f in $allFiles) {
        $rel = $f.FullName.Substring($script:Staging.Length + 1) -replace '\\', '/'
        $hash = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash.ToLower()
        [void]$manifest.Add("$hash  $rel")
        $manifestCount++
    }
    $manifestPath = Join-Path $script:Staging 'MANIFEST.sha256'
    [System.IO.File]::WriteAllText($manifestPath, (($manifest -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
    Write-Host ("[7/7] SOURCE-COMMIT.txt + MANIFEST.sha256: {0} 个文件（不含 MANIFEST 自身，commit {1}）" -f $manifestCount, $commit.Substring(0, 12)) -ForegroundColor Green

    if ($DryRun) {
        Write-Host ''
        Write-Host '--- DRY-RUN：不产出 zip，staging 留在原地供检查 -------------' -ForegroundColor Yellow
        Write-Host ("  staging : $script:Staging")
        Write-Host ("  文件数  : {0}" -f @(Get-ChildItem -LiteralPath $script:Staging -Recurse -Force -File).Count)
        Write-Host '  SOURCE-COMMIT.txt 内容：'
        Get-Content -LiteralPath (Join-Path $script:Staging 'SOURCE-COMMIT.txt') -Encoding UTF8 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        Write-Host '  MANIFEST.sha256 头部：'
        Get-Content -LiteralPath $manifestPath -Encoding UTF8 -TotalCount 8 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        Write-Host ''
        Write-Host '✔ DRY-RUN 结束（没有 zip 产出；staging 目录请自行删除）' -ForegroundColor Yellow
        $script:KeepStaging = $true
        return
    }

    # --- 8. 打 zip -------------------------------------------------------
    # 两个都要显式加载：ZipArchiveMode/ZipArchive 在 System.IO.Compression.dll 里，
    # ZipFile/ZipFileExtensions 在 System.IO.Compression.FileSystem.dll 里，
    # .NET Framework 下只加载后者不会把前者的类型带进来。
    Add-Type -AssemblyName System.IO.Compression | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null
    $noCompress = @('.img', '.qcow2', '.iso', '.vmdk', '.raw', '.whl', '.gz', '.xz', '.zip', '.7z')
    $zipFiles = @(Get-ChildItem -LiteralPath $script:Staging -Recurse -Force -File | Sort-Object FullName)
    Write-Host ("正在写 zip（{0} 个文件，{1}）…" -f $zipFiles.Count, (Format-Size (($zipFiles | Measure-Object -Property Length -Sum).Sum))) -ForegroundColor Cyan
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
    $zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($f in $zipFiles) {
            $rel = $f.FullName.Substring($script:Staging.Length + 1) -replace '\\', '/'
            $ext = [System.IO.Path]::GetExtension($f.Name).ToLower()
            $level = [System.IO.Compression.CompressionLevel]::Optimal
            if ($noCompress -contains $ext) { $level = [System.IO.Compression.CompressionLevel]::NoCompression }
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $f.FullName, $rel, $level)
            if ($f.Length -ge 100MB) { Write-Host ("      + {0}  ({1})" -f $rel, (Format-Size $f.Length)) -ForegroundColor DarkGray }
        }
    } finally {
        $zip.Dispose()
    }
    $zipItem = Get-Item -LiteralPath $zipPath
    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
    [System.IO.File]::WriteAllText("$zipPath.sha256", ("$zipHash  $bundleName.zip`r`n"), (New-Object System.Text.UTF8Encoding($false)))

    # --- 9. 报告 ---------------------------------------------------------
    $sbTotal = [long]0
    foreach ($n in $sbNeed) {
        $s = Get-PathSize (Join-Path $RepoRoot "var\sandbox\$n")
        if ($s -gt 0) { $sbTotal += $s }
    }
    $qemuFullBytes = Get-PathSize $qemuSrc
    $isoBytes = [long]0
    foreach ($iso in @(Get-ChildItem -LiteralPath $RepoRoot -Filter '*.iso' -File -ErrorAction SilentlyContinue)) { $isoBytes += $iso.Length }
    $whSrcBytes = Get-PathSize $whSrc
    $platBytes = Get-PathSize (Join-Path $RepoRoot 'var\platform\platform.qcow2')

    $qemuCell = '否（需手工）'
    if ($qemuIncluded) {
        if ($FullQemu) { $qemuCell = '是（全量）' } else { $qemuCell = ('是（精简白名单 {0}）' -f (Format-Size $qemuKeptBytes)) }
    }
    $whLabel = 'wheelhouse\ (离线 pip 依赖)'
    if ($IncludeWheelhouse) { $whLabel = "wheelhouse\ (离线 pip 依赖, Python $pyVerText)" }
    $rows = @(
        [pscustomobject]@{ 项目 = '源码 + 脚本 (git ls-files)'; 大小 = (Format-Size $srcBytes); 进包 = '是' }
        [pscustomobject]@{ 项目 = 'deploy\ 部署脚本'; 大小 = (Format-Size (Get-PathSize $deployDst)); 进包 = '是' }
        [pscustomobject]@{ 项目 = 'packaging\ 安装器 + 一键脚本 + 文档'; 大小 = (Format-Size (Get-PathSize $pkgDst)); 进包 = '是' }
        [pscustomobject]@{ 项目 = $whLabel; 大小 = (Format-Size $whSrcBytes); 进包 = $(if ($IncludeWheelhouse) { '是' } else { '否（-IncludeWheelhouse）' }) }
        [pscustomobject]@{ 项目 = "QEMU for Windows (全部 $([math]::Round($qemuFullBytes/1MB)) MB)"; 大小 = (Format-Size $qemuFullBytes); 进包 = $qemuCell }
        [pscustomobject]@{ 项目 = '沙箱镜像 var\sandbox\ (4 个文件)'; 大小 = (Format-Size $sbTotal); 进包 = $(if ($sbIncluded) { '是' } else { '否（-IncludeSandbox/-Lean/-Fat）' }) }
        [pscustomobject]@{ 项目 = '平台磁盘 var\platform\platform.qcow2'; 大小 = (Format-Size $platBytes); 进包 = $(if ($platIncluded) { '是' } else { '否（-IncludePlatform/-Fat）' }) }
        [pscustomobject]@{ 项目 = 'Debian 安装 ISO'; 大小 = (Format-Size $isoBytes); 进包 = $(if ($isoFound.Count -gt 0) { '是' } else { '否（-IncludeIso/-Fat）' }) }
        [pscustomobject]@{ 项目 = '模型权重 /opt/agentbox/models (~4.8 GB)'; 大小 = '在平台磁盘内'; 进包 = $(if ($platIncluded) { '间接（随 platform.qcow2）' } else { '否' }) }
        [pscustomobject]@{ 项目 = '.env / var\vm_key (密钥)'; 大小 = '—'; 进包 = '否（安全规则，强制排除）' }
        [pscustomobject]@{ 项目 = 'MANIFEST.sha256 + BUNDLE-README.md'; 大小 = (Format-Size ((Get-PathSize $manifestPath) + (Get-PathSize (Join-Path $script:Staging 'BUNDLE-README.md')))); 进包 = '是' }
    )

    Write-Host ''
    Write-Host '--- 打包清单 ---' -ForegroundColor Cyan
    $rows | Format-Table -AutoSize | Out-String -Width 200 | Write-Host
    if ($skipped.Count -gt 0) {
        Write-Host '--- 因密钥规则被跳过的文件 ---' -ForegroundColor Yellow
        $skipped | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
    } else {
        Write-Host '密钥规则：没有 tracked 文件被跳过（.env 本来就不在 git 里）' -ForegroundColor DarkGray
    }
    if ($qemuIncluded -and -not $FullQemu) {
        Write-Host ''
        Write-Host ("精简 QEMU 白名单：保留 {0} 个文件 / {1}；丢弃 {2} 个文件 / {3}" -f `
            $qemuKeptCount, (Format-Size $qemuKeptBytes), $qemuDroppedCount, (Format-Size $qemuDroppedBytes)) -ForegroundColor DarkGray
        Write-Host '  （丢弃的是别的架构的 system exe、share\ 里 arm/riscv/loongarch 固件、doc\ + icons\）' -ForegroundColor DarkGray
    }
    Write-Host ''
    Write-Host '✔ 打包完成' -ForegroundColor Green
    Write-Host "  zip    : $zipPath"
    Write-Host ("  大小   : {0}" -f (Format-Size $zipItem.Length))
    Write-Host "  文件数 : $(@(Get-ChildItem -LiteralPath $script:Staging -Recurse -Force -File).Count)（含 MANIFEST/BUNDLE-README/SOURCE-COMMIT）"
    Write-Host "  源码   : $describe"
    Write-Host "  commit : $commit"
    Write-Host "  工作树 : $(if ($isDirty) { '脏（-AllowDirty 快照，不对应 commit）' } else { '干净（= git 提交态）' })"
    Write-Host "  SHA256 : $zipHash"
    Write-Host "  校验   : $zipPath.sha256"
    Write-Host ''
    Write-Host '  说明：磁盘镜像/ISO/whl 用 NoCompression 存进 zip，所以 zip 体积≈原体积；' -ForegroundColor DarkGray
    Write-Host '        源码与文档用 Optimal 压缩。要更小就在 VM 里清缓存 + qemu-img convert -c 压 qcow2。' -ForegroundColor DarkGray
    Write-Host ''
} catch {
    Write-Host ''
    Write-Host "✘ 打包失败: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
} finally {
    if ($script:Staging -and (Test-Path -LiteralPath $script:Staging)) {
        if ($script:KeepStaging) {
            Write-Host ("（DRY-RUN）staging 保留在: {0}" -f $script:Staging) -ForegroundColor Yellow
        } else {
            Remove-Item -LiteralPath $script:Staging -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
exit $exitCode
