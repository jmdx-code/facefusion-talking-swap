$ErrorActionPreference = 'Stop'
$packageRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$url = 'http://127.0.0.1:7860'
$logsDir = Join-Path $packageRoot 'userdata\logs'
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
$pidFile = Join-Path $logsDir '.facefusion.pid'

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

if (Test-LocalWebReady $url 1500) {
    Start-Process $url
    Write-Host '检测到 FaceFusion 已经在运行，已打开现有网页。'
    exit 0
}

$launcher = Join-Path $packageRoot 'run-facefusion-portable.ps1'
if (-not (Test-Path -LiteralPath $launcher)) {
    throw '便携版文件不完整。请先完整解压 ZIP，再从解压后的文件夹启动。'
}

Set-Content -LiteralPath $pidFile -Value $PID -Encoding ascii

$faceFusionArgs = @(
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

try {
    & $launcher -FaceFusionArgs $faceFusionArgs
}
finally {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}
