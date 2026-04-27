# PotsWorks PisoWifi — Windows PowerShell Build Wrapper
# For development on Windows — runs the build inside WSL or Docker
#
# Usage:
#   .\build.ps1 [orangepi1|orangepipc|orangepizero3|all]

param(
    [string]$Board = "orangepizero3"
)

$ValidBoards = @("orangepi1", "orangepipc", "orangepizero3", "all")
if ($Board -notin $ValidBoards) {
    Write-Error "Unknown board: $Board. Valid: $($ValidBoards -join ', ')"
    exit 1
}

Write-Host "[build] PotsWorks PisoWifi Image Builder (Windows)" -ForegroundColor Cyan
Write-Host "[build] Board: $Board" -ForegroundColor Cyan

# Check if WSL is available
$wslAvailable = $null
try {
    $wslAvailable = Get-Command wsl -ErrorAction SilentlyContinue
} catch {}

if ($wslAvailable) {
    Write-Host "[build] Using WSL..." -ForegroundColor Green
    $scriptPath = (Get-Location).Path -replace '\\', '/'
    $wslPath = "/mnt/" + $scriptPath.Substring(0,1).ToLower() + $scriptPath.Substring(2)
    wsl bash -c "cd '$wslPath' && chmod +x build.sh && ./build.sh $Board"
} else {
    # Check if Docker is available
    $dockerAvailable = $null
    try {
        $dockerAvailable = Get-Command docker -ErrorAction SilentlyContinue
    } catch {}

    if ($dockerAvailable) {
        Write-Host "[build] Using Docker..." -ForegroundColor Green
        docker run --rm -it `
            -v "${PWD}:/workspace" `
            -w /workspace `
            ubuntu:22.04 `
            bash -c "apt-get update -qq && apt-get install -y git && ./build.sh $Board"
    } else {
        Write-Host "[build] ERROR: Neither WSL nor Docker found." -ForegroundColor Red
        Write-Host "[build] Please install WSL2 or Docker Desktop to build images on Windows."
        Write-Host "[build] Alternatively, run build.sh directly on a Linux machine."
        exit 1
    }
}
