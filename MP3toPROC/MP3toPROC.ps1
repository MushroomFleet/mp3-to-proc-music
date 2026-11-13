<#
.SYNOPSIS
    MP3toPROC - convert a folder of songs into procedural game music (intro / loop / outro).

.EXAMPLE
    .\MP3toPROC.ps1 -Setup
    .\MP3toPROC.ps1 -InputDir "D:\Music\MyTracks" -OutputDir "D:\Game\Audio\Music"
    .\MP3toPROC.ps1 -InputDir .\songs -OutputDir .\proc -Format wav -Jobs 4 -SkipExisting
    .\MP3toPROC.ps1 -Preview -OutputDir .\proc -Script .\examples\events.example.json
    .\MP3toPROC.ps1 -Play -OutputDir .\proc
#>
[CmdletBinding()]
param(
    [string]$InputDir,
    [string]$OutputDir = ".\proc_out",
    [ValidateSet("ogg", "wav", "flac")] [string]$Format = "ogg",
    [ValidateSet("ogg", "wav", "flac")] [string[]]$AlsoFormat = @(),
    [int]$Jobs = 0,
    [double]$Crossfade = 4.0,
    [int]$PhraseBars = 4,
    [string]$Overrides,
    [switch]$SkipExisting,
    [switch]$Setup,
    [switch]$Preview,
    [string]$Script = "$PSScriptRoot\examples\events.example.json",
    [switch]$Play,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Venv = Join-Path $Root ".venv"

function Find-Python {
    if ($Python) { return $Python }
    $venvPy = Join-Path $Venv "Scripts\python.exe"
    if (Test-Path $venvPy) { return $venvPy }
    $venvPyNix = Join-Path $Venv "bin/python"
    if (Test-Path $venvPyNix) { return $venvPyNix }
    foreach ($c in @("py", "python", "python3")) {
        if (Get-Command $c -ErrorAction SilentlyContinue) { return $c }
    }
    throw "Python 3.9+ not found. Install from https://www.python.org/downloads/ (tick 'Add to PATH')."
}

function Invoke-Py([string[]]$PyArgs) {
    $py = Find-Python
    Push-Location $Root
    try {
        & $py -m mp3toproc @PyArgs
        if ($LASTEXITCODE -ne 0) { throw "mp3toproc exited with code $LASTEXITCODE" }
    } finally { Pop-Location }
}

if ($Setup) {
    $py = Find-Python
    Write-Host "Creating virtual environment in $Venv ..." -ForegroundColor Cyan
    & $py -m venv $Venv
    $vpy = if (Test-Path (Join-Path $Venv "Scripts\python.exe")) { Join-Path $Venv "Scripts\python.exe" } else { Join-Path $Venv "bin/python" }
    & $vpy -m pip install --upgrade pip
    & $vpy -m pip install -r (Join-Path $Root "requirements.txt") sounddevice
    Write-Host "Setup complete." -ForegroundColor Green
    if (-not $InputDir -and -not $Preview -and -not $Play) { exit 0 }
}

$OutputDir = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $OutputDir))

if ($Preview) {
    $st = Join-Path $OutputDir "soundtrack.json"
    $wav = Join-Path $OutputDir "preview.wav"
    Invoke-Py @("preview", $st, (Resolve-Path $Script).Path, "-o", $wav)
    Write-Host "Preview: $wav" -ForegroundColor Green
    exit 0
}

if ($Play) {
    Invoke-Py @("play", (Join-Path $OutputDir "soundtrack.json"))
    exit 0
}

if (-not $InputDir) { throw "Specify -InputDir (folder of songs), or -Setup / -Preview / -Play." }
$InputDir = (Resolve-Path $InputDir).Path
if ($Jobs -le 0) { $Jobs = [Math]::Max(1, [int]([Environment]::ProcessorCount / 2)) }

$argsList = @("batch", $InputDir, "-o", $OutputDir, "-f", $Format, "-j", "$Jobs",
              "--crossfade", "$Crossfade", "--phrase-bars", "$PhraseBars")
if ($Overrides)    { $argsList += @("--overrides", (Resolve-Path $Overrides).Path) }
if ($SkipExisting) { $argsList += "--skip-existing" }
foreach ($f in $AlsoFormat) { $argsList += @("--also-format", $f) }

$sw = [Diagnostics.Stopwatch]::StartNew()
Invoke-Py $argsList
$sw.Stop()

# Summary table from the manifests
Get-ChildItem -Path $OutputDir -Filter track.json -Recurse | ForEach-Object {
    $m = Get-Content $_.FullName -Raw | ConvertFrom-Json
    [PSCustomObject]@{
        Track      = $m.id
        BPM        = [Math]::Round($m.bpm, 1)
        Intro      = "{0:N1}s" -f $m.segments.intro.seconds
        Loop       = "{0:N1}s ({1} bars)" -f $m.segments.loop.seconds, $m.segments.loop.bars
        Outro      = "{0:N1}s" -f $m.segments.outro.seconds
        Seam       = $m.analysis.confidence
    }
} | Format-Table -AutoSize

Write-Host ("Finished in {0:N1}s. Check each track's review.png, then edit soundtrack.json." -f $sw.Elapsed.TotalSeconds) -ForegroundColor Green
