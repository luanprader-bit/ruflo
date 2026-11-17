# YouTube Shorts Agent - Windows-Start
#   start.bat              -> richtet alles ein, legt eine Desktop-Verknuepfung an und oeffnet die App
#   start.bat -Console     -> Version ohne Fenster (fragt im Terminal nach Key und Link)
#   start.bat "<YouTube-Link>"  -> Terminal-Version direkt mit diesem Link

param([string]$Url, [switch]$Console)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Test-Cmd([string]$Name) { return [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Stop-WithHint([string]$Message) {
    Write-Host ""
    Write-Host $Message -ForegroundColor Red
    exit 1
}

Write-Host "=== YouTube Shorts Agent ===" -ForegroundColor Cyan

# 1. Python (3.12 bevorzugt, dafuer gibt es alle Pakete)
$py = $null; $pyArgs = @()
if (Test-Cmd "py") {
    $py = "py"
    & py -3.12 --version *> $null
    if ($LASTEXITCODE -eq 0) { $pyArgs = @("-3.12") }
} elseif (Test-Cmd "python") { $py = "python" }
if (-not $py) {
    Stop-WithHint ("Python fehlt. Installieren mit:`n  winget install Python.Python.3.12`n" +
                   "Danach dieses Fenster schliessen und start.bat neu starten.")
}

# 2. ffmpeg
if (-not (Test-Cmd "ffmpeg")) {
    Stop-WithHint ("ffmpeg fehlt. Installieren mit:`n  winget install Gyan.FFmpeg`n" +
                   "Danach dieses Fenster schliessen und start.bat neu starten.")
}

# 3. Eigene Python-Umgebung - liegt bewusst ausserhalb von OneDrive
$home_ = Join-Path $env:LOCALAPPDATA "YouTubeShortsAgent"
$venv = Join-Path $home_ "venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$venvPythonW = Join-Path $venv "Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Richte Python-Umgebung ein (nur beim ersten Start)..."
    New-Item -ItemType Directory -Force -Path $home_ | Out-Null
    & $py @pyArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) { Stop-WithHint "Konnte die Python-Umgebung nicht anlegen." }
}

# 4. Pakete - neu installieren, wenn dieser Ordner oder die Version neu ist
$stamp = "v4|$PSScriptRoot"
$marker = Join-Path $home_ "installed.txt"
$installed = if (Test-Path -LiteralPath $marker) { (Get-Content -LiteralPath $marker -Raw).Trim() } else { "" }
if ($installed -ne $stamp) {
    Write-Host "Installiere Pakete - das dauert beim ersten Mal einige Minuten, bitte warten..."
    & $venvPython -m pip install --disable-pip-version-check -q --upgrade pip
    & $venvPython -m pip install --disable-pip-version-check -q -e ".[vision]"
    if ($LASTEXITCODE -ne 0) { Stop-WithHint "Installation fehlgeschlagen (siehe Meldung oben)." }
    Write-Host "Installiere Spracherkennung (fuer Videos, bei denen YouTube keine Untertitel liefert)..."
    & $venvPython -m pip install --disable-pip-version-check -q "faster-whisper>=1.0"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Spracherkennung konnte nicht installiert werden - die App laeuft trotzdem." -ForegroundColor Yellow
    }
    Set-Content -LiteralPath $marker -Value $stamp
}

# 5. App-Modus: Desktop-Verknuepfung anlegen und Fenster oeffnen
if (-not $Url -and -not $Console) {
    try {
        $desktop = [Environment]::GetFolderPath("Desktop")
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut((Join-Path $desktop "YouTube Shorts Agent.lnk"))
        $link.TargetPath = $venvPythonW
        $link.Arguments = "-m shorts_agent.gui"
        $link.WorkingDirectory = $PSScriptRoot
        $link.Description = "YouTube-Videos zusammenfassen und als Shorts schneiden"
        $link.IconLocation = "$env:SystemRoot\System32\imageres.dll,18"
        $link.Save()
        Write-Host "Desktop-Verknuepfung 'YouTube Shorts Agent' angelegt." -ForegroundColor Green
    } catch {
        Write-Host "Konnte keine Desktop-Verknuepfung anlegen: $_" -ForegroundColor Yellow
    }
    Start-Process -FilePath $venvPythonW -ArgumentList "-m", "shorts_agent.gui" -WorkingDirectory $PSScriptRoot
    Write-Host "App wird geoeffnet. Ab jetzt reicht ein Doppelklick auf die Verknuepfung auf dem Desktop."
    exit 0
}

# 6. Terminal-Modus
& $venvPython -m pip install --disable-pip-version-check -q --upgrade yt-dlp
$analyzerArgs = @()
if (-not $env:ANTHROPIC_API_KEY) {
    Write-Host ""
    Write-Host "Anthropic API-Key eingeben (beginnt mit sk-ant-)."
    Write-Host "Einfach Enter druecken = ohne Key (einfache Auswahl, schwaechere Qualitaet)."
    $secure = Read-Host "API-Key" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $key = "$key".Trim()
    if ($key) { $env:ANTHROPIC_API_KEY = $key } else { $analyzerArgs = @("--analyzer", "heuristic") }
}
if (-not $Url) {
    Write-Host ""
    $Url = Read-Host "YouTube-Link einfuegen (Rechtsklick fuegt ein)"
}
$Url = "$Url".Trim().Trim('"')
if (-not $Url) { Stop-WithHint "Kein Link eingegeben." }

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
