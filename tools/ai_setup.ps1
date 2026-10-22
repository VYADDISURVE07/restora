# Restora - install the free local AI: Ollama (official installer from ollama.com) + one small model.
# Safe to run again: it skips whatever is already installed. Writes ai_setup.log next to this script.
param([string]$Model = "qwen3:4b-instruct")
$ErrorActionPreference = "Stop"
$log = Join-Path $PSScriptRoot "ai_setup.log"
function Say($m) { $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m; Write-Host $line; Add-Content -Path $log -Value $line -Encoding utf8 }
Set-Content -Path $log -Value "Restora AI setup $(Get-Date -Format s)" -Encoding utf8
try {
  $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
  if (-not (Test-Path $ollama)) {
    $setup = Join-Path $env:TEMP "OllamaSetup.exe"
    Say "Downloading Ollama from https://ollama.com/download/OllamaSetup.exe (about 1 GB, once)..."
    & curl.exe -L --fail --retry 3 --silent --show-error -o $setup "https://ollama.com/download/OllamaSetup.exe"
    if ($LASTEXITCODE -ne 0) { throw "download failed (curl exit code $LASTEXITCODE)" }
    Say ("Downloaded {0:N0} MB" -f ((Get-Item $setup).Length / 1MB))
    $sig = Get-AuthenticodeSignature $setup
    Say ("Installer signature: {0} - {1}" -f $sig.Status, $sig.SignerCertificate.Subject)
    if ($sig.Status -ne "Valid") { throw "the installer is not validly signed, so it was NOT run" }
    Say "Installing Ollama (per-user, no admin rights needed)..."
    # WaitForExit, not -Wait: the installer starts the Ollama tray app, and -Wait would wait for that app too
    $p = Start-Process -FilePath $setup -ArgumentList "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES" -PassThru
    $p.WaitForExit()
    Say "Installer finished with exit code $($p.ExitCode)"
  } else {
    Say "Ollama is already installed: $ollama"
  }
  if (-not (Test-Path $ollama)) { throw "ollama.exe not found after installing" }
  Say ("Ollama version: " + ((& $ollama --version) -join " "))
  $up = $false
  for ($i = 0; $i -lt 40; $i++) {
    try { Invoke-WebRequest -UseBasicParsing "http://localhost:11434/api/tags" -TimeoutSec 3 | Out-Null; $up = $true; break }
    catch { if ($i -eq 2) { Say "Starting the Ollama server..."; Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden }; Start-Sleep -Seconds 2 }
  }
  if (-not $up) { throw "the Ollama server did not start on http://localhost:11434" }
  Say "Ollama server is running."
  Say "Downloading the model $Model (about 2.5 GB, once). Progress shows in this window..."
  & $ollama pull $Model
  if ($LASTEXITCODE -ne 0) { throw "ollama pull failed (exit code $LASTEXITCODE)" }
  Say ("Installed models: " + ((& $ollama list) -join " | "))
  Say "Asking the model a test question..."
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $body = @{ model = $Model; stream = $false; think = $false; messages = @(@{ role = "user"; content = "Reply with exactly one word: ready" }) } | ConvertTo-Json -Depth 5
  $r = Invoke-RestMethod -Uri "http://localhost:11434/api/chat" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 600
  Say ("Model answered '{0}' in {1:N1} s (first answer includes loading the model)" -f $r.message.content.Trim(), $sw.Elapsed.TotalSeconds)
  $sw.Restart()
  $r = Invoke-RestMethod -Uri "http://localhost:11434/api/chat" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 600
  Say ("Second answer '{0}' in {1:N1} s" -f $r.message.content.Trim(), $sw.Elapsed.TotalSeconds)
  Say ("Loaded: " + ((& $ollama ps) -join " | "))
  Say "DONE"
} catch {
  Say "FAILED: $_"
}
if (-not $env:RESTORA_NO_PAUSE) { Start-Sleep -Seconds 8 }
