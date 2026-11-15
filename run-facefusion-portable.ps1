param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$FaceFusionArgs
)

$ErrorActionPreference = 'Stop'
$packageRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$sourceDir = Join-Path $packageRoot 'app\facefusion'
$python = Join-Path $packageRoot 'runtime\python\python.exe'
$ffmpegBin = Get-ChildItem (Join-Path $packageRoot 'tools\ffmpeg') -Directory -ErrorAction SilentlyContinue |
    Select-Object -First 1 |
    ForEach-Object { Join-Path $_.FullName 'bin' }
$nvidiaBins = Get-ChildItem (Join-Path $packageRoot 'runtime\python\Lib\site-packages\nvidia') -Directory -Recurse -ErrorAction SilentlyContinue |
    Where-Object Name -eq 'bin' |
    Select-Object -ExpandProperty FullName

if (-not (Test-Path -LiteralPath $python)) {
    throw "便携版 Python 不完整：$python"
}
if (-not $ffmpegBin -or -not (Test-Path -LiteralPath (Join-Path $ffmpegBin 'ffprobe.exe'))) {
    throw '便携版 FFmpeg/FFprobe 不完整。'
}

$nvcudaDll = if ($env:SystemRoot) { Join-Path $env:SystemRoot 'System32\nvcuda.dll' } else { '' }
if ((-not $nvcudaDll -or -not (Test-Path -LiteralPath $nvcudaDll)) -and (-not (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue))) {
    throw '没有检测到 NVIDIA 显卡驱动。请先安装或更新 NVIDIA 官方显卡驱动。'
}

# Isolate ALL local runtime data, auth tokens, presets, models, caches, temp files, and outputs inside userdata\
$userDataDir = Join-Path $packageRoot 'userdata'
$modelsDir = Join-Path $userDataDir 'models'
$tempDir = Join-Path $userDataDir 'temp'
$gradioTempDir = Join-Path $tempDir 'gradio'
$jobsDir = Join-Path $userDataDir 'jobs'
$manualOutputsDir = Join-Path $userDataDir 'manual_outputs'
$pyCacheDir = Join-Path $userDataDir 'pycache'
$logsDir = Join-Path $userDataDir 'logs'

foreach ($dir in @($userDataDir, $modelsDir, $tempDir, $gradioTempDir, $jobsDir, $manualOutputsDir, $pyCacheDir, $logsDir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}

$env:PATH = (@($ffmpegBin) + @($nvidiaBins) + @((Split-Path $python)) + @($env:PATH)) -join ';'
$env:FACEFUSION_USERDATA_DIR = $userDataDir
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONPYCACHEPREFIX = $pyCacheDir
$env:TEMP = $tempDir
$env:TMP = $tempDir
$env:GRADIO_TEMP_DIR = $gradioTempDir
$env:GRADIO_ANALYTICS_ENABLED = 'False'
$env:HF_HUB_DISABLE_TELEMETRY = '1'
$env:DO_NOT_TRACK = '1'
$loopbackBypass = '127.0.0.1,localhost'
$env:NO_PROXY = if ($env:NO_PROXY) { "$($env:NO_PROXY),$loopbackBypass" } else { $loopbackBypass }
$env:no_proxy = $env:NO_PROXY

if (-not $FaceFusionArgs -or $FaceFusionArgs.Count -eq 0) {
    $FaceFusionArgs = @(
        'run', '--open-browser',
        '--execution-providers', 'cuda',
        '--execution-thread-count', '2',
        '--video-memory-strategy', 'moderate',
        '--workflow-strategy', 'disk',
        '--temp-frame-format', 'jpeg',
        '--processors', 'face_swapper', 'expression_restorer', 'face_enhancer',
        '--face-selector-mode', 'one',
        '--face-swapper-model', 'inswapper_128_fp16',
        '--face-swapper-pixel-boost', '128x128',
        '--face-swapper-weight', '0.5',
        '--expression-restorer-model', 'live_portrait',
        '--expression-restorer-factor', '80',
        '--expression-restorer-areas', 'upper-face', 'lower-face',
        '--face-enhancer-model', 'gfpgan_1.4',
        '--face-enhancer-blend', '65',
        '--face-mask-types', 'box',
        '--face-mask-blur', '0.65',
        '--output-video-encoder', 'h264_nvenc',
        '--output-audio-encoder', 'aac',
        '--output-video-quality', '90'
    )
}

if ($FaceFusionArgs.Count -gt 0 -and $FaceFusionArgs[0] -eq 'run') {
    $extraPathArgs = @()
    if ($FaceFusionArgs -notcontains '--temp-path') {
        $extraPathArgs += @('--temp-path', $tempDir)
    }
    if ($FaceFusionArgs -notcontains '--jobs-path') {
        $extraPathArgs += @('--jobs-path', $jobsDir)
    }
    if (($FaceFusionArgs -notcontains '--output-path') -and ($FaceFusionArgs -notcontains '-o')) {
        $extraPathArgs += @('--output-path', $manualOutputsDir)
    }
    if ($extraPathArgs.Count -gt 0) {
        if ($FaceFusionArgs.Count -gt 1) {
            $FaceFusionArgs = @('run') + $extraPathArgs + $FaceFusionArgs[1..($FaceFusionArgs.Count - 1)]
        }
        else {
            $FaceFusionArgs = @('run') + $extraPathArgs
        }
    }
}

Push-Location $sourceDir
try {
    if ($FaceFusionArgs.Count -gt 0 -and $FaceFusionArgs[0] -eq 'run') {
        & $python -m facefusion.model_bootstrap --ensure
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }
    & $python 'facefusion.py' @FaceFusionArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
