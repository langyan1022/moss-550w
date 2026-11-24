param([switch]$Batch)
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and
    $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Wait-WinRt($operation, [Type] $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    $task.GetAwaiter().GetResult()
}
$ocr = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $ocr) { throw 'MOSS_OCR_LANGUAGE' }
function Read-OcrText($buffer) {
    $stream = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($buffer)
    try {
        $decoder = Wait-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Wait-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime])
        try {
            $result = Wait-WinRt ($ocr.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime])
            return ($result.Lines | ForEach-Object { $_.Text }) -join "`n"
        } finally { ([IDisposable]$bitmap).Dispose() }
    } finally { ([IDisposable]$stream).Dispose() }
}
if ($Batch) {
    $reader = [IO.StreamReader]::new([Console]::OpenStandardInput(), [Text.Encoding]::ASCII)
    while ($null -ne ($line = $reader.ReadLine())) {
        $buffer = [IO.MemoryStream]::new([Convert]::FromBase64String($line), $false)
        try { [Console]::WriteLine((ConvertTo-Json -InputObject ([string](Read-OcrText $buffer)) -Compress)) }
        finally { $buffer.Dispose() }
    }
} else {
    $buffer = [IO.MemoryStream]::new()
    try {
        [Console]::OpenStandardInput().CopyTo($buffer)
        $buffer.Position = 0
        [Console]::WriteLine((Read-OcrText $buffer))
    } finally { $buffer.Dispose() }
}
