$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot

while ($true) {
    & py quiz_bot.py
    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 0) {
        exit 0
    }

    Write-Warning "Bot exited with code $exitCode. Restarting in 5 seconds..."
    Start-Sleep -Seconds 5
}
