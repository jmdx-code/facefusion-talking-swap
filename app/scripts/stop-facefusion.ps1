$ErrorActionPreference = 'Stop'
# File lives at `<ROOT>\app\scripts\stop-facefusion.ps1`
$packageRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$portablePython = [System.IO.Path]::GetFullPath((Join-Path $packageRoot 'runtime\python\python.exe'))
$portableToolsDir = [System.IO.Path]::GetFullPath((Join-Path $packageRoot 'tools\ffmpeg')) + [System.IO.Path]::DirectorySeparatorChar
$pidFile = Join-Path $packageRoot 'userdata\logs\.facefusion.pid'
$legacyPidFile = Join-Path $packageRoot '.facefusion.pid'

$stoppedAny = $false

$ownPyProcs = Get-Process -Name 'python' -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and [string]::Equals([System.IO.Path]::GetFullPath($_.Path), $portablePython, [System.StringComparison]::OrdinalIgnoreCase) }

foreach ($proc in $ownPyProcs) {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    $stoppedAny = $true
}

$ownMediaProcs = Get-Process -Name @('ffmpeg', 'ffprobe') -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and ([System.IO.Path]::GetFullPath($_.Path)).StartsWith($portableToolsDir, [System.StringComparison]::OrdinalIgnoreCase) }

foreach ($mproc in $ownMediaProcs) {
    Stop-Process -Id $mproc.Id -Force -ErrorAction SilentlyContinue
    $stoppedAny = $true
}

foreach ($candidatePidFile in @($pidFile, $legacyPidFile)) {
    if (Test-Path -LiteralPath $candidatePidFile) {
        $savedText = Get-Content -LiteralPath $candidatePidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($savedText -match '^\d+$' -and ([int]$savedText -ne $PID)) {
            $wrapper = Get-Process -Id ([int]$savedText) -ErrorAction SilentlyContinue |
                Where-Object { $_.ProcessName -in @('powershell', 'pwsh', 'python') }
            if ($wrapper) {
                Stop-Process -Id $wrapper.Id -Force -ErrorAction SilentlyContinue
                $stoppedAny = $true
            }
        }
        Remove-Item -LiteralPath $candidatePidFile -Force -ErrorAction SilentlyContinue
    }
}

if (-not $stoppedAny) {
    Write-Host '没有找到正在运行的 FaceFusion。'
    exit 0
}

Write-Host 'FaceFusion 已停止。'
