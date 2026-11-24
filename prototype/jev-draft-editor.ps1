param(
    [Parameter(Mandatory = $true)] [string] $TargetId,
    [switch] $Offscreen,
    [switch] $RecipientBound
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
if ($TargetId -cnotmatch '\A[0-9a-f]{32}\z') { throw 'Invalid draft target identifier.' }
if ($RecipientBound) {
    [Console]::InputEncoding = [Text.UTF8Encoding]::new($false, $true)
    $line = [Console]::ReadLine()
    if ([string]::IsNullOrWhiteSpace($line) -or -not $line.TrimStart().StartsWith('"', [StringComparison]::Ordinal)) {
        throw 'Invalid draft recipient.'
    }
    $recipient = $line | ConvertFrom-Json
    if ($recipient -isnot [string] -or [string]::IsNullOrWhiteSpace($recipient) -or
        $recipient -match '[\p{Cc}\p{Zl}\p{Zp}]') {
        throw 'Invalid draft recipient.'
    }
}

Add-Type -AssemblyName PresentationFramework, PresentationCore
$window = [Windows.Window]::new()
$window.Title = 'MOSS · 本机草稿'
$window.Width = 550
$window.Height = 370
$window.MinWidth = 320
$window.MinHeight = 240
$window.WindowStartupLocation = 'CenterScreen'
[Windows.Automation.AutomationProperties]::SetAutomationId($window, 'MossDraftWindow')

if ($Offscreen) {
    [Windows.Media.RenderOptions]::ProcessRenderMode = [Windows.Interop.RenderMode]::SoftwareOnly
    $window.WindowStartupLocation = 'Manual'
    $window.Left = -12000
    $window.Top = -12000
    $window.ShowActivated = $false
    $window.ShowInTaskbar = $false
    $window.Opacity = 0.03
}

$panel = [Windows.Controls.DockPanel]::new()
$panel.Margin = [Windows.Thickness]::new(14)
$guide = [Windows.Controls.TextBlock]::new()
$guide.Text = if ($RecipientBound) {
    '请核对收件人和草稿正文。选中文字后按 Ctrl+C 复制。'
} else {
    '核对或编辑正文。选中文字后按 Ctrl+C 复制。'
}
$guide.TextWrapping = 'Wrap'
$guide.Margin = [Windows.Thickness]::new(0, 0, 0, 10)
[Windows.Automation.AutomationProperties]::SetAutomationId($guide, 'MossDraftConversationId')
$anchorStatus = "MOSS-DRAFT:$TargetId"
if ($RecipientBound) { $anchorStatus += ':RECIPIENT' }
[Windows.Automation.AutomationProperties]::SetItemStatus($guide, $anchorStatus)
[Windows.Controls.DockPanel]::SetDock($guide, 'Top')
$null = $panel.Children.Add($guide)

if ($RecipientBound) {
    $recipientLabel = [Windows.Controls.TextBlock]::new()
    $recipientLabel.Text = $recipient
    $recipientLabel.TextWrapping = 'Wrap'
    $recipientLabel.Margin = [Windows.Thickness]::new(0, 0, 0, 10)
    [Windows.Automation.AutomationProperties]::SetAutomationId($recipientLabel, 'MossJevRecipient')
    [Windows.Controls.DockPanel]::SetDock($recipientLabel, 'Top')
    $null = $panel.Children.Add($recipientLabel)
}

$editor = [Windows.Controls.TextBox]::new()
$editor.AcceptsReturn = $true
$editor.TextWrapping = 'Wrap'
$editor.VerticalScrollBarVisibility = 'Auto'
$editor.Padding = [Windows.Thickness]::new(8)
[Windows.Automation.AutomationProperties]::SetAutomationId($editor, 'MossDraftEditor')
[Windows.Automation.AutomationProperties]::SetName($editor, '草稿正文')
$null = $panel.Children.Add($editor)
$window.Content = $panel

$script:readyEmitted = $false
$window.Add_Loaded({
    if ($script:readyEmitted) { return }
    $script:readyEmitted = $true
    $handle = [Windows.Interop.WindowInteropHelper]::new($window).Handle
    $ready = @{ status = 'ready'; window = $handle.ToInt64(); pid = $PID }
    [Console]::WriteLine(($ready | ConvertTo-Json -Compress))
})

$application = [Windows.Application]::new()
$null = $application.Run($window)
