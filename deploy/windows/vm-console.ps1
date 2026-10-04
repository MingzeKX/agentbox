<#
.SYNOPSIS
  平台 VM 的串口控制台客户端（SSH 不通时的救命通道）。

.DESCRIPTION
  当 guest 被 debootstrap 这类重负载打满时，sshd 可能连 banner 都发不出来，这时只有串口能进去。

    # 平台 VM 必须用 -SerialTcp 启动：
    .\deploy\windows\run-platform-vm.ps1 -Headless -SerialTcp 8906

    # 跑一条命令（可以带 ; && | 等）：
    .\deploy\windows\vm-console.ps1 -Command 'uptime; free -m | head -2'

    # 交互式：
    .\deploy\windows\vm-console.ps1

  注意：用 powershell -File 调用时数组参数会被拼成一个字符串（PowerShell 已知行为），
  所以这里只收单条字符串命令，多条用 ; 或 && 串起来。

.EXAMPLE
  .\deploy\windows\vm-console.ps1 -Command 'pgrep -a -f debootstrap; tail -3 /var/log/sandbox-build.log'
#>
[CmdletBinding()]
param(
    [int]$Port = 8906,
    [string]$Command,
    [string]$User = 'agent',
    [string]$Password = 'agentbox',
    [string]$LogFile,
    [int]$TimeoutSec = 60,
    [switch]$SkipLogin
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if (-not $LogFile) { $LogFile = Join-Path $repoRoot 'var\platform\console-interactive.log' }

function Write-Log([string]$text) { if ($text) { Add-Content -LiteralPath $LogFile -Value $text -Encoding UTF8 } }
function Strip-Ansi([string]$text) {
    $t = $text -replace "`e\[[0-9;?]*[A-Za-z]", ''
    $t = $t -replace "`e\][0-9]+;?[^\a]*\a", ''
    return $t
}

$client = New-Object System.Net.Sockets.TcpClient
try { $client.Connect('127.0.0.1', $Port) } catch {
    Write-Host "连不上串口 $Port —— 平台 VM 是不是没用 -SerialTcp 启动？" -ForegroundColor Red
    Write-Host "  powershell -ExecutionPolicy Bypass -File .\deploy\windows\run-platform-vm.ps1 -Headless -SerialTcp $Port" -ForegroundColor Yellow
    exit 1
}
$stream = $client.GetStream()
$stream.ReadTimeout = 500

function Read-For([int]$seconds) {
    $sb = New-Object System.Text.StringBuilder
    $deadline = (Get-Date).AddSeconds($seconds)
    $buffer = New-Object byte[] 8192
    while ((Get-Date) -lt $deadline) {
        try {
            $n = $stream.Read($buffer, 0, $buffer.Length)
            if ($n -gt 0) { [void]$sb.Append([Text.Encoding]::UTF8.GetString($buffer, 0, $n)) }
        } catch [System.IO.IOException] { }
        catch [System.ObjectDisposedException] { break }
    }
    $text = $sb.ToString()
    Write-Log $text
    return $text
}
function Send-Line([string]$text) {
    $bytes = [Text.Encoding]::ASCII.GetBytes("$text`r")
    $stream.Write($bytes, 0, $bytes.Length); $stream.Flush()
}
function Read-Until([string]$pattern, [int]$seconds) {
    $sb = New-Object System.Text.StringBuilder
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        [void]$sb.Append((Read-For 1))
        if ((Strip-Ansi $sb.ToString()) -match $pattern) { return (Strip-Ansi $sb.ToString()) }
    }
    return (Strip-Ansi $sb.ToString())
}

Write-Host "串口 $Port 已连接（日志: $LogFile）" -ForegroundColor Cyan

# --- login：先等 getty 出现，再输账号密码 ------------------------------------
if (-not $SkipLogin) {
    $pre = Read-Until 'login:|[\$#]\s*$' 25
    if ($pre -match 'login:') {
        Send-Line $User
        [void](Read-For 3)
        Send-Line $Password
        [void](Read-For 4)
    }
    # 串口会回显你输入的文字，所以 "echo MARKER" 在没拿到 shell 时也会匹配到自己的
    # 输入（假阳性）。用 printf 让"输出的字符串"与"输入的命令文本"不同：
    #   输入: printf 'SK%s\n' OK     输出: SKOK
    $shellUp = $false
    for ($i = 0; $i -lt 3; $i++) {
        Send-Line "printf 'SK%s\n' OK_$i"
        $probe = Read-Until "SKOK_$i" 12
        if ($probe -match "SKOK_$i") { $shellUp = $true; break }
        Send-Line ''
    }
    if (-not $shellUp) {
        Write-Host "没能确认拿到 shell（可能还在启动或密码不对），原始输出：" -ForegroundColor Yellow
        Write-Host (Strip-Ansi (Read-For 3))
        $client.Close(); exit 2
    }
    Write-Host "已登录（$User）" -ForegroundColor DarkGray
}

# --- 单条命令模式 -----------------------------------------------------------
if ($Command) {
    $done = "__CMDDONE_$([Guid]::NewGuid().ToString('N').Substring(0,8))__"
    Write-Host "`n$ $Command" -ForegroundColor Yellow
    Send-Line $Command
    [void](Read-For 1)
    Send-Line "echo $done"
    $out = Read-Until $done $TimeoutSec
    $lines = ($out -split "`r?`n") | Where-Object { $_ -notmatch $done -and $_.Trim() -ne '' }
    Write-Host (($lines -join "`n").Trim())
    $client.Close()
    exit 0
}

# --- 交互模式 ---------------------------------------------------------------
Write-Host "交互模式：输入命令，空行或 /exit 退出" -ForegroundColor Cyan
while ($true) {
    $line = Read-Host "console"
    if (-not $line -or $line -eq '/exit') { break }
    Send-Line $line
    Write-Host (Strip-Ansi (Read-For 6))
}
Send-Line 'exit'
Start-Sleep -Milliseconds 400
[void](Read-For 1)
$client.Close()
