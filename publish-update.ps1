param(
    [string]$Version = '',
    [string]$Message = ''
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath($PSScriptRoot)
Set-Location $root

$versionFile = Join-Path $root 'VERSION'
$currentVer = if (Test-Path -LiteralPath $versionFile) { (Get-Content -LiteralPath $versionFile -Raw).Trim().TrimStart('v', 'V') } else { '1.0.0' }

if (-not $Version) {
    $parts = $currentVer.Split('.')
    if ($parts.Count -ge 3 -and ($parts[2] -match '^\d+$')) {
        $nextPatch = [int]$parts[2] + 1
        $Version = "$($parts[0]).$($parts[1]).$nextPatch"
    }
    else {
        $Version = "$currentVer.1"
    }
}
$Version = $Version.Trim().TrimStart('v', 'V')
$tag = "v$Version"

if (-not $Message) {
    $Message = "chore(release): publish $tag hot update"
}

Write-Host ">>> 1/4 更新版本号：$currentVer -> $Version"
Set-Content -LiteralPath $versionFile -Value "$Version`n" -NoNewline -Encoding utf8

Write-Host ">>> 2/4 生成轻量热更新包 (~0.4 MB) 与极简便携初始包 (~14 MB)..."
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
& "$root\runtime\python\python.exe" "$root\create-portable-zip.py"
if ($LASTEXITCODE -ne 0) {
    throw '打包失败，已中止发布。'
}

Write-Host ">>> 3/4 隐私安全核验并推送源码至 GitHub..."
git add .
$staged = git diff --cached --name-only
$bad = $staged | Where-Object { $_ -match '^(userdata|runtime|tools)/' -or $_ -match '\.(onnx|part|zip|pyc|log)$' -or $_ -match 'client_secret|google-token|sheet-worker|presets\.json' }
if ($bad) {
    git reset
    throw "安全拦截：检测到疑似个人或大体积文件进入暂存区：$($bad -join ', ')"
}

if ($staged) {
    git commit -m $Message
}
git push origin main

Write-Host ">>> 4/4 发布 GitHub Release $tag（客户端启动将自动秒级热更新）..."
$dist = Join-Path $root 'userdata\dist'
$updZip = Join-Path $dist 'FaceFusion-Update-CodeOnly.zip'
$updSha = Join-Path $dist 'FaceFusion-Update-CodeOnly.zip.sha256.txt'
$fullZip = Join-Path $dist 'FaceFusion-Portable-Windows-NVIDIA.zip'
$fullSha = Join-Path $dist 'FaceFusion-Portable-Windows-NVIDIA.zip.sha256.txt'

$notes = @"
## FaceFusion 口播换脸 $tag

- **老用户无需手动下载**：直接重启本地 `后台启动FaceFusion.cmd`（或在网页右上角点击 **🔄 检查在线热更新**），客户端将自动下载 `0.4 MB` 热更新包秒级完成升级，个人配置与已下载模型 100% 保留。
- **新用户首次安装**：请下载下方 **`FaceFusion-Portable-Windows-NVIDIA.zip`**（约 `14 MB` 极简启动包），解压后双击 `后台启动FaceFusion.cmd` 即可全自动从官方国际源完成环境与模型初始化。
"@

gh release create $tag `
    --title "FaceFusion 口播换脸 $tag" `
    --notes $notes `
    $fullZip $fullSha $updZip $updSha

Write-Host ""
Write-Host "✅ 发布成功！所有已安装客户端下次打开将自动升级到 $tag。"
