<#
.SYNOPSIS
  下载 Debian 13 成品云镜像（qcow2），保存到 var\images\ 并可复用。

.DESCRIPTION
  这是"不想等安装器"的那条快路：直接下一个装好的 Debian 13（326 MiB），
  再用 provision-cloud-vm.ps1 通过 cloud-init 一次性配好 PostgreSQL/pgvector/python。

  下载用 Windows 自带的 curl.exe：
    * 断点续传（-C -）：中断了再跑一次会接着下
    * 带进度条，比串口日志直观
    * 下完自动核对官方 SHA512

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\fetch-platform-image.ps1
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\fetch-platform-image.ps1 -Force   # 重新下
#>
[CmdletBinding()]
param(
    [string]$ImageDir,
    [string]$Url = 'https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2',
    [string]$ChecksumUrl = 'https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS',
    [switch]$Force,
    [switch]$SkipChecksum
)

$ErrorActionPreference = 'Stop'
function Info($m) { Write-Host "[fetch-image] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[fetch-image] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[fetch-image] $m" -ForegroundColor Red; exit 1 }

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if (-not $ImageDir) { $ImageDir = Join-Path $repoRoot 'var\images' }
New-Item -ItemType Directory -Force -Path $ImageDir | Out-Null

$name = [System.IO.Path]::GetFileName($Url)
$target = Join-Path $ImageDir $name
$curl = (Get-Command 'curl.exe' -ErrorAction SilentlyContinue).Source
if (-not $curl) { Fail "找不到 curl.exe（Windows 10+ 自带）；也可以手动下载 $Url 到 $ImageDir" }

Info "目标文件 : $target"
Info "镜像 URL : $Url"

if ($Force -and (Test-Path -LiteralPath $target)) {
    Remove-Item -LiteralPath $target -Force
}

if (Test-Path -LiteralPath $target) {
    $size = (Get-Item -LiteralPath $target).Length
    Info "已存在（$([math]::Round($size / 1MB, 1)) MiB），尝试续传/校验；想重下加 -Force"
}

# curl 的 -C - 会断点续传；已下完时它只做一次很小的 range 请求
Info "开始下载（支持断点续传；中断后重跑本脚本即可继续）"
& $curl -L --fail --retry 5 --retry-delay 3 -C - -o $target $Url
if ($LASTEXITCODE -ne 0) {
    Fail "下载失败（exit $LASTEXITCODE）。网络不稳时重跑本脚本会续传"
}

$size = (Get-Item -LiteralPath $target).Length
Info "下载完成 : $([math]::Round($size / 1MB, 1)) MiB"

if (-not $SkipChecksum) {
    Info "核对 SHA512 ..."
    $sums = & $curl -sL --max-time 60 $ChecksumUrl
    $expected = ($sums | Where-Object { $_ -match [regex]::Escape($name) } | Select-Object -First 1)
    if (-not $expected) {
        Warn "拿不到官方校验和（$ChecksumUrl），跳过校验"
    } else {
        $expectedHash = ($expected -split '\s+')[0].ToUpper()
        $actualHash = (Get-FileHash -LiteralPath $target -Algorithm SHA512).Hash.ToUpper()
        if ($actualHash -ne $expectedHash) {
            Fail "SHA512 不匹配！`n  期望 $expectedHash`n  实际 $actualHash`n文件可能损坏，用 -Force 重下"
        }
        Info "SHA512 校验通过"
    }
}

# 记录来源，便于以后追溯/复现
$meta = [ordered]@{
    image      = $name
    url        = $Url
    sha512     = (Get-FileHash -LiteralPath $target -Algorithm SHA512).Hash
    bytes      = $size
    fetched_at = (Get-Date).ToString('s')
}
$meta | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ImageDir "$name.json") -Encoding UTF8

Info ""
Info "下一步（用成品镜像一次性配好平台 VM，跳过安装器）:"
Info "  powershell -ExecutionPolicy Bypass -File .\deploy\windows\provision-cloud-vm.ps1 -Follow"
