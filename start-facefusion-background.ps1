$ErrorActionPreference = 'Stop'
$packageRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$portablePython = [System.IO.Path]::GetFullPath((Join-Path $packageRoot 'runtime\python\python.exe'))
$autoUpdater = Join-Path $packageRoot 'auto_updater.py'
$envBootstrap = Join-Path $packageRoot 'env_bootstrap.py'
$logsDir = Join-Path $packageRoot 'userdata\logs'
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
$pidFile = Join-Path $logsDir '.facefusion.pid'
$logFile = Join-Path $logsDir 'facefusion.log'
$errorLog = Join-Path $logsDir 'facefusion-error.log'

function Test-PortableRunning([int]$SavedPid) {
    $ownPy = Get-Process -Name 'python' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and [string]::Equals([System.IO.Path]::GetFullPath($_.Path), $portablePython, [System.StringComparison]::OrdinalIgnoreCase) }
    if ($ownPy) { return $true }
    if ($SavedPid -gt 0) {
        $wrapper = Get-Process -Id $SavedPid -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessName -in @('powershell', 'pwsh', 'python') }
        if ($wrapper) { return $true }
    }
    return $false
}

function Test-LocalWebReady([string]$Uri, [int]$TimeoutMs = 1500) {
    try {
        $req = [System.Net.WebRequest]::Create($Uri)
        $req.Proxy = $null
        $req.Timeout = $TimeoutMs
        $resp = [System.Net.HttpWebResponse]$req.GetResponse()
        try {
            return ([int]$resp.StatusCode -eq 200)
        }
        finally {
            $resp.Close()
        }
    }
    catch {
        return $false
    }
}

if (Test-Path -LiteralPath $pidFile) {
    $savedPidText = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    $savedPidNum = if ($savedPidText -match '^\d+$') { [int]$savedPidText } else { 0 }
    if ($savedPidNum -gt 0 -and (Test-PortableRunning $savedPidNum) -and (Test-LocalWebReady 'http://127.0.0.1:7860' 1500)) {
        Start-Process 'http://127.0.0.1:7860'
        Write-Host "FaceFusion 已在运行，PID: $savedPidNum"
        exit 0
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

if (Test-LocalWebReady 'http://127.0.0.1:7860' 1500) {
    Start-Process 'http://127.0.0.1:7860'
    Write-Host '检测到 7860 端口已有服务运行，已打开现有网页。'
    exit 0
}

# Rotate redirected log files if larger than 10 MB to prevent unbounded disk growth
$maxLogBytes = 10MB
foreach ($targetLog in @($logFile, $errorLog)) {
    if (Test-Path -LiteralPath $targetLog) {
        $item = Get-Item -LiteralPath $targetLog -ErrorAction SilentlyContinue
        if ($item -and $item.Length -ge $maxLogBytes) {
            $rotated = "$targetLog.old"
            Move-Item -LiteralPath $targetLog -Destination $rotated -Force -ErrorAction SilentlyContinue
        }
    }
}

# Visible console preflight BEFORE spawning hidden background server:
# 1) Scheme A: Automatic silent 0.4 MB code hot-update from official GitHub Releases
# 2) Scheme C: First-run FFmpeg + Python/CUDA runtime bootstrap from official GitHub/PyPI
# 3) First-run AI .onnx model bootstrap from official HuggingFace/GitHub
$sourceDir = Join-Path $packageRoot 'app\facefusion'
$userDataDir = Join-Path $packageRoot 'userdata'
$pyCacheDir = Join-Path $userDataDir 'pycache'
New-Item -ItemType Directory -Path (Join-Path $userDataDir 'models'), $pyCacheDir -Force | Out-Null
$env:FACEFUSION_USERDATA_DIR = $userDataDir
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONPYCACHEPREFIX = $pyCacheDir

if (Test-Path -LiteralPath $autoUpdater) {
    & $portablePython -B $autoUpdater --startup-check
}

if (Test-Path -LiteralPath $envBootstrap) {
    & $portablePython -B $envBootstrap --check-only
    if ($LASTEXITCODE -eq 10) {
        & $portablePython -B $envBootstrap --ensure
        if ($LASTEXITCODE -ne 0) {
            throw '首次运行初始化运行环境未完成，请检查网络连接后重新双击启动（支持断点续传）。'
        }
    }
}

Push-Location $sourceDir
try {
    & $portablePython -m facefusion.model_bootstrap --check-only
    if ($LASTEXITCODE -eq 10) {
        Write-Host ''
        Write-Host '>>> [环境初始化 3/3] 正在校验并自动下载口播换脸所需 AI 模型...'
        & $portablePython -m facefusion.model_bootstrap --ensure
        if ($LASTEXITCODE -ne 0) {
            throw '首次运行自动下载模型未完成，请检查网络连接后重新双击启动（支持断点续传）。'
        }
    }
}
finally {
    Pop-Location
}

Write-Host '正在后台启动 FaceFusion 服务，启动完成后将自动打开浏览器...'
$launcher = Join-Path $packageRoot 'run-facefusion-portable.ps1'
$argumentLine = '-NoProfile -ExecutionPolicy Bypass -File "' + $launcher + '" -SkipPreflight run --execution-providers cuda --execution-thread-count 2 --video-memory-strategy moderate --workflow-strategy disk --temp-frame-format jpeg --processors face_swapper expression_restorer face_enhancer --face-selector-mode one --face-swapper-model inswapper_128_fp16 --face-swapper-pixel-boost 128x128 --face-swapper-weight 0.5 --expression-restorer-model live_portrait --expression-restorer-factor 80 --expression-restorer-areas upper-face lower-face --face-enhancer-model gfpgan_1.4 --face-enhancer-blend 65 --face-mask-types box --face-mask-blur 0.65 --output-video-encoder h264_nvenc --output-audio-encoder aac --output-video-quality 90'
$process = Start-Process -FilePath 'powershell.exe' -ArgumentList $argumentLine -WorkingDirectory $packageRoot -WindowStyle Hidden -RedirectStandardOutput $logFile -RedirectStandardError $errorLog -PassThru
Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii

for ($attempt = 0; $attempt -lt 300; $attempt++) {
    if ($process.HasExited) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        throw "FaceFusion 启动进程已异常退出（代码 $($process.ExitCode)），请查看：$errorLog"
    }
    if (Test-LocalWebReady 'http://127.0.0.1:7860' 1500) {
        Start-Process 'http://127.0.0.1:7860'
        Write-Host "FaceFusion 已启动，PID: $($process.Id)"
        exit 0
    }
    Start-Sleep -Seconds 1
}

throw "FaceFusion 未在 300 秒内启动，请查看：$errorLog"
