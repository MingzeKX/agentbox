# 找出这台机器上 WHPX 真正能跑起来的 CPU 型号 / vCPU 组合。
# 对每个组合启动 25 秒安装器内核，看两件事：QEMU 是否报 WHPX 错误、串口有没有输出。
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$qemu = Join-Path $repo 'qemu\qemu-system-x86_64.exe'
$dir = Join-Path $repo 'var\platform-smoketest'
$kernel = Join-Path $dir 'vmlinuz'
$initrd = Join-Path $dir 'initrd.gz'

if (-not (Test-Path $kernel)) { Write-Host "missing $kernel"; exit 1 }

$combos = @(
    @{ cpu = 'host';    smp = 2 },
    @{ cpu = 'host';    smp = 1 },
    @{ cpu = 'qemu64';  smp = 2 },
    @{ cpu = 'qemu64';  smp = 1 },
    @{ cpu = 'Nehalem'; smp = 2 },
    @{ cpu = 'max';     smp = 1 }
)

foreach ($combo in $combos) {
    $log = Join-Path $dir ("probe-" + $combo.cpu + "-smp" + $combo.smp + ".log")
    Remove-Item -LiteralPath $log -ErrorAction SilentlyContinue
    $qemuArgs = @(
        '-machine', 'q35', '-accel', 'whpx', '-cpu', $combo.cpu,
        '-smp', "$($combo.smp)", '-m', '2048',
        '-kernel', $kernel, '-initrd', $initrd,
        '-append', 'console=ttyS0,115200 ignoring_loglevel',
        '-display', 'none', '-serial', "file:$log", '-no-reboot', '-nic', 'none'
    )
    $job = Start-Job -ScriptBlock { param($q, $a) & $q @a 2>&1 } -ArgumentList $qemu, $qemuArgs
    Start-Sleep -Seconds 25
    $output = Receive-Job $job
    Stop-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    Get-Process qemu-system-x86_64 -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1

    $stdErr = ($output | Out-String)
    $whpxBad = $stdErr -match 'Unexpected VP exit'
    $interrupt = $stdErr -match 'interrupt vector'
    $console = if (Test-Path $log) { Get-Content -LiteralPath $log -ErrorAction SilentlyContinue } else { @() }
    $consoleSize = if (Test-Path $log) { (Get-Item $log).Length } else { 0 }

    $verdict = if ($whpxBad) { 'WHPX FAIL ' } elseif ($consoleSize -gt 0) { 'BOOTS OK  ' } else { 'no output ' }
    Write-Host ("{0} cpu={1,-8} smp={2} console={3,7} bytes  interrupt-warn={4}" -f $verdict, $combo.cpu, $combo.smp, $consoleSize, $interrupt)
    if ($console.Count -gt 0) {
        Write-Host ("           first line: " + ($console | Select-Object -First 1))
    }
    if ($whpxBad) {
        Write-Host ("           qemu: " + (($output | Select-Object -Last 2) -join ' | '))
    }
}
