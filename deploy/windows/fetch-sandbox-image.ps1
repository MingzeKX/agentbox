<#
.SYNOPSIS
  把平台 VM 里构建好的沙箱镜像拉到 Windows 宿主（控制平面就需要这几个文件）。

.DESCRIPTION
  为什么需要这一步：沙箱镜像必须用 debootstrap 在 Debian 里构建，而"控制平面"
  跑在 Windows 上（Windows 上的 QEMU 才有 WHPX 加速）。两个 VM 之间没有共享目录，
  所以让平台 VM 用 HTTP 把这些文件发出来，宿主拉回来。

  平台 VM 里的准备（一次）：
      cd /var/lib/agentbox/sandbox
      nohup python3 -m http.server 8099 --bind 0.0.0.0 >/tmp/seed-http.log 2>&1 &

  需要的端口转发（run-platform-vm.ps1 / provision-cloud-vm.ps1 已经带上了）：
      127.0.0.1:8099 -> guest 8099

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\fetch-sandbox-image.ps1
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\fetch-sandbox-image.ps1 -Force
#>
[CmdletBinding()]
param(
    [string]$SandboxDir,
    [string]$SourceUrl = 'http://127.0.0.1:8099',
    [string[]]$Files = @('vmlinuz', 'initrd.img', 'rootfs.img', 'workspace-blank.qcow2'),
    [switch]$Force,
    # 运行中的沙箱 VM 会占着 rootfs.img / workspace-blank.qcow2：换镜像前必须停掉控制平面
    [switch]$StopControlPlane
)

$ErrorActionPreference = 'Stop'
function Info($m) { Write-Host "[fetch-sandbox] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[fetch-sandbox] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[fetch-sandbox] $m" -ForegroundColor Red; exit 1 }

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if (-not $SandboxDir) { $SandboxDir = Join-Path $repoRoot 'var\sandbox' }
New-Item -ItemType Directory -Force -Path $SandboxDir | Out-Null

if ($StopControlPlane) {
    Info "停止控制平面（它启动的沙箱 VM 正占着镜像文件）..."
    $stopped = 0
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*agent.cli*serve*control*' } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $stopped++
        }
    Info "  已停 $stopped 个控制平面进程；等它释放镜像文件"
    Start-Sleep -Seconds 4
}

$curl = (Get-Command 'curl.exe' -ErrorAction SilentlyContinue).Source
if (-not $curl) { Fail "找不到 curl.exe（Windows 10+ 自带）" }

Info "来源      : $SourceUrl"
Info "目标目录  : $SandboxDir"

# 先确认对面的服务活着，避免逐个文件报无关的错
$probe = & $curl -s -o NUL -w '%{http_code}' --max-time 8 "$SourceUrl/" 2>$null
if ($probe -ne '200') {
    Fail @"
拉取失败：$SourceUrl 返回 HTTP $probe。
请先在【平台 VM 内】启动发送服务：
    cd /var/lib/agentbox/sandbox
    nohup python3 -m http.server 8099 --bind 0.0.0.0 >/tmp/seed-http.log 2>&1 &
并确认平台 VM 是用 run-platform-vm.ps1 / provision-cloud-vm.ps1 启动的（它们带 8099 端口转发）。
若镜像还没构建：sudo deploy/sandbox/build-sandbox-image.sh
"@
}

$failed = @()
foreach ($name in $Files) {
    $target = Join-Path $SandboxDir $name
    if ((Test-Path -LiteralPath $target) -and -not $Force) {
        Info "跳过      : $name 已存在（$([math]::Round((Get-Item -LiteralPath $target).Length / 1MB, 1)) MiB）；想重下加 -Force"
        continue
    }
    if (Test-Path -LiteralPath $target) {
        try {
            Remove-Item -LiteralPath $target -Force -ErrorAction Stop
        } catch {
            Fail @"
无法替换 $name：它正被运行中的沙箱 VM 占用。
沙箱 VM 由控制平面启动，先停掉控制平面再拉：
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\fetch-sandbox-image.ps1 -Force -StopControlPlane
（注意：换完镜像要重启控制平面，池里的旧 VM 跑的还是旧镜像）
"@
        }
    }
    Info "下载      : $name ..."
    & $curl -L --fail --retry 3 --retry-delay 2 -o $target "$SourceUrl/$name"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $target)) {
        $failed += $name
        continue
    }
    Info "          : $name -> $([math]::Round((Get-Item -LiteralPath $target).Length / 1MB, 1)) MiB"
}

if ($failed.Count -gt 0) { Fail "以下文件没拉到：$($failed -join ', ')" }

# 和 Python 侧同一套判据：四个文件齐了控制平面才会启动 VM
Info ""
Info "在 Windows 宿主上校验（应与上面一致）:"
Info "  .\.venv\Scripts\python -m agent.cli image verify"
Info ""
Info "然后启动控制平面:"
Info "  `$env:AGENT_CONTROL_SECRET = '<与平台 VM 的 .env 相同>'"
Info "  .\.venv\Scripts\python -m agent.cli serve control"
Info "  .\.venv\Scripts\python -m agent.cli sandbox status    # 应显示预热 VM"
