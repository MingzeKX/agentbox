<#
.SYNOPSIS
  用 Debian 成品云镜像 + cloud-init 一次性配好平台 VM（跳过安装器，几分钟完事）。

.DESCRIPTION
  流程：
    1. 从 var\images\ 的云镜像复制出 var\platform\platform.qcow2，扩容到 40 GiB
    2. 把本仓库打成 tar.gz，和渲染好的 cloud-init 文件一起用 HTTP 提供给 guest
    3. 启动 QEMU，用 SMBIOS 串号告诉 cloud-init：
         ds=nocloud;s=http://10.0.2.2:8899/
       guest 因此会自己拉取 user-data/meta-data，按里面的配置：
         换清华 apt 源 -> 装 PostgreSQL/pgvector/python -> 拉本仓库 -> 跑 install-platform.sh
    4. 串口日志实时可见（-Follow 另开窗口），配好后 VM 里会有 /opt/agentbox/PROVISIONED

  默认给图形控制台（-Display sdl），可以直接在窗口里用 agent / agentbox 登录。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\provision-cloud-vm.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\provision-cloud-vm.ps1 -Follow
  powershell -ExecutionPolicy Bypass -File .\deploy\windows\provision-cloud-vm.ps1 -Display none -Follow
#>
[CmdletBinding()]
param(
    [string]$VmDir,
    [string]$ImageDir,
    [string]$QemuDir,
    [ValidateSet('sdl', 'none')]
    [string]$Display = 'sdl',
    [ValidateSet('tuna', 'aliyun', 'official', 'custom')]
    [string]$Mirror = 'tuna',
    [string]$MirrorUri,
    [string]$PipIndex,
    [string]$Password = 'agentbox',
    [string]$Hostname = 'agentbox-platform',
    [string]$CpuModel = 'Nehalem',
    [int]$MemoryMb = 6144,
    [int]$Cpus = 6,
    [int]$DiskGb = 40,
    [int]$HttpPort = 8899,
    [int]$SshPort = 2222,
    [switch]$ReuseDisk,
    [switch]$Follow,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
# qemu64 保守但没有 SSE4.2：最新 numpy 等 x86-64-v2 wheel 会拒绝加载。
# Nehalem 实测在 WHPX 下可用且带 SSE4.2，所以默认它（host/max 仍然不可用）。
function Info($m) { Write-Host "[cloud-vm] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[cloud-vm] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[cloud-vm] $m" -ForegroundColor Red; exit 1 }

# --------------------------------------------------------------------------
# 0. paths and mirrors
# --------------------------------------------------------------------------
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if (-not $VmDir) { $VmDir = Join-Path $repoRoot 'var\platform' }
if (-not $ImageDir) { $ImageDir = Join-Path $repoRoot 'var\images' }
if (-not $QemuDir) { $QemuDir = Join-Path $repoRoot 'qemu' }
$VmDir = [System.IO.Path]::GetFullPath($VmDir)

switch ($Mirror) {
    'tuna' { $mirrorDefault = 'http://mirrors.tuna.tsinghua.edu.cn/debian'; $secDefault = 'http://mirrors.tuna.tsinghua.edu.cn/debian-security'; $pipDefault = 'https://pypi.tuna.tsinghua.edu.cn/simple' }
    'aliyun' { $mirrorDefault = 'http://mirrors.aliyun.com/debian'; $secDefault = 'http://mirrors.aliyun.com/debian-security'; $pipDefault = 'https://mirrors.aliyun.com/pypi/simple' }
    'official' { $mirrorDefault = 'http://deb.debian.org/debian'; $secDefault = 'http://security.debian.org/debian-security'; $pipDefault = 'https://pypi.org/simple' }
    default { $mirrorDefault = 'http://mirrors.tuna.tsinghua.edu.cn/debian'; $secDefault = 'http://mirrors.tuna.tsinghua.edu.cn/debian-security'; $pipDefault = 'https://pypi.tuna.tsinghua.edu.cn/simple' }
}
if ($MirrorUri) { $mirrorDefault = $MirrorUri }
if (-not $PipIndex) { $PipIndex = $pipDefault }
$pipHost = ([System.Uri]$PipIndex).Host

$qemu = Join-Path $QemuDir 'qemu-system-x86_64.exe'
if (-not (Test-Path -LiteralPath $qemu)) {
    $found = Get-Command 'qemu-system-x86_64.exe' -ErrorAction SilentlyContinue
    if ($found) { $qemu = $found.Source }
}
if (-not (Test-Path -LiteralPath $qemu)) { Fail "找不到 qemu-system-x86_64.exe；用 -QemuDir 指定" }
$qemuImg = Join-Path $QemuDir 'qemu-img.exe'
if (-not (Test-Path -LiteralPath $qemuImg)) {
    $found = Get-Command 'qemu-img.exe' -ErrorAction SilentlyContinue
    if ($found) { $qemuImg = $found.Source }
}
if (-not (Test-Path -LiteralPath $qemuImg)) { Fail "找不到 qemu-img.exe；用 -QemuDir 指定" }
$tar = (Get-Command 'tar.exe' -ErrorAction SilentlyContinue).Source
$python = Get-Command 'py.exe' -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command 'python.exe' -ErrorAction SilentlyContinue }
if (-not $python) { Fail "需要一个 Python 解释器（py 或 python）来提供 HTTP 服务" }
# py.exe 只是启动器：用真正的解释器，这样 Start-Process 返回的就是 http.server 本体，
# 杀掉它不会留下子进程（早期版本因此泄漏过服务，把端口占住导致后续静默失败）。
$pythonReal = $python.Source
try {
    $resolved = & $python.Source -c "import sys; print(sys.executable)" 2>$null
    if ($resolved -and (Test-Path -LiteralPath $resolved.Trim())) { $pythonReal = $resolved.Trim() }
} catch { }

# Public key injected into the guest so the host can operate it over ssh
# (QEMU user networking forwards 127.0.0.1:<SshPort> to guest port 22).
$sshKeyPath = Join-Path $repoRoot 'var\vm_key.pub'
if (-not (Test-Path -LiteralPath $sshKeyPath)) {
    $keygen = Join-Path $env:WINDIR 'System32\OpenSSH\ssh-keygen.exe'
    if (-not (Test-Path -LiteralPath $keygen)) {
        Fail "缺少 ssh-keygen（Windows 自带 OpenSSH）；或者手工生成 var\vm_key.pub 后重跑"
    }
    Info "generating   : var\vm_key (ed25519, no passphrase)"
    & $keygen -t ed25519 -N '""' -C agentbox -f (Join-Path $repoRoot 'var\vm_key') | Out-Null
}
if (-not (Test-Path -LiteralPath $sshKeyPath)) { Fail "拿不到 SSH 公钥: $sshKeyPath" }
$sshKey = (Get-Content -Raw -LiteralPath $sshKeyPath).Trim()

function Wait-SeedFile($url, $expectPrefix, $timeoutSec) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -eq 200) {
                if (-not $expectPrefix) { return $true }
                # PS 5.1 对 binary 类型返回 byte[]，直接 .StartsWith() 会抛异常被吞掉
                $body = $response.Content
                if ($body -is [byte[]]) { $body = [System.Text.Encoding]::UTF8.GetString($body) }
                if ($body.StartsWith($expectPrefix)) { return $true }
                Warn "  $url 内容不以 '$expectPrefix' 开头，前 40 字符: $($body.Substring(0, [Math]::Min(40, $body.Length)))"
                return $false
            }
        } catch { Start-Sleep -Milliseconds 400 }
    }
    return $false
}

function Assert-PortFree($port) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $owner = (Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue).ProcessName
        Fail "端口 $port 已被占用（PID $($listener.OwningProcess) / $owner）。`n可能是上一次运行残留的 HTTP 服务，先清理：`n  Stop-Process -Id $($listener.OwningProcess) -Force`n或者换端口： -HttpPort 8901"
    }
}

$baseImage = Get-ChildItem -LiteralPath $ImageDir -Filter '*genericcloud-amd64.qcow2' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $baseImage) {
    if ($DryRun) {
        Info "(dry-run) 还没有云镜像（$ImageDir）；真正运行时会先自动执行:"
        Info "(dry-run)   powershell -ExecutionPolicy Bypass -File .\deploy\windows\fetch-platform-image.ps1   (约 326 MiB)"
    } else {
        Info "还没下载云镜像，先跑 fetch-platform-image.ps1"
        $fetch = Join-Path $PSScriptRoot 'fetch-platform-image.ps1'
        & powershell -NoProfile -ExecutionPolicy Bypass -File $fetch
        if ($LASTEXITCODE -ne 0) { Fail "云镜像下载失败" }
        $baseImage = Get-ChildItem -LiteralPath $ImageDir -Filter '*genericcloud-amd64.qcow2' | Select-Object -First 1
        if (-not $baseImage) { Fail "下载后仍找不到镜像文件，检查 $ImageDir" }
    }
}

$disk = Join-Path $VmDir 'platform.qcow2'
$consoleLog = Join-Path $VmDir 'console.log'
$instanceId = "agentbox-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))"

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
if ($baseImage) {
    Info "base image   : $($baseImage.FullName) ($([math]::Round($baseImage.Length / 1MB, 1)) MiB)"
} else {
    Info "base image   : (尚未下载，将自动获取 326 MiB 的 Debian 13 云镜像)"
}
Info "platform disk: $disk"
Info "apt mirror   : $mirrorDefault"
Info "pip index    : $PipIndex"
Info "instance-id  : $instanceId"
Info "display      : $Display"
Info "cpu model    : $CpuModel"

# --------------------------------------------------------------------------
# 1. accelerate check (WHPX cannot virtualise -cpu host/max)
# --------------------------------------------------------------------------
$accel = 'whpx'
if ($CpuModel -eq 'host' -or $CpuModel -eq 'max') {
    Warn "-cpu $CpuModel 在 WHPX 上会让 guest 停住（实测串口 0 字节），自动改用 Nehalem"
    $CpuModel = 'Nehalem'
}
if ($DryRun) {
    $accel = '(dry-run: 会先探测 whpx，失败则 tcg)'
} else {
    $probe = Start-Job -ScriptBlock { param($q) & $q -accel whpx -machine q35 -m 128 -display none -nodefaults -S 2>&1 } -ArgumentList $qemu
    if (Wait-Job $probe -Timeout 6) {
        $probeOutput = Receive-Job $probe
        Warn "WHPX 不可用，退回 tcg: $($probeOutput -join ' ')"
        $accel = 'tcg'
    }
    Stop-Job $probe -ErrorAction SilentlyContinue
    Remove-Job $probe -Force -ErrorAction SilentlyContinue
}

# --------------------------------------------------------------------------
# 2. render cloud-init files into a serve directory
# --------------------------------------------------------------------------
$serveDir = Join-Path $VmDir 'seed'
New-Item -ItemType Directory -Force -Path $serveDir | Out-Null

$tplDir = Join-Path $PSScriptRoot 'cloud-init'
$userDataTpl = Get-Content -Raw -LiteralPath (Join-Path $tplDir 'user-data.tpl')
$metaDataTpl = Get-Content -Raw -LiteralPath (Join-Path $tplDir 'meta-data.tpl')

$userData = $userDataTpl.
    Replace('@HOSTNAME@', $Hostname).
    Replace('@MIRROR_URI@', $mirrorDefault).
    Replace('@SECURITY_URI@', $secDefault).
    Replace('@PIP_INDEX@', $PipIndex).
    Replace('@PIP_HOST@', $pipHost).
    Replace('@INSTANCE_ID@', $instanceId).
    Replace('@PASSWORD@', $Password).
    Replace('@SERVE_URL@', "http://10.0.2.2:$HttpPort/").
    Replace('@SSH_KEY@', $sshKey)
$metaData = $metaDataTpl.Replace('@INSTANCE_ID@', $instanceId).Replace('@HOSTNAME@', $Hostname)

# PowerShell 5.1 的 `Set-Content -Encoding UTF8` 会写 BOM，而 BOM 出现在
# `#cloud-config` 之前会让 cloud-init 判定为未知类型（退回 DataSourceNone，
# 整份 user-data 静默失效），所以显式写成无 BOM UTF-8。
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $serveDir 'user-data'), $userData, $utf8NoBom)
[System.IO.File]::WriteAllText((Join-Path $serveDir 'meta-data'), $metaData, $utf8NoBom)

# repository tarball the guest unpacks into /opt/agentbox/app
$tarball = Join-Path $serveDir 'agentbox.tar.gz'
Remove-Item -LiteralPath $tarball -ErrorAction SilentlyContinue
$tarArgs = @(
    '-czf', $tarball,
    '--exclude=.venv', '--exclude=var', '--exclude=qemu', '--exclude=*.iso',
    '--exclude=__pycache__', '--exclude=.git', '--exclude=.pytest_cache', '--exclude=.ruff_cache',
    '-C', $repoRoot,
    'src', 'deploy', 'tests', 'pyproject.toml', 'README.md', '.env.example', '.gitignore'
)

# --------------------------------------------------------------------------
# 3. build the QEMU command line
# --------------------------------------------------------------------------
$displayArgs = @('-display', 'none', '-serial', "file:$consoleLog")
if ($Display -eq 'sdl') {
    # VGA console in a window *and* the serial log for cloud-init progress
    $displayArgs = @('-display', 'sdl', '-serial', "file:$consoleLog")
}

$qemuArgs = @(
    '-machine', 'q35', '-accel', $accel, '-cpu', $CpuModel,
    '-smp', "$Cpus", '-m', "$MemoryMb",
    '-drive', "file=$disk,if=virtio,format=qcow2,discard=unmap",
    '-nic', "user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:8090-:8090,hostfwd=tcp:127.0.0.1:8099-:8099,hostfwd=tcp:127.0.0.1:$SshPort-:22",
    '-device', 'virtio-rng-pci',
    '-smbios', "type=1,serial=ds=nocloud;s=http://10.0.2.2:$HttpPort/",
    '-rtc', 'base=utc'
) + $displayArgs

if ($DryRun) {
    $baseName = if ($baseImage) { $baseImage.FullName } else { "$ImageDir\debian-13-genericcloud-amd64.qcow2" }
    Info "(dry-run) 云镜像 -> 平台磁盘: $qemuImg convert -O qcow2 `"$baseName`" `"$disk`"；再 resize ${DiskGb}G"
    Info "(dry-run) 仓库打包: $tar $($tarArgs -join ' ')"
    Info "(dry-run) HTTP 服务: $pythonReal -m http.server $HttpPort --bind 0.0.0.0 --directory `"$serveDir`""
    Info "(dry-run) 将要执行的命令:"
    Write-Host ""
    Write-Host "  $qemu $($qemuArgs -join ' ')"
    Write-Host ""
    Info "(dry-run) guest 侧会自己执行：换清华源 -> 装 PostgreSQL/pgvector/python -> 拉仓库 -> install-platform.sh"
    Info "(dry-run) 没有改动任何东西；去掉 -DryRun 就真正开始"
    exit 0
}

# --------------------------------------------------------------------------
# 4. materialise the disk, pack the repo, serve the seed, boot
# --------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $VmDir | Out-Null
if (-not $ReuseDisk -or -not (Test-Path -LiteralPath $disk)) {
    if (Test-Path -LiteralPath $disk) { Remove-Item -LiteralPath $disk -Force }
    Info "creating     : 从云镜像生成 $DiskGb GiB 平台磁盘（本地拷贝，几秒）"
    & $qemuImg convert -O qcow2 -o "preallocation=metadata" $baseImage.FullName $disk
    if ($LASTEXITCODE -ne 0) { Fail "qemu-img convert 失败" }
    & $qemuImg resize $disk "${DiskGb}G" | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "qemu-img resize 失败" }
} else {
    Info "reusing      : $disk（-ReuseDisk：不再灌入云镜像，但 cloud-init 会按新 instance-id 重跑一遍）"
}

Info "packing repo : $tarball"
& $tar @tarArgs
if ($LASTEXITCODE -ne 0) { Fail "打包仓库失败" }
Info "             : $([math]::Round((Get-Item -LiteralPath $tarball).Length / 1KB, 0)) KiB"

Remove-Item -LiteralPath $consoleLog -ErrorAction SilentlyContinue
Assert-PortFree $HttpPort
$httpJob = Start-Process -PassThru -WindowStyle Hidden -FilePath $pythonReal `
    -ArgumentList @('-m', 'http.server', "$HttpPort", '--bind', '0.0.0.0', '--directory', $serveDir)
Start-Sleep -Seconds 2
Info "seed server  : pid $($httpJob.Id)  http://10.0.2.2:$HttpPort/{user-data,meta-data,agentbox.tar.gz}"

# 绝不启动一个取不到 seed 的 guest：这里静默 404 会让 cloud-init 退回
# DataSourceNone 并且什么都不配置（排查起来非常费劲）。
$seedOk = (Wait-SeedFile "http://127.0.0.1:$HttpPort/user-data" '#cloud-config' 10) -and
          (Wait-SeedFile "http://127.0.0.1:$HttpPort/meta-data" 'instance-id' 10) -and
          (Wait-SeedFile "http://127.0.0.1:$HttpPort/agentbox.tar.gz" $null 10)
if (-not $seedOk) {
    if ($httpJob) { & taskkill /PID $httpJob.Id /T /F 2>$null | Out-Null }
    Fail "seed 服务校验失败：http://127.0.0.1:$HttpPort/ 取不到 user-data/meta-data/agentbox.tar.gz。检查 $serveDir 与端口占用"
}
Info "seed 校验    : user-data / meta-data / agentbox.tar.gz 均可访问且格式正确"

$tailJob = $null
if ($Follow) {
    $tailJob = Start-Process -PassThru -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-Command', "Get-Content -LiteralPath '$consoleLog' -Wait -Tail 30")
    Info "console tail : 已另开窗口显示串口日志（cloud-init 进度在这里）"
}

Info ""
Info "guest 正在自动配置（换源 -> apt 装包 -> 拉仓库 -> install-platform.sh），预计 3-8 分钟"
Info "完成标志：串口日志出现 'agentbox platform ready'，且 VM 内 /opt/agentbox/PROVISIONED 存在"
Info "登录：agent / $Password （图形窗口，或 ssh -p 8091 转发）"
Info "SSH      : ssh -i var\vm_key -p $SshPort -o StrictHostKeyChecking=no agent@127.0.0.1"
Info ""
try {
    & $qemu @qemuArgs
} finally {
    # taskkill /T 顺带回收启动器可能派生的子进程
    if ($httpJob) { & taskkill /PID $httpJob.Id /T /F 2>$null | Out-Null }
    if ($tailJob) { & taskkill /PID $tailJob.Id /T /F 2>$null | Out-Null }
}

Info ""
Info "配好后建议立刻把它存成你自己的镜像（以后不用再配）:"
Info "  powershell -ExecutionPolicy Bypass -File .\deploy\windows\save-platform-image.ps1"
