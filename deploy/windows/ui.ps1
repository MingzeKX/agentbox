<#
.SYNOPSIS
  agentbox Windows 脚本的共享显示层：横幅、原地刷新的进度条、计时表和每次运行的日志。

.USAGE
  在最前面点源它（不要用 . 之外的调用方式）：
      . (Join-Path $PSScriptRoot 'ui.ps1')
  然后：
      Initialize-UiRun -LogName 'start' -Title '一键启动 agentbox' -RepoRoot $repoRoot |
          ForEach-Object { $logPath = $_ }          # 或者直接 Initialize-UiRun ...（会打印日志路径）
      Show-UiBanner -Title '...' -Subtitle '...' -LogPath $logPath
      Set-UiSteps @('前置检查', '平台 VM', ...)      # Total 由这里决定
      Start-UiStep -Index 1 -Title '前置检查'
      Update-UiStep -Percent 52 -Status '等待 SSH… (42s)'
      Complete-UiStep -Detail 'SSH 已通'
      Skip-UiStep -Detail '跳过对话（-NoChat）'
      Fail-UiStep -Detail 'AI 服务 /health 不通'
      Complete-UiRun
      Exit-Ui                                   # 还原光标，别在失败路径上漏掉

.DESCRIPTION
  两个显示模式，自动选择，不需要调用方判断：
    * 实时模式：输出到真实控制台 -> `r 原地刷新一行进度条。
    * 纯文本模式：输出被重定向（日志/CI）-> 只打带时间戳的整行，绝不含 `r。
  规则：日志文件永远只有整行；控制台永远不留半行；不隐藏光标就不还原；
  同时挂屏幕和文件时用 -Console none 去重。不使用 ANSI 颜色，
  一律沿用 Write-Host -ForegroundColor。
#>

# ------------------------------------------------------------------ 全局状态
$script:UiLogPath = $null
$script:UiLogDir = $null
$script:UiStream = $null
$script:UiTitle = 'agentbox'
$script:UiRepoRoot = $null
$script:UiSteps = @()
$script:UiStepIndex = 0
$script:UiStepTitle = ''
$script:UiStepStart = $null
$script:UiSubTitle = ''
$script:UiSubStart = $null
$script:UiStepStatus = ''
$script:UiLastStatusText = ''
$script:UiLastLogAt = [datetime]::MinValue
$script:UiPlain = $true
$script:UiLiveLineLen = 0
$script:UiCursorHidden = $false
$script:UiExited = $false
$script:UiFailed = $false
$script:UiRecords = New-Object System.Collections.ArrayList
$script:UiBarState = @{}
$script:UiConsoleWidth = 0

try { $script:UiPlain = [bool][Console]::IsOutputRedirected } catch { $script:UiPlain = $true }

# ------------------------------------------------------------------ 小工具
function Get-UiDisplayWidth([string]$Text) {
    # 中文/全角算 2 列，其余算 1 列：用于表格对齐，否则中文一多就歪。
    if ([string]::IsNullOrEmpty($Text)) { return 0 }
    $width = 0
    foreach ($ch in $Text.ToCharArray()) {
        $code = [int][char]$ch
        if (($code -ge 0x1100 -and $code -le 0x115F) -or
            ($code -ge 0x2E80 -and $code -le 0xA4CF) -or
            ($code -ge 0xAC00 -and $code -le 0xD7A3) -or
            ($code -ge 0xF900 -and $code -le 0xFAFF) -or
            ($code -ge 0xFE30 -and $code -le 0xFE4F) -or
            ($code -ge 0xFF00 -and $code -le 0xFF60) -or
            ($code -ge 0xFFE0 -and $code -le 0xFFE6)) { $width += 2 } else { $width += 1 }
    }
    return $width
}

function Add-UiPadding([string]$Text, [int]$Width) {
    $pad = $Width - (Get-UiDisplayWidth $Text)
    if ($pad -gt 0) { return $Text + (' ' * $pad) }
    return $Text
}

function Get-UiElapsedText([double]$Seconds) {
    if ($Seconds -ge 60) { return ('{0}m{1:00}s' -f [int][math]::Floor($Seconds / 60), [int][math]::Round($Seconds % 60)) }
    return ('{0:N0}s' -f $Seconds)
}

function Format-UiDuration([double]$Seconds) {
    if ($Seconds -ge 60) { return ('{0}m{1:00}s' -f [int][math]::Floor($Seconds / 60), [int][math]::Round($Seconds % 60)) }
    if ($Seconds -ge 10) { return ('{0:N0}s' -f $Seconds) }
    return ('{0:N1}s' -f $Seconds)
}

function Get-UiConsoleWidth {
    # 每次重算：操作员可能在跑的时候把窗口拖宽/拉窄。
    # 拿不到宽度时（没有真控制台、$Host.UI 不给）用 100 这个保守值：
    # 乐观估计会让状态行折行，折行就毁掉 `r 原地刷新，宁可比实际窄。
    if (-not (Get-UiInteractive)) { return 100 }
    try {
        $w = [int]$Host.UI.RawUI.WindowSize.Width
        if ($w -lt 40) { return 40 }
        if ($w -gt 200) { return 200 }
        return $w
    } catch { return 100 }
}

function Get-UiInteractive {
    # 明确要求：重定向或没有真实控制台时不玩 `r 把戏。
    if ($env:AGENTBOX_UI_MODE -eq 'plain') { return $false }
    if ($env:AGENTBOX_UI_MODE -eq 'live') { return $true }
    if ($script:UiPlain) { return $false }
    try { if (-not $Host.UI.RawUI) { return $false } } catch { return $false }
    return $true
}

# ------------------------------------------------------------------ 日志文件
function Initialize-UiRun {
    [CmdletBinding()]
    param(
        [string]$LogName = 'run',
        [string]$RepoRoot = '',
        [string]$Title = 'agentbox'
    )
    if (-not $RepoRoot) { $RepoRoot = (Get-Location).Path }
    $script:UiRepoRoot = $RepoRoot
    $script:UiTitle = $Title
    $script:UiLogDir = Join-Path $RepoRoot 'var\logs'
    $script:UiStepIndex = 0
    $script:UiRecords = New-Object System.Collections.ArrayList
    $script:UiBarState = @{}
    $script:UiFailed = $false
    $script:UiExited = $false
    $script:UiLastStatusText = ''
    $script:UiLastLogAt = [datetime]::MinValue

    try {
        # python 的 doctor 表格是 UTF-8（带框线字符）；Windows PowerShell 默认按 GBK
        # 解码原生命令输出，不设这一行表格里全是 U+FFFD。结束时在 Exit-Ui 还原。
        $script:UiOldOutputEncoding = [Console]::OutputEncoding
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        # Console.OutputEncoding 管不了 python：它的 sys.stdout.encoding 只看
        # PYTHONIOENCODING/控制台代码页，中文 Windows 上是 gbk，框线字符和 … 会变成
        # U+FFFD（数据在那一步就丢了，PowerShell 这边救不回来）。Exit-Ui 里还原。
        $script:UiOldPythonIoEncoding = $env:PYTHONIOENCODING
        $env:PYTHONIOENCODING = 'utf-8'
    } catch { }
    try {
        if (-not (Test-Path -LiteralPath $script:UiLogDir)) {
            New-Item -ItemType Directory -Force -Path $script:UiLogDir | Out-Null
        }
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $script:UiLogPath = Join-Path $script:UiLogDir "$LogName-$stamp.log"
        # UTF8Encoding($false) = 不带 BOM：既保证中文正确，又不给 diff/工具添乱。
        $script:UiStream = New-Object System.IO.StreamWriter($script:UiLogPath, $false, (New-Object System.Text.UTF8Encoding($false)))
        $script:UiStream.AutoFlush = $true
    } catch {
        $script:UiLogPath = $null
        $script:UiStream = $null
    }
    return $script:UiLogPath
}

function Write-UiLog([string]$Text) {
    if (-not $script:UiStream) { return }
    try {
        $script:UiStream.WriteLine(('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $Text))
    } catch { }
}

function Write-UiRecord {
    # -Console host|stdout|none：屏幕上打哪里去；日志文件永远写整行。
    [CmdletBinding()]
    param(
        # 允许空行（计时表里的分隔行），所以这里不能用 Mandatory。
        [string]$Text = '',
        [ValidateSet('host', 'stdout', 'none')][string]$Console = 'host',
        [string]$Color = '',
        [switch]$Log
    )
    if ($Console -eq 'host') {
        if ($Color) { Write-Host $Text -ForegroundColor $Color } else { Write-Host $Text }
    } elseif ($Console -eq 'stdout') {
        # 走控制台 stdout 而不是 Write-Output：调用方不会被这条显示语句污染返回值。
        [Console]::Out.WriteLine($Text)
        [Console]::Out.Flush()
    }
    if ($Log) { Write-UiLog $Text }
}

# ------------------------------------------------------------------ 横幅
$script:UiBannerFont = @{
    'A' = @(' █████╗ ', '██╔══██╗', '███████║', '██╔══██║', '██║  ██║', '╚═╝  ╚═╝')
    'B' = @('██████╗ ', '██╔══██╗', '██████╔╝', '██╔══██╗', '██████╔╝', '╚═════╝ ')
    'E' = @('███████╗', '██╔════╝', '█████╗  ', '██╔══╝  ', '███████╗', '╚══════╝')
    'G' = @(' ██████╗ ', '██╔════╝ ', '██║  ███╗', '██║   ██║', '╚██████╔╝', ' ╚═════╝ ')
    'N' = @('███╗   ██╗', '████╗  ██║', '██╔██╗ ██║', '██║╚██╗██║', '██║ ╚████║', '╚═╝  ╚═══╝')
    'O' = @(' ██████╗ ', '██╔═══██╗', '██║   ██║', '██║   ██║', '╚██████╔╝', ' ╚═════╝ ')
    'X' = @('██╗  ██╗', '╚██╗██╔╝', ' ╚███╔╝ ', ' ██╔██╗ ', '██╔╝ ██╗', '╚═╝  ╚═╝')
}

function Format-UiBlockWord([string]$Word) {
    $rows = @('', '', '', '', '', '')
    foreach ($ch in $Word.ToUpperInvariant().ToCharArray()) {
        $glyph = $script:UiBannerFont["$ch"]
        if (-not $glyph) { $glyph = @('      ', '      ', '      ', '      ', '      ', '      ') }
        for ($i = 0; $i -lt 6; $i++) { $rows[$i] = $rows[$i] + $glyph[$i] }
    }
    return $rows
}

function Show-UiBanner {
    [CmdletBinding()]
    param(
        [string]$Title = '',
        [string]$Subtitle = '',
        [string]$LogPath = '',
        [string]$Version = ''
    )
    if (-not $Title) { $Title = $script:UiTitle }
    if (-not $LogPath) { $LogPath = $script:UiLogPath }
    $live = Get-UiInteractive

    # 只在实时模式打块状字：重定向/CI 里 6 行美术字纯属噪声。
    if ($live) {
        $rows = Format-UiBlockWord 'AGENTBOX'
        foreach ($row in $rows) { Write-Host "  $row" -ForegroundColor Cyan }
    }
    $head = '  agentbox'
    if ($Version) { $head += " $Version" }
    $head += " · $Title"
    Write-UiRecord -Text $head -Console $(if ($live) { 'host' } else { 'stdout' }) -Color White -Log
    if ($Subtitle) { Write-UiRecord -Text "  $Subtitle" -Console $(if ($live) { 'host' } else { 'stdout' }) -Color DarkGray -Log }
    Write-UiRecord -Text "  时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  仓库: $($script:UiRepoRoot)" -Console $(if ($live) { 'host' } else { 'stdout' }) -Color DarkGray -Log
    Write-UiRecord -Text "  完整日志：$LogPath" -Console $(if ($live) { 'host' } else { 'stdout' }) -Color Yellow -Log
}

# ------------------------------------------------------------------ 进度条
function Format-UiBar([int]$Percent, [int]$Width) {
    if ($Percent -lt 0) { $Percent = 0 }
    if ($Percent -gt 100) { $Percent = 100 }
    if ($Width -lt 10) { $Width = 10 }
    $filled = [int][math]::Floor($Width * $Percent / 100)
    if ($filled -gt $Width) { $filled = $Width }
    return ('█' * $filled) + ('░' * ($Width - $filled))
}

function Write-UiLiveLine([string]$Text) {
    if (-not (Get-UiInteractive)) { return }
    $pad = ''
    if ($script:UiLiveLineLen -gt $Text.Length) { $pad = ' ' * ($script:UiLiveLineLen - $Text.Length) }
    # `r 回到行首 + 补齐空格：上一行永远被完全覆盖，不会换行成垃圾。
    Write-Host ("`r" + $Text + $pad) -NoNewline
    $script:UiLiveLineLen = $Text.Length
}

function Clear-UiLiveLine {
    if (-not (Get-UiInteractive)) { $script:UiLiveLineLen = 0; return }
    if ($script:UiLiveLineLen -gt 0) {
        Write-Host ("`r" + (' ' * $script:UiLiveLineLen) + "`r") -NoNewline
        $script:UiLiveLineLen = 0
    }
}

function Format-UiStatusLine {
    param(
        [int]$Index,
        [int]$Total,
        [string]$Title,
        [int]$Percent,
        [string]$Status,
        [double]$ElapsedSec,
        [bool]$ShowElapsed
    )
    if ($Percent -lt 0) { $Percent = 0 }
    if ($Percent -gt 100) { $Percent = 100 }
    $width = Get-UiConsoleWidth
    $tail = '  {0}%' -f $Percent

    $suffix = ''
    if ($Status) { $suffix = "  ·  $Status" }
    if ($ShowElapsed) { $suffix += "  ($(Get-UiElapsedText $ElapsedSec))" }

    $head = '   [{0}/{1}] {2}' -f $Index, $Total, $Title
    $tailW = Get-UiDisplayWidth $tail

    # 预算：整行 = head + ' [' + bar + ']' + tail + 状态文字，必须 <= $width。
    # 顺序：先压进度条（10..40 格）-> 再截状态文字 -> 最后截标题。
    # 绝不能折行：折行会毁掉 `r 原地刷新，屏幕上就成垃圾了。
    $barWidth = [int][math]::Floor(($width - (Get-UiDisplayWidth $head) - $tailW - (Get-UiDisplayWidth $suffix) - 4) / 2)
    if ($barWidth -gt 40) { $barWidth = 40 }
    if ($barWidth -lt 10) { $barWidth = 10 }

    $headRoom = $width - $tailW - $barWidth - 4
    if ($headRoom -lt 10) { $headRoom = 10 }

    # 窄窗口下 head 自己就超了：先砍标题（保留 "[n/total] " 前缀）。
    if ((Get-UiDisplayWidth $head) -gt $headRoom) {
        $prefix = '   [{0}/{1}] ' -f $Index, $Total
        $prefixW = Get-UiDisplayWidth $prefix
        $kept = ''
        foreach ($ch in $Title.ToCharArray()) {
            if (($prefixW + (Get-UiDisplayWidth ($kept + $ch))) -gt ($headRoom - 1)) { break }
            $kept += $ch
        }
        $head = $prefix + $kept.TrimEnd() + '…'
    }

    $budget = $headRoom - (Get-UiDisplayWidth $head) + 1   # 1 = ']' 与状态文字之间的空格
    if ($budget -lt 0) { $budget = 0 }
    if ($budget -eq 0) {
        $suffix = ''
    } elseif ((Get-UiDisplayWidth $suffix) -gt $budget) {
        $kept = ''
        foreach ($ch in $suffix.ToCharArray()) {
            if ((Get-UiDisplayWidth ($kept + $ch)) -gt ($budget - 1)) { break }
            $kept += $ch
        }
        $suffix = $kept.TrimEnd() + '…'
    }
    return $head + ' [' + (Format-UiBar $Percent $barWidth) + ']' + $tail + $suffix
}

# ------------------------------------------------------------------ 步骤
function Set-UiSteps {
    param([string[]]$Titles)
    $script:UiSteps = @($Titles)
}

# Start-UiStep 里用它兜底：万一 Set-UiSteps 漏了一格，计时表也不会出现 [8/7] 这种编号。
function Set-UiStepTotal {
    param([int]$Total)
    if ($script:UiSteps.Count -lt $Total) {
        $script:UiSteps = @($script:UiSteps) + @('…') * ($Total - $script:UiSteps.Count)
    }
}

function Add-UiRecord {
    param(
        [string]$Index, [string]$Title, [string]$Status,
        [double]$Seconds, [string]$Detail,
        [switch]$Sub, [string]$Parent = ''
    )
    $null = $script:UiRecords.Add([pscustomobject]@{
        Index   = $Index
        Title   = $Title
        Status  = $Status
        Seconds = $Seconds
        Detail  = $Detail
        Sub     = [bool]$Sub
        Parent  = $Parent
    })
}

# 子步骤：同一个大步骤里分段计时（“等待 systemctl active” / “等待 :8090/health”），
# 结束时在计时表里以缩进行挂在所属步骤下面，专门用来说明时间花在哪一段。
function Start-UiSubStep {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Title)
    $script:UiSubTitle = $Title
    $script:UiSubStart = Get-Date
    Update-UiStep -Status $Title
}

function Complete-UiSubStep {
    [CmdletBinding()]
    param([string]$Detail = '')
    $elapsed = 0.0
    if ($script:UiSubStart) { $elapsed = ((Get-Date) - $script:UiSubStart).TotalSeconds }
    $script:UiSubStart = $null
    if ($script:UiSubTitle) {
        Add-UiRecord -Index '' -Title $script:UiSubTitle -Status 'ok' -Seconds $elapsed -Detail $Detail `
            -Sub -Parent "$($script:UiStepIndex)/$($script:UiSteps.Count)"
        Write-UiLog ("         └─ {0} · {1}{2}" -f $script:UiSubTitle, (Format-UiDuration $elapsed), $(if ($Detail) { " · $Detail" } else { '' }))
        $script:UiSubTitle = ''
    }
    return [double]$elapsed
}

function Start-UiStep {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int]$Index,
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][int]$Total
    )
    $script:UiStepIndex = $Index
    $script:UiStepTitle = $Title
    $script:UiStepStart = Get-Date
    $script:UiStepStatus = ''
    $script:UiLastStatusText = ''
    $script:UiLastLogAt = [datetime]::MinValue
    $script:UiBarState = @{}
    $script:UiLiveLineLen = 0
    $script:UiSubTitle = ''
    $script:UiSubStart = $null
    Set-UiStepTotal -Total $Total

    # 实时模式先单独打标题（进度行马上盖在它下面）；
    # 纯文本模式没有光标，标题本身就是这一行。
    $header = '   [{0}/{1}] {2}' -f $Index, $Total, $Title
    if (Get-UiInteractive) {
        Write-UiRecord -Text $header -Console host -Color Cyan -Log
    } else {
        Write-UiRecord -Text $header -Console stdout -Log
    }
}

function Get-UiStepElapsed {
    if (-not $script:UiStepStart) { return 0.0 }
    return ((Get-Date) - $script:UiStepStart).TotalSeconds
}

function Update-UiStep {
    [CmdletBinding()]
    param(
        [int]$Percent = -1,
        [string]$Status = '',
        [switch]$NoElapsed
    )
    $live = Get-UiInteractive
    $elapsed = Get-UiStepElapsed
    if ($Percent -lt 0) {
        $key = "step$($script:UiStepIndex)"
        if ($script:UiBarState.ContainsKey($key)) { $Percent = $script:UiBarState[$key] } else { $Percent = 0 }
    }
    $script:UiBarState["step$($script:UiStepIndex)"] = $Percent
    $showElapsed = -not $NoElapsed
    if ($Status) { $script:UiStepStatus = $Status }

    # 日志节流：实时模式下状态会每几百毫秒变一次，全写进文件会把日志淹掉；
    # 文字变了就记，没变至少 15 秒记一次，所以日志里永远是“这一步当时在等什么”。
    if ($Status -or $Percent -ge 100) {
        $now = Get-Date
        $changed = ($Status -ne $script:UiLastStatusText)
        if ($changed -or ($now - $script:UiLastLogAt).TotalSeconds -ge 15) {
            $line = '       ..   [{0}%] {1}' -f $Percent, $script:UiStepStatus
            Write-UiRecord -Text $line -Console $(if ($live) { 'none' } else { 'stdout' }) -Log
            $script:UiLastStatusText = $Status
            $script:UiLastLogAt = $now
        }
    }

    if ($live) {
        $text = Format-UiStatusLine -Index $script:UiStepIndex -Total ([int]$script:UiSteps.Count) `
            -Title $script:UiStepTitle -Percent $Percent -Status $script:UiStepStatus `
            -ElapsedSec $elapsed -ShowElapsed $showElapsed
        Write-UiLiveLine $text
    }
}

function Get-UiStepLine {
    param([string]$Mark, [string]$Color, [string]$Detail, [switch]$KeepLive)
    $elapsed = Get-UiStepElapsed
    $suffix = ''
    if ($Detail) { $suffix = "  ·  $Detail" }
    $line = '   [{0}/{1}] {2}   {3} · {4}{5}' -f $script:UiStepIndex, ([int]$script:UiSteps.Count),
            $script:UiStepTitle, $Mark, (Format-UiDuration $elapsed), $suffix
    if (Get-UiInteractive -or $KeepLive) {
        # 一整行一次写完（`r 回行首 + 补齐空格 + 换行），所以不会出现半行残留。
        $pad = ''
        if ($script:UiLiveLineLen -gt $line.Length) { $pad = ' ' * ($script:UiLiveLineLen - $line.Length) }
        Write-Host ("`r" + $line + $pad) -ForegroundColor $Color
        $script:UiLiveLineLen = 0
    } else {
        [Console]::Out.WriteLine($line)
        [Console]::Out.Flush()
    }
    Write-UiLog $line
    # 关键：函数里先用 Write-Output 再用 return，返回值会变成 Object[]
    # （$elapsed 传进 [double] 参数就炸）。这里直接写 stdout，只留一个返回值出口。
    return [double]$elapsed
}

function Complete-UiStep {
    [CmdletBinding()]
    param([string]$Detail = '')
    $elapsed = Get-UiStepLine -Mark '✔ 完成' -Color Green -Detail $Detail
    Add-UiRecord -Index "$($script:UiStepIndex)/$($script:UiSteps.Count)" -Title $script:UiStepTitle -Status 'ok' -Seconds $elapsed -Detail $Detail
    $script:UiStepStart = $null
}

function Skip-UiStep {
    [CmdletBinding()]
    param([string]$Detail = '')
    $elapsed = Get-UiStepLine -Mark '○ 跳过' -Color Yellow -Detail $Detail
    Add-UiRecord -Index "$($script:UiStepIndex)/$($script:UiSteps.Count)" -Title $script:UiStepTitle -Status 'skip' -Seconds $elapsed -Detail $Detail
    $script:UiStepStart = $null
}

function Fail-UiStep {
    [CmdletBinding()]
    param([string]$Detail = '')
    $script:UiFailed = $true
    $elapsed = Get-UiStepLine -Mark '✘ 失败' -Color Red -Detail $Detail
    Add-UiRecord -Index "$($script:UiStepIndex)/$($script:UiSteps.Count)" -Title $script:UiStepTitle -Status 'fail' -Seconds $elapsed -Detail $Detail
    $script:UiStepStart = $null
}

function Stop-UiStepLine {
    # 长时间交互（比如进入 chat）时收掉进度行，但不把这一步算成已完成。
    if (Get-UiInteractive) {
        $line = '       ▶ {0}' -f $script:UiStepTitle
        $pad = ''
        if ($script:UiLiveLineLen -gt $line.Length) { $pad = ' ' * ($script:UiLiveLineLen - $line.Length) }
        Write-Host ("`r" + $line + $pad)
        $script:UiLiveLineLen = 0
    }
    $script:UiStepStart = $null
}

function Write-UiLine {
    # 日志写整行；调用方决定屏幕怎么显示（原地刷新或直接打）。
    param([string]$Line, [string]$Color = '', [switch]$Live)
    if ($Live -and (Get-UiInteractive)) { Write-UiLiveLine $Line } else { Write-Host $Line -ForegroundColor $Color }
    Write-UiLog $Line
}

function Write-UiOk { param([string]$Text, [switch]$Live) Write-UiLine -Line "       ok   $Text" -Color Green -Live:$Live }
function Write-UiNote { param([string]$Text, [switch]$Live) Write-UiLine -Line "       ..   $Text" -Color DarkGray -Live:$Live }
function Write-UiWarn { param([string]$Text, [switch]$Live) Write-UiLine -Line "       !    $Text" -Color Yellow -Live:$Live }
function Write-UiBad { param([string]$Text, [switch]$Live) Write-UiLine -Line "       FAIL $Text" -Color Red -Live:$Live }
function Write-UiRaw {
    # 外部命令的原样输出（journalctl、doctor 摘要等），进日志但保持缩进。
    param([string]$Text = '', [string]$Color = '')
    Write-UiRecord -Text $Text -Console $(if (Get-UiInteractive) { 'host' } else { 'stdout' }) -Color $Color -Log
}

function Stop-UiAgent {
    # 失败收尾：打印提示、计时表、日志位置，然后由调用方 exit。
    [CmdletBinding()]
    param([string]$Text, [string]$Hint = '')
    if ($Text) { Write-UiBad $Text }
    if ($Hint) { Write-UiRecord -Text "       提示: $Hint" -Console $(if (Get-UiInteractive) { 'host' } else { 'stdout' }) -Color Yellow -Log }
    Complete-UiRun -Failed
}

# ------------------------------------------------------------------ 计时表
function Get-UiTimingTable {
    $lines = New-Object System.Collections.ArrayList
    $null = $lines.Add('')
    $null = $lines.Add('  步骤耗时')
    $null = $lines.Add('  ' + ('-' * 66))
    $null = $lines.Add('  ' + (Add-UiPadding '步骤' 6) + (Add-UiPadding '耗时' 10) + (Add-UiPadding '状态' 8) + '说明')
    $total = 0.0
    foreach ($rec in $script:UiRecords) {
        if ($rec.Sub) { continue }   # 子项紧跟父步骤渲染
        $mark = switch ($rec.Status) {
            'ok' { '✔' }
            'fail' { '✘' }
            'skip' { '○' }
            default { '·' }
        }
        $total += $rec.Seconds
        $title = $rec.Title
        if ((Get-UiDisplayWidth $title) -gt 28) {
            while ((Get-UiDisplayWidth $title) -gt 27 -and $title.Length -gt 1) { $title = $title.Substring(0, $title.Length - 1) }
            $title += '…'
        }
        $detail = $rec.Detail
        $null = $lines.Add('  ' + (Add-UiPadding $rec.Index 6) + (Add-UiPadding (Format-UiDuration $rec.Seconds) 10) +
            (Add-UiPadding "$mark $($rec.Status)" 8) + $title + $(if ($detail) { " · $detail" } else { '' }))
        # 该步骤的子项：缩进 + └─，耗时不再计入合计（已经算在父步骤里了）。
        foreach ($sub in @($script:UiRecords | Where-Object { $_.Sub -and $_.Parent -eq $rec.Index })) {
            $subTitle = $sub.Title
            if ((Get-UiDisplayWidth $subTitle) -gt 26) {
                while ((Get-UiDisplayWidth $subTitle) -gt 25 -and $subTitle.Length -gt 1) { $subTitle = $subTitle.Substring(0, $subTitle.Length - 1) }
                $subTitle += '…'
            }
            $subDetail = $sub.Detail
            $null = $lines.Add('  ' + (Add-UiPadding '└─' 6) + (Add-UiPadding (Format-UiDuration $sub.Seconds) 10) +
                (Add-UiPadding '✔' 8) + $subTitle + $(if ($subDetail) { " · $subDetail" } else { '' }))
        }
    }
    $null = $lines.Add('  ' + ('-' * 66))
    $null = $lines.Add('  ' + (Add-UiPadding '合计' 6) + (Add-UiPadding (Format-UiDuration $total) 10))
    return $lines
}

function Complete-UiRun {
    [CmdletBinding()]
    param([switch]$Failed)
    if ($Failed) { $script:UiFailed = $true }
    $live = Get-UiInteractive
    $table = Get-UiTimingTable
    foreach ($line in $table) {
        Write-UiRecord -Text ([string]$line) -Console $(if ($live) { 'host' } else { 'stdout' }) -Log
    }
    $verdict = '✔ 成功'
    $color = 'Green'
    if ($script:UiFailed) { $verdict = '✘ 失败'; $color = 'Red' }
    Write-UiRecord -Text "  结果: $verdict" -Console $(if ($live) { 'host' } else { 'stdout' }) -Color $color -Log
    Write-UiRecord -Text "  var\logs 里的完整日志：$($script:UiLogPath)" -Console $(if ($live) { 'host' } else { 'stdout' }) -Color Yellow -Log
    Write-UiRecord -Text "  （日志文件里含每一步的中间状态和耗时，出问题先看它）" -Console $(if ($live) { 'host' } else { 'stdout' }) -Color DarkGray -Log
}

# ------------------------------------------------------------------ 光标
function Show-UiCursor {
    if (-not $script:UiExited) {
        if ($script:UiCursorHidden) {
            try { [void][UiNative]::ShowConsoleCursor($true) } catch { }
            $script:UiCursorHidden = $false
        }
        $script:UiLiveLineLen = 0
    }
    $script:UiExited = $true
}

function Hide-UiCursor {
    if (-not (Get-UiInteractive)) { return }
    try {
        [void][UiNative]::ShowConsoleCursor($false)
        $script:UiCursorHidden = $true
    } catch { }
}

function Exit-Ui {
    Show-UiCursor
    if ($script:UiOldOutputEncoding) {
        try { [Console]::OutputEncoding = $script:UiOldOutputEncoding } catch { }
        $script:UiOldOutputEncoding = $null
    }
    if ($script:UiOldPythonIoEncoding -ne $null) {
        $env:PYTHONIOENCODING = $script:UiOldPythonIoEncoding
    } else {
        Remove-Item Env:\PYTHONIOENCODING -ErrorAction SilentlyContinue
    }
    $script:UiOldPythonIoEncoding = $null
    if ($script:UiStream) {
        try { $script:UiStream.Flush(); $script:UiStream.Dispose() } catch { }
        $script:UiStream = $null
    }
}

# 只在真的用得上时加载 P/Invoke：ConstrainedLanguage 下 Add-Type 会失败，
# 那种环境下光标本来也不用管（不是交互式控制台）。
if (Get-UiInteractive) {
    try {
        if (-not ('UiNative' -as [type])) {
            Add-Type -Namespace '' -Name 'UiNative' -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
private static extern System.IntPtr GetStdHandle(int nStdHandle);
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
private static extern bool SetConsoleCursorInfo(System.IntPtr hConsoleOutput, ref CONSOLE_CURSOR_INFO lpConsoleCursorInfo);
[System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
private struct CONSOLE_CURSOR_INFO { public uint dwSize; public bool bVisible; }
public static bool ShowConsoleCursor(bool visible) {
    System.IntPtr handle = GetStdHandle(-11);
    if (handle == System.IntPtr.Zero) { return false; }
    CONSOLE_CURSOR_INFO info = new CONSOLE_CURSOR_INFO();
    info.dwSize = 25;
    info.bVisible = visible;
    return SetConsoleCursorInfo(handle, ref info);
}
'@
        }
    } catch { }
}
