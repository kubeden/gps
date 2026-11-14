# Windows: copy the addon into WoW Forever and prepare the helper environment.
$ErrorActionPreference = "Stop"
$WowDir = if ($env:WOW_DIR) { $env:WOW_DIR } else { "C:\Program Files (x86)\World of Warcraft\_forever_" }
$Addons = Join-Path $WowDir "Interface\AddOns"
New-Item -ItemType Directory -Force -Path $Addons | Out-Null
$Target = Join-Path $Addons "GamepadSpeak"
if (Test-Path $Target) { Remove-Item -Recurse -Force $Target }
Copy-Item -Recurse (Join-Path $PSScriptRoot "addon\GamepadSpeak") $Target
Write-Host "Addon copied: $Target"
if (Get-Command uv -ErrorAction SilentlyContinue) {
  Push-Location (Join-Path $PSScriptRoot "helper"); uv sync --quiet; Pop-Location
  Write-Host "Helper environment ready (uv)."
} else {
  Write-Host "uv not found; .\run-helper.ps1 will create a venv with pip on first run."
}
Write-Host "Next: in game run /gps setup, then .\run-helper.ps1"
