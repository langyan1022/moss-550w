param([Parameter(Mandatory=$true)][string]$Target)
$ErrorActionPreference = 'Stop'
chcp 65001 > $null
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()
Add-Type -AssemblyName System.Speech
$speaker = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $speaker.SelectVoice('Microsoft Huihui Desktop')
    $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000,16,1)
    $speaker.SetOutputToWaveFile($Target,$format)
    $speaker.Speak([string]::Concat([char]0x6253,[char]0x5f00,[char]0x5a92,[char]0x4f53,[char]0x5e93))
}
finally { $speaker.Dispose() }
