<#
.SYNOPSIS
    把 agentbox 打成一个可以搬到另一台 Windows 机器上的 zip（离线安装包）。

.DESCRIPTION
    文件清单以 `git ls-files` 为准，所以天然不含 var\、.env、.venv\、qemu\、*.iso；
    再补上 deploy\、packaging\install.ps1、BUNDLE-README.md 和 MANIFEST.sha256。

    永远不进包（会打印被跳过的文件名）：
      .env / .env.*（密钥）、var\vm_key（SSH 私钥）、*.pem / *.key / id_rsa*、
      以及任何文件内容里出现"真实"密钥赋值（AGENT_LLM_API_KEY=sk-xxxx…）。

    -IncludeWheelhouse  先把 Python 依赖下载到 packaging\wheelhouse\，一起打进包里，
                        目标机器就能完全离线建 .venv（+ 约 100~300 MB）。
    -IncludeImages      把 var\sandbox\ 的 4 个镜像文件、var\platform\platform.qcow2
                        和仓库根目录的 Debian 安装 ISO 一起打进去（"胖包"，约 +14 GB）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\packaging\make-bundle.ps1
    powershell -ExecutionPolicy Bypass -File .\packaging\make-bundle.ps1 -IncludeWheelhouse -Force
    powershell -ExecutionPolicy Bypass -File .\packaging\make-bundle.ps1 -IncludeWheelhouse -IncludeImages -OutDir D:\ship
#>
[CmdletBinding()]
param(
    # 把 packaging\wheelhouse\ 也打进包（离线建 venv 用）
    [switch]$IncludeWheelhouse,
    # 把沙箱镜像 / 平台磁盘 / Debian ISO 也打进包（多 GB）
    [switch]$IncludeImages,
    # 顺带下载的可选 extras（逗号分隔，例如 'voice,asr'）；默认只带基础依赖 + 开发工具
    [string]$WheelExtras = '',
    # 输出目录，默认 <仓库>\dist
    [string]$OutDir,
    # 已存在同名 zip 时覆盖
    [switch]$Force,
    # 覆盖版本号（默认取 pyproject.toml 的 project.version）
    [string]$Version,
    # 覆盖仓库根目录（默认脚本的上一级）
    [string]$RepoRoot
)

$ErrorActionPreference = 'Stop'

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
        # sk-replace-me / <与平台 VM 相同> 之类
        if ($val -match '(?i)replace|your-|yourkey|<.*>') { continue }
        return $true
    }
    return $false
}

function Resolve-Python {
    $cands = New-Object System.Collections.ArrayList
    $venv = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { [void]$cands.Add(@($venv)) }
    $py = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($py) { [void]$cands.Add(@($py.Source, '-3')) }
    $p = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($p) { [void]$cands.Add(@($p.Source)) }
    foreach ($c in $cands) {
        $exe = $c[0]
        $pre = @()
        if ($c.Count -gt 1) { $pre = @($c[1..($c.Count - 1)]) }
        try {
            $v = & $exe @pre -c "import sys;print('%d.%d'%(sys.version_info[0],sys.version_info[1]))" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$v" -match '^\d+\.\d+$') {
                return [pscustomobject]@{ Exe = $exe; Pre = $pre; Ver = "$v" }
            }
        } catch { }
    }
    return $null
}

function Get-RequirementList([string]$pyproject, [string[]]$extras) {
    $code = @'
import json, sys, tomllib
with open(sys.argv[1], "rb") as fh:
    data = tomllib.load(fh)
reqs = list(data["project"]["dependencies"])
opt = data["project"].get("optional-dependencies", {})
for name in sys.argv[2:]:
    if name not in opt:
        raise SystemExit("unknown extra: " + name)
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
$exitCode = 0

try {
    if (-not $RepoRoot) { $RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path }
    $RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'pyproject.toml'))) {
        Die "看起来不是 agentbox 仓库: $RepoRoot（没有 pyproject.toml）" '用 -RepoRoot 指定仓库根目录'
    }

    Write-Host ''
    Write-Host '=== agentbox 打包 ===' -ForegroundColor Cyan
    Write-Host "仓库: $RepoRoot"

    # --- 0. git 清单 ---------------------------------------------------
    $git = Get-Command 'git.exe' -ErrorAction SilentlyContinue
    if (-not $git) { Die '找不到 git.exe' '安装 Git for Windows 后重试（打包依赖 git ls-files 来取源码清单）' }
    $inside = (& git -C $RepoRoot rev-parse --is-inside-work-tree 2>$null)
    if ("$inside".Trim() -ne 'true') { Die "$RepoRoot 不是 git 工作区" '在仓库里执行 git init/commit 后重试' }
    $tracked = @(& git -C $RepoRoot ls-files)
    if ($LASTEXITCODE -ne 0) { Die 'git ls-files 失败' '检查 .git 是否损坏' }
    if ($tracked.Count -lt 20) { Die "git ls-files 只返回 $($tracked.Count) 个文件，太少" '确认已经 commit 了源码快照（打包只收 tracked 文件）' }

    # --- 1. 版本 / 输出路径 --------------------------------------------
    if (-not $Version) {
        $pj = Get-Content -LiteralPath (Join-Path $RepoRoot 'pyproject.toml') -Raw -Encoding UTF8
        $m = [regex]::Match($pj, '(?m)^\s*version\s*=\s*"([^"]+)"')
        if (-not $m.Success) { Die 'pyproject.toml 里找不到 project.version' '用 -Version 指定版本号' }
        $Version = $m.Groups[1].Value
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmm'
    $bundleName = "agentbox-$Version-$stamp"
    if (-not $OutDir) { $OutDir = Join-Path $RepoRoot 'dist' }
    elseif (-not [System.IO.Path]::IsPathRooted($OutDir)) { $OutDir = Join-Path $RepoRoot $OutDir }
    $OutDir = [System.IO.Path]::GetFullPath($OutDir)
    if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
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
    Write-Host ("[1/6] 源码 {0} 个文件，{1}" -f $copied, (Format-Size $srcBytes)) -ForegroundColor Green

    # deploy\ 必须完整（tracked 之外若有文件也提示出来，别装作打进去了）
    $deployDst = Join-Path $script:Staging 'deploy'
    $deployFiles = @(Get-ChildItem -LiteralPath $deployDst -Recurse -Force -File -ErrorAction SilentlyContinue)
    if ($deployFiles.Count -eq 0) { Die 'deploy\ 没进包' '确认 deploy\ 已被 git 跟踪' }
    Write-Host ("[2/6] deploy\ {0} 个文件" -f $deployFiles.Count) -ForegroundColor Green
    $untrackedDeploy = @(& git -C $RepoRoot status --porcelain --untracked-files=all -- deploy)
    if ($untrackedDeploy.Count -gt 0) {
        Write-Host '      注意：deploy\ 下有未跟踪/未提交的改动，这些内容不会进包：' -ForegroundColor Yellow
        $untrackedDeploy | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkYellow }
    }

    # --- 3. packaging\install.ps1 + BUNDLE-README -------------------------
    $installSrc = Join-Path $RepoRoot 'packaging\install.ps1'
    if (-not (Test-Path -LiteralPath $installSrc)) { Die "缺少 $installSrc" '打包必须带上安装器' }
    $pkgDst = Join-Path $script:Staging 'packaging'
    New-Item -ItemType Directory -Force -Path $pkgDst | Out-Null
    foreach ($f in @('install.ps1', 'make-bundle.ps1', 'README.md')) {
        $p = Join-Path $RepoRoot "packaging\$f"
        if (Test-Path -LiteralPath $p) { Copy-Item -LiteralPath $p -Destination (Join-Path $pkgDst $f) -Force }
    }
    Write-Host '[3/6] 安装器 packaging\install.ps1 已加入' -ForegroundColor Green

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
        # 离线建 venv 还需要构建后端，以及跑测试要用的开发工具
        $reqs = @($reqs) + @('hatchling>=1.24', 'pip', 'setuptools', 'wheel', 'pytest>=8.2', 'pytest-asyncio>=0.23', 'ruff>=0.5')
        $reqs = @($reqs | Select-Object -Unique)
        Write-Host ("[4/6] pip download -> packaging\wheelhouse  (Python {0}, {1} 个依赖)" -f $script:Py.Ver, $reqs.Count) -ForegroundColor Green
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
        Write-Host "[4/6] 跳过 wheelhouse$whMissing" -ForegroundColor DarkGray
    }

    # --- 5. 镜像（可选）---------------------------------------------------
    $imgIncluded = @{}
    $isoFound = @()
    if ($IncludeImages) {
        $sandboxSrc = Join-Path $RepoRoot 'var\sandbox'
        $sbNeed = @('rootfs.img', 'vmlinuz', 'initrd.img', 'workspace-blank.qcow2')
        $sbDst = Join-Path $script:Staging 'var\sandbox'
        New-Item -ItemType Directory -Force -Path $sbDst | Out-Null
        foreach ($n in $sbNeed) {
            $p = Join-Path $sandboxSrc $n
            if (-not (Test-Path -LiteralPath $p)) {
                Die "缺少沙箱镜像 var\sandbox\$n" '先在平台 VM 里跑 deploy/sandbox/build-sandbox-image.sh，或 deploy\windows\fetch-sandbox-image.ps1'
            }
            Copy-Item -LiteralPath $p -Destination (Join-Path $sbDst $n) -Force
            $imgIncluded[$n] = $true
        }
        $sbExtra = @(Get-ChildItem -LiteralPath $sandboxSrc -Force -ErrorAction SilentlyContinue |
                     Where-Object { $sbNeed -notcontains $_.Name })
        if ($sbExtra.Count -gt 0) {
            Write-Host ("      跳过 var\sandbox\ 里 {0} 项运行期垃圾（sessions/console/smoke/tmp）" -f $sbExtra.Count) -ForegroundColor DarkGray
        }

        $platDisk = Join-Path $RepoRoot 'var\platform\platform.qcow2'
        if (-not (Test-Path -LiteralPath $platDisk)) {
            Die "缺少平台磁盘 $platDisk" '先跑 deploy\windows\provision-cloud-vm.ps1 建平台 VM'
        }
        $platDst = Join-Path $script:Staging 'var\platform'
        New-Item -ItemType Directory -Force -Path $platDst | Out-Null
        Copy-Item -LiteralPath $platDisk -Destination (Join-Path $platDst 'platform.qcow2') -Force
        Write-Host '      平台磁盘已复制（注意：磁盘里的 SSH 授权公钥是原机器的 var\vm_key.pub）' -ForegroundColor Yellow

        $isoFound = @(Get-ChildItem -LiteralPath $RepoRoot -Filter '*.iso' -File -ErrorAction SilentlyContinue)
        if ($isoFound.Count -eq 0) {
            Die '仓库根目录没有 *.iso（Debian 安装盘）' '下载 debian-13.x-amd64-netinst.iso 放到仓库根，或去掉 -IncludeImages'
        }
        foreach ($iso in $isoFound) {
            Copy-Item -LiteralPath $iso.FullName -Destination (Join-Path $script:Staging $iso.Name) -Force
        }
        Write-Host ("[5/6] 镜像已加入：沙箱 4 个 + 平台磁盘 + {0} 个 ISO" -f $isoFound.Count) -ForegroundColor Green
    } else {
        Write-Host '[5/6] 跳过镜像（未指定 -IncludeImages）' -ForegroundColor DarkGray
    }

    # --- 6. BUNDLE-README + MANIFEST --------------------------------------
    $bundleReadme = @"
# agentbox 离线安装包（$bundleName）

生成时间 : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
生成机器 : $env:COMPUTERNAME  /  PowerShell $($PSVersionTable.PSVersion)
版本     : $Version
wheelhouse: $(if ($IncludeWheelhouse) { "已包含（Python $($script:Py.Ver)，Windows x64）" } else { '未包含（安装 .venv 需要联网）' })
镜像     : $(if ($IncludeImages) { '已包含（沙箱镜像 + 平台磁盘 + Debian ISO）' } else { '未包含（需要自己准备 QEMU / 镜像）' })

## 怎么用

    Expand-Archive .\$bundleName.zip -DestinationPath C:\agentbox
    cd C:\agentbox
    powershell -ExecutionPolicy Bypass -File .\packaging\install.ps1
    powershell -ExecutionPolicy Bypass -File .\packaging\install.ps1 -Check   # 只看状态，不改任何东西

装完按 install.ps1 打印的"下一步"做：把 DeepSeek API key 填进 .env，准备 QEMU 与镜像，
然后 powershell -ExecutionPolicy Bypass -File .\deploy\windows\start-agent.ps1

## 包里有什么

* 源码 + 脚本（来自 git ls-files）、deploy\、packaging\（安装器 + 说明）
$(if ($IncludeWheelhouse) { '* wheelhouse\ —— 离线 pip 依赖（' + $whWhl.Count + ' 个 wheel）' } else { '* （没有 wheelhouse\）' })
$(if ($IncludeImages) { '* var\sandbox\ 4 个镜像文件、var\platform\platform.qcow2、Debian 安装 ISO' } else { '* （没有 VM 镜像 / ISO）' })
* MANIFEST.sha256 —— 每个文件的 SHA256

## 包里没有（离线覆盖不到的部分）

* Python 3.13+ 本体（要自己装；wheelhouse 里的轮子绑定生成时的 Python 版本）
* QEMU for Windows（qemu\ 约 1.2 GB，从未打进包）：到 https://qemu.weilnetz.de/w64/ 装一份，
  或从旧机器 robocopy qemu\ 整个目录过来，再设 AGENT_QEMU_DIR 或放到 <仓库>\qemu
* var\vm_key / var\vm_key.pub（SSH 私钥，属凭据，永不进包）
* 平台 VM 里的模型权重 /opt/agentbox/models（4.3 GB，在 platform.qcow2 内部）
* 沙箱镜像如果没打进包：必须在平台 VM 里用 deploy/sandbox/build-sandbox-image.sh 重建

## 校验

    # 逐行核对 sha256（PowerShell）
    Get-Content .\MANIFEST.sha256 | ForEach-Object {
        if (`$_ -match '^([0-9a-f]{64})  (.+)$') {
            `$h = (Get-FileHash -Algorithm SHA256 -LiteralPath `$Matches[2]).Hash.ToLower()
            if (`$h -ne `$Matches[1]) { "BAD  " + `$Matches[2] }
        }
    }
    # 或 Linux: sha256sum -c MANIFEST.sha256
"@
    [System.IO.File]::WriteAllText((Join-Path $script:Staging 'BUNDLE-README.md'), $bundleReadme, (New-Object System.Text.UTF8Encoding($true)))

    $allFiles = @(Get-ChildItem -LiteralPath $script:Staging -Recurse -Force -File -ErrorAction SilentlyContinue)
    $manifest = New-Object System.Collections.ArrayList
    foreach ($f in $allFiles) {
        $rel = $f.FullName.Substring($script:Staging.Length + 1) -replace '\\', '/'
        $hash = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash.ToLower()
        [void]$manifest.Add("$hash  $rel")
    }
    $manifestPath = Join-Path $script:Staging 'MANIFEST.sha256'
    [System.IO.File]::WriteAllText($manifestPath, (($manifest -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
    Write-Host ("[6/6] MANIFEST.sha256: {0} 个文件（不含 MANIFEST 自身）" -f $manifest.Count) -ForegroundColor Green

    # --- 7. 打 zip -------------------------------------------------------
    Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null
    $noCompress = @('.img', '.qcow2', '.iso', '.vmdk', '.raw', '.whl', '.gz', '.xz', '.zip', '.7z')
    $zipFiles = @(Get-ChildItem -LiteralPath $script:Staging -Recurse -Force -File | Sort-Object FullName)
    Write-Host ("正在写 zip（{0} 个文件，{1}）…" -f $zipFiles.Count, (Format-Size (($zipFiles | Measure-Object -Property Length -Sum).Sum))) -ForegroundColor Cyan
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
    $zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        $i = 0
        foreach ($f in $zipFiles) {
            $i++
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

    # --- 8. 报告 ---------------------------------------------------------
    $sbBytes = [long]0
    foreach ($n in @('rootfs.img', 'vmlinuz', 'initrd.img', 'workspace-blank.qcow2')) {
        $s = Get-PathSize (Join-Path $RepoRoot "var\sandbox\$n")
        if ($s -gt 0) { $sbBytes += $s }
    }
    $qemuBytes = Get-PathSize (Join-Path $RepoRoot 'qemu')
    $isoBytes = [long]0
    foreach ($iso in @(Get-ChildItem -LiteralPath $RepoRoot -Filter '*.iso' -File -ErrorAction SilentlyContinue)) { $isoBytes += $iso.Length }

    $rows = @(
        [pscustomobject]@{ 项目 = '源码 + 脚本 (git ls-files)'; 大小 = (Format-Size $srcBytes); 进包 = '是' }
        [pscustomobject]@{ 项目 = 'deploy\ 部署脚本'; 大小 = (Format-Size (Get-PathSize $deployDst)); 进包 = '是' }
        [pscustomobject]@{ 项目 = 'packaging\ 安装器 + 文档'; 大小 = (Format-Size (Get-PathSize (Join-Path $script:Staging 'packaging'))); 进包 = '是' }
        [pscustomobject]@{ 项目 = "wheelhouse\ (Python $($script:Py.Ver) 依赖)"; 大小 = $(if ($IncludeWheelhouse) { Format-Size (Get-PathSize (Join-Path $script:Staging 'wheelhouse')) } else { Format-Size (Get-PathSize $whSrc) }); 进包 = $(if ($IncludeWheelhouse) { '是' } else { '否（-IncludeWheelhouse）' }) }
        [pscustomobject]@{ 项目 = '沙箱镜像 var\sandbox\ (4 个文件)'; 大小 = (Format-Size $sbBytes); 进包 = $(if ($IncludeImages) { '是' } else { '否（-IncludeImages）' }) }
        [pscustomobject]@{ 项目 = '平台磁盘 var\platform\platform.qcow2'; 大小 = (Format-Size (Get-PathSize (Join-Path $RepoRoot 'var\platform\platform.qcow2'))); 进包 = $(if ($IncludeImages) { '是' } else { '否（-IncludeImages）' }) }
        [pscustomobject]@{ 项目 = 'Debian 安装 ISO'; 大小 = (Format-Size $isoBytes); 进包 = $(if ($IncludeImages) { '是' } else { '否（-IncludeImages）' }) }
        [pscustomobject]@{ 项目 = 'QEMU for Windows (qemu\)'; 大小 = (Format-Size $qemuBytes); 进包 = '否（永远不进包，需手工）' }
        [pscustomobject]@{ 项目 = '模型权重 /opt/agentbox/models'; 大小 = '4.3 GB（在平台磁盘内）'; 进包 = $(if ($IncludeImages) { '间接（随 platform.qcow2）' } else { '否' }) }
        [pscustomobject]@{ 项目 = '.env / var\vm_key (密钥)'; 大小 = '—'; 进包 = '否（安全规则，强制排除）' }
        [pscustomobject]@{ 项目 = 'MANIFEST.sha256'; 大小 = (Format-Size (Get-PathSize $manifestPath)); 进包 = '是' }
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
    Write-Host ''
    Write-Host '✔ 打包完成' -ForegroundColor Green
    Write-Host "  zip    : $zipPath"
    Write-Host ("  大小   : {0}" -f (Format-Size $zipItem.Length))
    Write-Host "  文件数 : $(@(Get-ChildItem -LiteralPath $script:Staging -Recurse -Force -File).Count)（含 MANIFEST/BUNDLE-README）"
    Write-Host "  SHA256 : $zipHash"
    Write-Host "  校验   : $zipPath.sha256"
    Write-Host ''
    Write-Host '  说明：磁盘镜像/ISO/whl 用 NoCompression 存进 zip，所以 zip 体积≈原体积；' -ForegroundColor DarkGray
    Write-Host '        源码与文档用 Optimal 压缩。' -ForegroundColor DarkGray
    Write-Host ''
} catch {
    Write-Host ''
    Write-Host "✘ 打包失败: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
} finally {
    if ($script:Staging -and (Test-Path -LiteralPath $script:Staging)) {
        Remove-Item -LiteralPath $script:Staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}
exit $exitCode
