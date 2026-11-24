param([switch] $Session)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$utf8 = [Text.UTF8Encoding]::new($false, $true)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8

function Refuse([string] $Reason) {
    $failure = [InvalidOperationException]::new('Draft handoff could not be verified.')
    $failure.Data['DraftHandoffReason'] = $Reason
    throw $failure
}

function Unique-Control($Root, [string] $AutomationId, [string] $MissingReason = 'invalid_control') {
    $condition = [Windows.Automation.PropertyCondition]::new(
        [Windows.Automation.AutomationElement]::AutomationIdProperty, $AutomationId)
    $matches = $Root.FindAll([Windows.Automation.TreeScope]::Descendants, $condition)
    if ($matches.Count -ne 1) { Refuse $MissingReason }
    return $matches[0]
}

function Resolve-Target($Request) {
    $window = [IntPtr][long]$Request.window
    if (-not [MossDraftWindowIdentity]::IsWindow($window)) { Refuse 'invalid_window' }
    [uint32] $nativePid = 0
    $null = [MossDraftWindowIdentity]::GetWindowThreadProcessId($window, [ref]$nativePid)
    if ($nativePid -ne $Request.pid) { Refuse 'invalid_process' }
    $process = Get-Process -Id $nativePid
    $image = Join-Path $PSHOME 'powershell.exe'
    if ($process.SessionId -ne $script:workerSession -or
        -not [string]::Equals($process.Path, $image, [StringComparison]::OrdinalIgnoreCase)) {
        Refuse 'invalid_process'
    }

    $root = [Windows.Automation.AutomationElement]::FromHandle($window)
    if ($root.Current.AutomationId -cne 'MossDraftWindow' -or $root.Current.ProcessId -ne $nativePid) {
        Refuse 'invalid_window'
    }
    $anchor = Unique-Control $root 'MossDraftConversationId'
    $anchorStatus = $anchor.Current.ItemStatus
    $conversation = "MOSS-DRAFT:$($Request.target_id)"
    $bound = [string]::Equals($anchorStatus, "$($conversation):RECIPIENT", [StringComparison]::Ordinal)
    if ((-not $bound -and -not [string]::Equals($anchorStatus, $conversation, [StringComparison]::Ordinal)) -or
        $anchor.Current.ProcessId -ne $nativePid) {
        Refuse 'conversation_changed'
    }
    if ($bound -ne ($null -ne $Request.PSObject.Properties['recipient'])) {
        Refuse 'recipient_changed'
    }
    if ($bound) {
        $label = Unique-Control $root 'MossJevRecipient' 'recipient_changed'
        $recipient = $label.Current.Name
        if ($label.Current.ControlType -ne [Windows.Automation.ControlType]::Text -or
            $label.Current.ProcessId -ne $nativePid -or
            -not [string]::Equals($recipient, $Request.recipient, [StringComparison]::Ordinal)) {
            Refuse 'recipient_changed'
        }
    }
    $editor = Unique-Control $root 'MossDraftEditor'
    if ($editor.Current.ControlType -ne [Windows.Automation.ControlType]::Edit -or
        $editor.Current.ProcessId -ne $nativePid -or -not $editor.Current.IsEnabled -or $editor.Current.IsPassword) {
        Refuse 'not_writable_edit'
    }
    $pattern = $null
    if (-not $editor.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$pattern) -or
        $pattern.Current.IsReadOnly) {
        Refuse 'not_writable_edit'
    }
    $identity = @{
        pid = [int]$nativePid
        window = $window.ToInt64()
        process_start = $process.StartTime.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
        editor_runtime = $editor.GetRuntimeId() -join '.'
        conversation_anchor = [string]$Request.target_id
    }
    $resolved = @{ identity = $identity; pattern = $pattern }
    if ($bound) {
        $identity['recipient'] = $recipient
        $resolved['recipient_label'] = $label
    }
    return $resolved
}

function Assert-Identity($Current, $Expected) {
    if ($null -eq $Expected) { Refuse 'missing_identity' }
    foreach ($field in ('pid', 'window', 'process_start', 'editor_runtime', 'conversation_anchor')) {
        if ($Current[$field] -cne $Expected.$field) { Refuse 'identity_changed' }
    }
    if (($null -ne $Current['recipient'] -or $null -ne $Expected.PSObject.Properties['recipient']) -and
        ($Expected.recipient -isnot [string] -or
         -not [string]::Equals($Current['recipient'], $Expected.recipient, [StringComparison]::Ordinal))) {
        Refuse 'identity_changed'
    }
}

function Read-Request([string] $RequiredMode) {
    $raw = [Console]::ReadLine()
    if ($null -eq $raw) { return }
    if ([string]::IsNullOrWhiteSpace($raw)) { Refuse 'invalid_request' }
    $request = $raw | ConvertFrom-Json
    if ($request -isnot [pscustomobject] -or $request.mode -cnotin @('arm', 'fill') -or
        $request.target_id -isnot [string] -or $request.target_id -cnotmatch '\A[0-9a-f]{32}\z' -or
        ($request.pid -isnot [int] -and $request.pid -isnot [long]) -or $request.pid -le 0 -or
        ($request.window -isnot [int] -and $request.window -isnot [long]) -or $request.window -le 0) {
        Refuse 'invalid_request'
    }
    if ($null -ne $request.PSObject.Properties['recipient'] -and
        ($request.recipient -isnot [string] -or [string]::IsNullOrWhiteSpace($request.recipient) -or
         $request.recipient -match '[\p{Cc}\p{Zl}\p{Zp}]')) {
        Refuse 'invalid_recipient'
    }
    if ($RequiredMode -and $request.mode -cne $RequiredMode) { Refuse 'invalid_sequence' }
    if ($request.mode -ceq 'fill' -and ($request.text -isnot [string] -or [string]::IsNullOrWhiteSpace($request.text))) {
        Refuse 'invalid_text'
    }
    return $request
}

function Initialize-Uia {
    $script:workerSession = (Get-Process -Id $PID).SessionId
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class MossDraftWindowIdentity {
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr window);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
}
'@
}

function Get-ArmIdentity($Request) {
    $current = Resolve-Target $Request
    if (-not [string]::Equals($current.pattern.Current.Value, '', [StringComparison]::Ordinal)) {
        Refuse 'input_not_empty'
    }
    return $current.identity
}

function Fill-Draft($Request, $ArmIdentity) {
    if ($ArmIdentity) {
        Assert-Identity $ArmIdentity $Request.expected
        if ($Request.pid -ne $ArmIdentity.pid -or $Request.window -ne $ArmIdentity.window -or
            $Request.target_id -cne $ArmIdentity.conversation_anchor) {
            Refuse 'identity_changed'
        }
    }
    $current = Resolve-Target $Request
    Assert-Identity $current.identity $Request.expected
    $bytes = $utf8.GetBytes($Request.text)
    if (-not [string]::Equals($current.pattern.Current.Value, '', [StringComparison]::Ordinal)) {
        Refuse 'input_changed'
    }
    if ($current.recipient_label -and
        -not [string]::Equals($current.recipient_label.Current.Name, $Request.recipient, [StringComparison]::Ordinal)) {
        Refuse 'recipient_changed'
    }
    $script:attemptedWrite = $true
    $current.pattern.SetValue($Request.text)
    $confirmed = Resolve-Target $Request
    Assert-Identity $confirmed.identity $Request.expected
    if (-not [string]::Equals($confirmed.pattern.Current.Value, $Request.text, [StringComparison]::Ordinal)) {
        Refuse 'readback_mismatch'
    }
    if ($confirmed.recipient_label -and
        -not [string]::Equals($confirmed.recipient_label.Current.Name, $Request.recipient, [StringComparison]::Ordinal)) {
        Refuse 'recipient_changed'
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
    return @{ status = 'filled'; sha256 = $digest; bytes = $bytes.Length }
}

function Write-Response($Response) {
    [Console]::WriteLine(($Response | ConvertTo-Json -Compress -Depth 4))
    [Console]::Out.Flush()
}

$script:attemptedWrite = $false
try {
    if ($Session) {
        Initialize-Uia
        $request = Read-Request 'arm'
    } else {
        $request = Read-Request
        if ($request) { Initialize-Uia }
    }
    if ($null -eq $request) { Refuse 'invalid_request' }
    if ($request.mode -ceq 'arm') {
        $identity = Get-ArmIdentity $request
        $result = @{ status = 'armed'; identity = $identity }
        if ($Session) {
            Write-Response $result
            $request = Read-Request 'fill'
            if ($null -eq $request) { return }
            $result = Fill-Draft $request $identity
        }
    } else {
        $result = Fill-Draft $request
    }
} catch {
    $reason = $_.Exception.Data['DraftHandoffReason']
    if ($script:attemptedWrite) {
        if ($reason -ne 'readback_mismatch') { $reason = 'unavailable_after_write' }
        $result = @{ status = 'uncertain'; reason = $reason }
    } else {
        if (-not $reason) { $reason = 'target_unavailable' }
        $result = @{ status = 'refused'; reason = $reason }
    }
}
Write-Response $result
