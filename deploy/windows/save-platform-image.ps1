<#
.SYNOPSIS
  把配好的平台 VM 存成你自己可复用的镜像（qcow2，压缩、独立、无后备文件）。

.DESCRIPTION
  这就是"下完/配完保存为镜像"那一步：平台 VM 配好一次之后，用 qemu-img convert -c
  导出一份自包含的压缩镜像，放到 var\images\。以后要重建环境只要：

      qemu-img convert -O qcow2 var\images\agentbox-platform-<日期>.qcow2 var\platform\platform.qcow2
      powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-platform-vm.ps1

  也可以直接把这个 qcow2 拷到别的机器上用（QEMU 通用格式）。

  注意：导出前必须让 VM 干净关机（关机命令：sudo poweroff），否则文件系统可能不一致。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\save-platform-image.ps1
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\save-platform-image.ps1 -Name agentbox-platform-v1
#>
[CmdletBinding()]
param(
    [string]$VmDir,
    [string]$ImageDir,
    [string]$QemuDir,
    [string]$Name,
    [switch]$KeepUncompressed
)

$ErrorActionPreference = 'Stop'
function Info($m) { Write-Host "[save-image] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[save-image] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[save-image] $m" -ForegroundColor Red; exit 1 }

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if (-not $VmDir) { $VmDir = Join-Path $repoRoot 'var\platform' }
if (-not $ImageDir) { $ImageDir = Join-Path $repoRoot 'var\images' }
if (-not $QemuDir) { $QemuDir = Join-Path $repoRoot 'qemu' }
$VmDir = [System.IO.Path]::GetFullPath($VmDir)

$qemuImg = Join-Path $QemuDir 'qemu-img.exe'
if (-not (Test-Path -LiteralPath $qemuImg)) {
    $found = Get-Command 'qemu-img.exe' -ErrorAction SilentlyContinue
    if ($found) { $qemuImg = $found.Source }
}
if (-not (Test-Path -LiteralPath $qemuImg)) { Fail "找不到 qemu-img.exe；用 -QemuDir 指定" }
$qemuImg = (Resolve-Path -LiteralPath $qemuImg).Path

$disk = Join-Path $VmDir 'platform.qcow2'
if (-not (Test-Path -LiteralPath $disk)) { Fail "没有找到 $disk" }

if ((Get-Process qemu-system-x86_64 -ErrorAction SilentlyContinue)) {
    Warn "检测到 QEMU 还在运行。请在 VM 里执行 sudo poweroff，等进程退出后再导出；"
    Warn "否则导出的文件系统可能是未干净卸载的状态。"
    Fail "已中止导出"
}

New-Item -ItemType Directory -Force -Path $ImageDir | Out-Null
if (-not $Name) { $Name = "agentbox-platform-$([DateTime]::Now.ToString('yyyyMMdd-HHmm'))" }
$target = Join-Path $ImageDir "$Name.qcow2"
if (Test-Path -LiteralPath $target) { Fail "$target 已存在；用 -Name 换个名字" }

$before = (Get-Item -LiteralPath $disk).Length
Info "源磁盘   : $disk ($([math]::Round($before / 1MB, 0)) MiB)"
Info "导出目标 : $target"
Info "正在导出（压缩，数据量大时可能要几分钟）..."

& $qemuImg convert -p -c -O qcow2 $disk $target
if ($LASTEXITCODE -ne 0) { Fail "qemu-img convert 失败" }

$after = (Get-Item -LiteralPath $target).Length
Info "导出完成 : $([math]::Round($after / 1MB, 0)) MiB（压缩率 $([math]::Round(100.0 * $after / [Math]::Max(1, $before), 0))%）"

& $qemuImg info --output=json $target | Set-Content -LiteralPath (Join-Path $ImageDir "$Name.info.json") -Encoding UTF8

Info ""
Info "以后用它重建平台 VM:"
Info "  Remove-Item var\platform\platform.qcow2 -Force"
Info "  .\qemu\qemu-img.exe convert -O qcow2 `"$target`" var\platform\platform.qcow2"
Info "  powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-platform-vm.ps1"
Info ""
Info "或者用 provision-cloud-vm.ps1 -ReuseDisk 复用现有磁盘（保留数据、重跑一次 cloud-init 配置）"
