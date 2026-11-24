param([ValidateSet('inventory', 'status', 'pause', 'resume', 'next', 'previous')][string]$Action = 'status')
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

function Write-Result($value) { [Console]::WriteLine(($value | ConvertTo-Json -Depth 5 -Compress)) }

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $managerType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime]
    $metadataType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties,Windows.Media.Control,ContentType=WindowsRuntime]
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and
        $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    function Await-Operation($operation, [Type]$type) {
        $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
        if (-not $task.Wait(2500)) { throw [TimeoutException]::new('Media session request timed out') }
        $task.GetAwaiter().GetResult()
    }
    function Read-Session($session) {
        $playback = $session.GetPlaybackInfo()
        try { $metadata = Await-Operation ($session.TryGetMediaPropertiesAsync()) $metadataType } catch { $metadata = $null }
        @{
            source = [string]$session.SourceAppUserModelId; state = [string]$playback.PlaybackStatus
            title = [string]$metadata.Title; artist = [string]$metadata.Artist; track = [int]$metadata.TrackNumber
            controls = @{pause=$playback.Controls.IsPauseEnabled; resume=$playback.Controls.IsPlayEnabled;
                         next=$playback.Controls.IsNextEnabled; previous=$playback.Controls.IsPreviousEnabled}
        }
    }
    function Test-SameMedia($data, $target) {
        $data.source -ceq $target.source -and $data.title -ceq $target.title -and
        $data.artist -ceq $target.artist -and $data.track -eq $target.track
    }
    $manager = Await-Operation ($managerType::RequestAsync()) $managerType
    $sessions = @($manager.GetSessions())
    $current = $manager.GetCurrentSession()
    if ($Action -eq 'inventory') {
        $states = @($sessions | ForEach-Object { [string]$_.GetPlaybackInfo().PlaybackStatus })
        Write-Result @{code='inventory'; count=$sessions.Count; states=$states; current=($null -ne $current)}
        exit 0
    }
    $selection = 'current'
    if ($Action -eq 'resume' -and $env:MOSS_MEDIA_RESUME_TARGET) {
        $target = $env:MOSS_MEDIA_RESUME_TARGET | ConvertFrom-Json
        $matching = @(foreach ($session in $sessions) {
            if ([string]$session.SourceAppUserModelId -ceq $target.source -and
                (Test-SameMedia (Read-Session $session) $target)) { $session }
        })
        if ($matching.Count -eq 0) { Write-Result @{code='target_missing'}; exit 0 }
        if ($matching.Count -ne 1) { Write-Result @{code='target_ambiguous'}; exit 0 }
        $current = $matching[0]
        $selection = 'paused_target'
    } elseif ($null -eq $current) {
        if ($sessions.Count -eq 0) { Write-Result @{code='none'}; exit 0 }
        if ($sessions.Count -eq 1) {
            $current = $sessions[0]; $selection = 'sole'
        } else {
            $playing = @($sessions | Where-Object { [string]$_.GetPlaybackInfo().PlaybackStatus -eq 'Playing' })
            if ($playing.Count -ne 1) { Write-Result @{code='ambiguous'; count=$sessions.Count}; exit 0 }
            $current = $playing[0]; $selection = 'playing'
        }
    }
    $before = Read-Session $current
    $result = @{code='status'; action=$Action; selection=$selection; count=$sessions.Count; before=$before}
    if ($Action -eq 'status') { Write-Result $result; exit 0 }
    if ($selection -eq 'paused_target' -and -not (Test-SameMedia $before $target)) {
        $result.code='target_missing'; Write-Result $result; exit 0
    }
    if ($selection -eq 'paused_target' -and $before.state -notin @('Paused','Playing')) {
        $result.code='target_inactive'; Write-Result $result; exit 0
    }
    if (($env:MOSS_MEDIA_EXPECT_SOURCE -and $env:MOSS_MEDIA_EXPECT_SOURCE -ne $before.source) -or
        ($env:MOSS_MEDIA_EXPECT_TITLE -and $env:MOSS_MEDIA_EXPECT_TITLE -ne $before.title)) {
        $result.code='changed'; Write-Result $result; exit 0
    }
    if (($Action -eq 'pause' -and $before.state -eq 'Paused') -or ($Action -eq 'resume' -and $before.state -eq 'Playing')) {
        $result.code='already'; Write-Result $result; exit 0
    }
    if (-not $before.controls[$Action]) { $result.code='unsupported'; Write-Result $result; exit 0 }
    $operation = switch ($Action) {
        'pause'    { $current.TryPauseAsync() }
        'resume'   { $current.TryPlayAsync() }
        'next'     { $current.TrySkipNextAsync() }
        'previous' { $current.TrySkipPreviousAsync() }
    }
    $ok = Await-Operation $operation ([bool])
    if (-not $ok) { $result.code='rejected'; Write-Result $result; exit 0 }
    $result.code='accepted'
    $watch = [Diagnostics.Stopwatch]::StartNew()
    do {
        try { $after = Read-Session $current } catch { break }
        $result.after = $after
        if (($Action -eq 'pause' -and $after.state -eq 'Paused') -or ($Action -eq 'resume' -and $after.state -eq 'Playing') -or
            ($Action -in @('next','previous') -and (
                ($before.title -and $after.title -and $before.title -ne $after.title) -or
                ($before.artist -and $after.artist -and $before.artist -ne $after.artist) -or
                ($before.track -and $after.track -and $before.track -ne $after.track)))) {
            $result.code='observed'; break
        }
        Start-Sleep -Milliseconds 90
    } while ($watch.ElapsedMilliseconds -lt 1200)
    Write-Result $result
} catch {
    Write-Result @{code='unavailable'; hresult=$_.Exception.HResult}
}
