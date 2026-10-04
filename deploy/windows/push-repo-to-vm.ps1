<#
.SYNOPSIS
  把当前仓库推送到平台 VM（覆盖 /opt/agentbox/app 里的代码，保留 .env）。

.DESCRIPTION
  平台 VM 里的 /opt/agentbox/app 是 provisioning 时从 tar 包解开的一份快照。
  改了代码之后必须重新推送，否则 VM 里跑的还是旧代码（这个坑踩过一次：
  修好的构建脚本没生效，重建出来的镜像依旧缺 ctypes）。

  怎么传：宿主起一个临时 HTTP 服务，guest 主动去 10.0.2.2 拉（QEMU 用户态网络里
  宿主就是 10.0.2.2，guest→host 是通的；反向不通，所以不能用 scp 到宿主）。
  .env 不在 tar 包里，不会被覆盖。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\push-repo-to-vm.ps1
#>
[CmdletBinding()]
param(
    [string]$VmDir,
    [string]$SshKey,
    [int]$SshPort = 2222,
    [int]$HttpPort = 8905,
    [string]$RemoteDir = '/opt/agentbox/app',
    [switch]$RestartAi
)

$ErrorActionPreference = 'Stop'
function Invoke-Native([scriptblock]$Block) {
    # 原生命令(stderr 有输出)在 $ErrorActionPreference='Stop' 下会抛 NativeCommandError，
    # 这里临时放开，把 stderr 合并进捕获的文本。
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        return (& $Block 2>&1 | Out-String)
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Info($m) { Write-Host "[push-repo] $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "[push-repo] $m" -ForegroundColor Red; exit 1 }

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if (-not $VmDir) { $VmDir = Join-Path $repoRoot 'var\platform' }
if (-not $SshKey) { $SshKey = Join-Path $repoRoot 'var\vm_key' }

$ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$tar = (Get-Command 'tar.exe' -ErrorAction SilentlyContinue).Source
$py = Get-Command 'py.exe' -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command 'python.exe' -ErrorAction SilentlyContinue }
foreach ($tool in @($ssh, $tar)) { if (-not $tool -or -not (Test-Path -LiteralPath $tool)) { Fail "缺少必要工具: $tool" } }
if (-not (Test-Path -LiteralPath "$SshKey.pub")) { Fail "找不到 $SshKey.pub；先跑 provision-cloud-vm.ps1 生成密钥" }

# 1. pack -------------------------------------------------------------------
$stageDir = Join-Path $VmDir 'repo-push'
New-Item -ItemType Directory -Force -Path $stageDir | Out-Null
$tarball = Join-Path $stageDir 'repo.tar.gz'
Remove-Item -LiteralPath $tarball -ErrorAction SilentlyContinue
Info "打包仓库..."
& $tar -czf $tarball --exclude=.venv --exclude=var --exclude=qemu --exclude=*.iso `
    --exclude=__pycache__ --exclude=.git --exclude=.pytest_cache --exclude=.ruff_cache `
    -C $repoRoot src deploy tests pyproject.toml README.md .env.example .gitignore
if ($LASTEXITCODE -ne 0) { Fail "打包失败" }
Info "  $( [math]::Round((Get-Item -LiteralPath $tarball).Length / 1KB, 1) ) KiB"

# 2. serve it to the guest --------------------------------------------------
$pythonReal = $py.Source
try {
    $resolved = & $py.Source -c "import sys; print(sys.executable)" 2>$null
    if ($resolved -and (Test-Path -LiteralPath $resolved.Trim())) { $pythonReal = $resolved.Trim() }
} catch { }
$listener = Get-NetTCPConnection -LocalPort $HttpPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) { Fail "端口 $HttpPort 已被占用（PID $($listener.OwningProcess)）；用 -HttpPort 换一个" }
$http = Start-Process -PassThru -WindowStyle Hidden -FilePath $pythonReal `
    -ArgumentList @('-m', 'http.server', "$HttpPort", '--bind', '0.0.0.0', '--directory', $stageDir)
Info "临时文件服务: pid $($http.Id)  http://10.0.2.2:$HttpPort/repo.tar.gz"

$sshArgs = @('-i', $SshKey, '-p', "$SshPort", '-o', 'StrictHostKeyChecking=no',
             '-o', 'UserKnownHostsFile=NUL', '-o', 'LogLevel=ERROR', '-o', 'ConnectTimeout=10',
             'agent@127.0.0.1')
function RunRemote([string]$script) {
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($script -replace "`r`n", "`n")))
    Invoke-Native { & $ssh @sshArgs "echo $b64 | base64 -d | bash -s" }
}

try {
    Start-Sleep -Seconds 2
    Info "推送到 $RemoteDir ..."
    $result = RunRemote @"
set -e
curl -fsS --retry 3 -o /tmp/repo.tar.gz http://10.0.2.2:$HttpPort/repo.tar.gz
sudo tar -xzf /tmp/repo.tar.gz -C $RemoteDir
sudo find $RemoteDir -type f -name '*.sh' -exec chmod 0755 {} +
rm -f /tmp/repo.tar.gz
echo "--- 远端文件指纹（确认是新代码）---"
md5sum $RemoteDir/deploy/sandbox/build-sandbox-image.sh $RemoteDir/src/agent/sandbox/init.py
grep -c 'smoke-testing the guest python' $RemoteDir/deploy/sandbox/build-sandbox-image.sh || true
"@
    Info $result.Trim()
    $localMd5 = (& certutil -hashfile (Join-Path $repoRoot 'deploy\sandbox\build-sandbox-image.sh') MD5 2>$null | Select-Object -Index 1)
    Info "本地 build 脚本 MD5: $($localMd5 -replace ' ','')"
} finally {
    if ($http) { & taskkill /PID $http.Id /T /F 2>$null | Out-Null }
}

if ($RestartAi) {
    Info "重启 AI 服务..."
    Info (RunRemote 'sudo systemctl restart agentbox-ai; sleep 2; systemctl is-active agentbox-ai').Trim()
}
Info ""
Info "提示：改了 guest 侧代码（src/agent/sandbox/*）需要重建沙箱镜像才会生效:"
Info "  ssh -i `"$SshKey`" -p $SshPort agent@127.0.0.1 'sudo bash $RemoteDir/deploy/sandbox/build-sandbox-image.sh'"
Info "改了 AI 服务代码则重启即可: .\deploy\windows\push-repo-to-vm.ps1 -RestartAi"
