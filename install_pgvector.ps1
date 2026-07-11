# install_pgvector.ps1
# This script downloads and installs pgvector for PostgreSQL 17 on Windows.
# Run this script as Administrator.

$ErrorActionPreference = "Stop"

# 1. Check if running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warning "This script MUST be run as Administrator because it copies files to C:\Program Files\"
    Write-Host "Please restart PowerShell as Administrator and run this script again." -ForegroundColor Red
    Read-Host "Press Enter to exit..."
    exit
}

# 2. Check/ask for PostgreSQL installation directory
$pgPath = "C:\Program Files\PostgreSQL\17"
if (-not (Test-Path $pgPath)) {
    Write-Host "PostgreSQL 17 was not found at standard path: $pgPath" -ForegroundColor Yellow
    $pgPath = Read-Host "Please enter your PostgreSQL installation directory (e.g. C:\Program Files\PostgreSQL\17)"
    if (-not (Test-Path $pgPath)) {
        Write-Host "Directory does not exist. Exiting." -ForegroundColor Red
        Read-Host "Press Enter to exit..."
        exit
    }
}

Write-Host "Installing pgvector for PostgreSQL at: $pgPath" -ForegroundColor Cyan

# 3. Define download URL and temp paths
$url = "https://github.com/andreiramani/pgvector_pgsql_windows/releases/download/0.8.3_17.6/vector.v0.8.3-pg17.zip"
$tempZip = Join-Path $env:TEMP "pgvector_temp.zip"
$tempDir = Join-Path $env:TEMP "pgvector_temp_extracted"

# Clean up old temp files
if (Test-Path $tempZip) { Remove-Item $tempZip -Force }
if (Test-Path $tempDir) { Remove-Item $tempDir -Recurse -Force -ErrorAction SilentlyContinue }

# 4. Download the precompiled zip file
Write-Host "Downloading pgvector from GitHub..." -ForegroundColor Green
Invoke-WebRequest -Uri $url -OutFile $tempZip

# 5. Extract files
Write-Host "Extracting files..." -ForegroundColor Green
Expand-Archive -Path $tempZip -DestinationPath $tempDir

# 6. Locate source directory (handle nested folders in ZIP if any)
$srcPath = $tempDir
if (-not (Test-Path (Join-Path $srcPath "lib"))) {
    $subDirs = Get-ChildItem $tempDir -Directory
    foreach ($d in $subDirs) {
        if (Test-Path (Join-Path $d.FullName "lib")) {
            $srcPath = $d.FullName
            break
        }
    }
}

# 7. Copy files to PostgreSQL installation directory
Write-Host "Copying extension files..." -ForegroundColor Green

# Copy bin folder contents
if (Test-Path (Join-Path $srcPath "bin")) {
    Copy-Item -Path (Join-Path $srcPath "bin\*") -Destination (Join-Path $pgPath "bin") -Force -Recurse
}

# Copy lib folder contents
if (Test-Path (Join-Path $srcPath "lib")) {
    Copy-Item -Path (Join-Path $srcPath "lib\*") -Destination (Join-Path $pgPath "lib") -Force -Recurse
}

# Copy share folder contents
if (Test-Path (Join-Path $srcPath "share")) {
    Copy-Item -Path (Join-Path $srcPath "share\*") -Destination (Join-Path $pgPath "share") -Force -Recurse
}

# 8. Clean up temp files
Remove-Item $tempZip -Force -ErrorAction SilentlyContinue
Remove-Item $tempDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host "pgvector has been installed successfully!" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""
Write-Host "You can now run: CREATE EXTENSION IF NOT EXISTS vector;"
Write-Host "inside your PostgreSQL 'ragdb' database."
Write-Host ""
Read-Host "Press Enter to exit..."
