$ErrorActionPreference = "Continue"

$baseDir = "C:\ProgramData\Coriolis"
$firstBootDir = Join-Path $baseDir "firstboot"

$serviceScriptsDir = Join-Path $firstBootDir "service"
$userScriptsDir = Join-Path $firstBootDir "user"

$completionFile = Join-Path $baseDir "firstboot-complete"

$logFile = Join-Path $firstBootDir "runner.log"

function Invoke-FirstBootScripts {
    param(
        [string]$ScriptDir
    )

    if (-not (Test-Path $ScriptDir)) {
        Write-Host "Script directory does not exist: $ScriptDir"
        return
    }

    Get-ChildItem -Path $ScriptDir -Filter *.ps1 -File | Sort-Object Name |
        ForEach-Object {
            $scriptPath = $_.FullName
            Write-Host "Invoking script: $scriptPath"
            try {
                & powershell.exe `
                    -NonInteractive `
                    -NoProfile `
                    -ExecutionPolicy Bypass `
                    -File $scriptPath
                Write-Host "Exit code: $LASTEXITCODE"
            }
            catch {
                Write-Host "Script failed: $scriptPath"
                Write-Host $_
            }
        }
}

# Exit immediately if the first boot task already completed.
if (Test-Path $completionFile) {
    Write-Host "First boot task already completed."
    exit 0
}

mkdir -Path $firstBootDir -Force

Start-Transcript -Path $logFile -Append

try {
    # Run Coriolis provided scripts.
    Invoke-FirstBootScripts -ScriptDir $serviceScriptsDir

    # Run user provided scripts.
    Invoke-FirstBootScripts -ScriptDir $userScriptsDir

    # Mark completion.
    New-Item `
        -ItemType File `
        -Path $completionFile `
        -Force

    Write-Host "First boot task completed successfully."
}
finally {
    Stop-Transcript
}
