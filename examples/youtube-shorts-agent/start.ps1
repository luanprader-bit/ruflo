# YouTube Shorts Agent - Start fuer Windows (PowerShell)
# Doppelklick auf start.bat, oder in PowerShell:  .\start.bat
# Optional direkt mit Link:                         .\start.bat "https://www.youtube.com/watch?v=..."

param([string]$Url)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Test-Cmd([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Stop-WithHint([string]$Message) {
    Write-Host ""
    Write-Host $Message -ForegroundColor Red
    exit 1
}

Write-Host "=== YouTube Shorts Agent ===" -ForegroundColor Cyan

# 1. Python
$python = $null
if (Test-Cmd "py") { $python = "py" }
elseif (Test-Cmd "python") { $python = "python" }
if (-not $python) {
    Stop-WithHint ("Python fehlt. Installieren mit:`n  winget install Python.Python.3.12`n" +
                   "Danach dieses Fenster schliessen und start.bat neu starten.")
}

# 2. ffmpeg
if (-not (Test-Cmd "ffmpeg")) {
    Stop-WithHint ("ffmpeg fehlt. Installieren mit:`n  winget install Gyan.FFmpeg`n" +
                   "Danach dieses Fenster schliessen und start.bat neu starten.")
}

# 3. Eigene Python-Umgebung im Ordner .venv (einmalig)
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Richte Python-Umgebung ein (nur beim ersten Start, dauert 1-3 Minuten)..."
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Stop-WithHint "Konnte die Python-Umgebung nicht anlegen." }
}

$marker = Join-Path $PSScriptRoot ".venv\installed.txt"
if (-not (Test-Path -LiteralPath $marker)) {
    Write-Host "Installiere Pakete..."
    & $venvPython -m pip install --disable-pip-version-check -q --upgrade pip
    & $venvPython -m pip install --disable-pip-version-check -q -e ".[vision]"
    if ($LASTEXITCODE -ne 0) { Stop-WithHint "Installation fehlgeschlagen (siehe Meldung oben)." }
    Set-Content -LiteralPath $marker -Value (Get-Date -Format s)
} else {
    # YouTube aendert sich oft - yt-dlp aktuell halten.
    & $venvPython -m pip install --disable-pip-version-check -q --upgrade yt-dlp
}

# 4. API-Key (optional)
$analyzerArgs = @()
if (-not $env:ANTHROPIC_API_KEY) {
    Write-Host ""
    Write-Host "Anthropic API-Key eingeben (beginnt mit sk-ant-)."
    Write-Host "Einfach Enter druecken = ohne Key (einfache Auswahl, schwaechere Qualitaet)."
    $secure = Read-Host "API-Key" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $key = $key.Trim()
    if ($key) {
        $env:ANTHROPIC_API_KEY = $key
        $save = Read-Host "Key fuer spaeter speichern? (j/n)"
        if ($save -match "^[jJyY]") {
            [Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", $key, "User")
            Write-Host "Gespeichert - beim naechsten Start wird nicht mehr gefragt."
        }
    } else {
        $analyzerArgs = @("--analyzer", "heuristic")
    }
}

# 5. Video-Link
if (-not $Url) {
    Write-Host ""
    $Url = Read-Host "YouTube-Link einfuegen (Rechtsklick fuegt ein)"
}
$Url = $Url.Trim().Trim('"')
if (-not $Url) { Stop-WithHint "Kein Link eingegeben." }

# 6. Los
Write-Host ""
& $venvPython -m shorts_agent.cli $Url @analyzerArgs
$code = $LASTEXITCODE

$out = Join-Path $PSScriptRoot "out"
if ($code -eq 0 -and (Test-Path -LiteralPath $out)) {
    Write-Host ""
    Write-Host "Fertig! Ergebnisse liegen in: $out" -ForegroundColor Green
    Start-Process explorer.exe -ArgumentList "`"$out`""
}
exit $code
