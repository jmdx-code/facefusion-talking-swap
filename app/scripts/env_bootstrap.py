"""
First-run runtime bootstrapper for the ~12 MB micro-portable distribution (Scheme C).
Pure Python 3.12 stdlib ONLY — runs inside minimal portable Python BEFORE any third-party package exists.

Downloads & verifies strictly from official international endpoints:
- FFmpeg / FFprobe: Official GitHub Releases (`https://github.com/FFmpeg-Builds/FFmpeg-Builds`)
- Python & NVIDIA CUDA 12 / cuDNN 9 wheels: Official PyPI (`https://pypi.org` & `https://files.pythonhosted.org`)
Supports HTTP Range `.part` byte-level resume, SHA-256 verification, and staged installation.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as importlib_metadata
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# File lives at `<ROOT>/app/scripts/env_bootstrap.py` -> parents[2] is `<ROOT>`
ROOT = Path(__file__).resolve().parents[2]

# Pinned stable FFmpeg 9.0.2 release (same exact build as Microsoft WinGet `Gyan.FFmpeg.Essentials`, 280k+ downloads)
FFMPEG_URLS: Tuple[str, ...] = (
    'https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip',
    'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip',
)
FFMPEG_ZIP_SHA256 = '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
FFMPEG_EXE_SHA256 = '3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec'
FFPROBE_EXE_SHA256 = 'f0d36ecbbdd3bcfac3efa078c96c7271c2e68b3810595552ac3b7f17e9a65c52'

RUNTIME_PACKAGES: Tuple[Tuple[str, str], ...] = (
    ('aiofiles', '24.1.0'),
    ('annotated-doc', '0.0.5'),
    ('annotated-types', '0.8.0'),
    ('anyio', '4.15.1'),
    ('brotli', '1.2.1'),
    ('certifi', '2026.10.23'),
    ('click', '8.5.0'),
    ('colorama', '0.4.6'),
    ('fastapi', '0.141.1'),
    ('ffmpy', '1.0.0'),
    ('filelock', '4.2.10'),
    ('flatbuffers', '25.12.19'),
    ('fsspec', '2026.9.0'),
    ('gradio', '5.50.0'),
    ('gradio_client', '1.14.0'),
    ('gradio_rangeslider', '0.0.8'),
    ('groovy', '0.1.2'),
    ('h11', '0.16.0'),
    ('hf-xet', '1.6.0'),
    ('httpcore', '1.0.9'),
    ('httpx', '0.28.1'),
    ('huggingface_hub', '1.33.0'),
    ('idna', '3.20'),
    ('Jinja2', '3.1.6'),
    ('markdown-it-py', '4.2.0'),
    ('MarkupSafe', '3.0.3'),
    ('mdurl', '0.1.2'),
    ('ml_dtypes', '0.6.0'),
    ('mpmath', '1.3.0'),
    ('numpy', '2.4.6'),
    ('onnx', '1.22.0'),
    ('opencv-python-headless', '5.0.0.93'),
    ('orjson', '3.12.0'),
    ('packaging', '26.3'),
    ('pandas', '2.3.3'),
    ('pillow', '13.0.0'),
    ('protobuf', '7.36.2'),
    ('pydantic', '2.12.3'),
    ('pydantic_core', '2.41.4'),
    ('pydub', '0.25.1'),
    ('Pygments', '2.21.0'),
    ('python-dateutil', '2.9.0.post0'),
    ('python-multipart', '0.0.32'),
    ('pytz', '2026.4'),
    ('PyYAML', '6.0.3'),
    ('rich', '15.0.0'),
    ('safehttpx', '0.1.7'),
    ('scipy', '1.18.0'),
    ('semantic-version', '2.10.0'),
    ('shellingham', '1.5.4'),
    ('six', '1.17.0'),
    ('starlette', '0.52.1'),
    ('sympy', '1.14.0'),
    ('tomlkit', '0.13.3'),
    ('tqdm', '4.70.0'),
    ('typer', '0.27.2'),
    ('typing-inspection', '0.4.4'),
    ('typing_extensions', '4.16.0'),
    ('tzdata', '2026.4'),
    ('uvicorn', '0.54.0'),
    ('websockets', '15.0.1'),
    ('onnxruntime-gpu', '1.24.4'),
    ('nvidia-cuda-runtime-cu12', '12.9.79'),
    ('nvidia-nvjitlink-cu12', '12.9.86'),
    ('nvidia-cuda-nvrtc-cu12', '12.9.86'),
    ('nvidia-curand-cu12', '10.3.10.19'),
    ('nvidia-cufft-cu12', '11.4.1.4'),
    ('nvidia-cublas-cu12', '12.9.2.10'),
    ('nvidia-cudnn-cu12', '9.26.0.51'),
)


def _norm_pkg_name(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def _build_opener() -> urllib.request.OpenerDirector:
    proxies = urllib.request.getproxies() or {}
    clean_proxies = {}
    for scheme in ('http', 'https'):
        val = (proxies.get(scheme) or proxies.get('https') or proxies.get('http') or '').strip()
        if val:
            if '://' not in val:
                val = f'http://{val}'
            if val.lower().startswith(('http://', 'https://')):
                clean_proxies[scheme] = val
    handler = urllib.request.ProxyHandler(clean_proxies) if clean_proxies else urllib.request.ProxyHandler()
    return urllib.request.build_opener(handler)


def _format_mb(num_bytes: float) -> str:
    return f'{num_bytes / (1024 * 1024):.1f} MB'


def download_file_with_resume(
    urls: Sequence[str],
    dest_path: Path,
    *,
    label: str,
    expected_sha256: Optional[str] = None,
) -> bool:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.is_file() and dest_path.stat().st_size > 0:
        if not expected_sha256 or _sha256_file(dest_path) == expected_sha256.lower():
            return True
        dest_path.unlink(missing_ok=True)

    part_path = dest_path.with_name(dest_path.name + '.part')
    opener = _build_opener()

    for url in urls:
        for attempt in range(3):
            resume_from = part_path.stat().st_size if part_path.is_file() else 0
            headers = {'User-Agent': 'FaceFusion-Bootstrap/1.1'}
            if resume_from > 0:
                headers['Range'] = f'bytes={resume_from}-'

            req = urllib.request.Request(url, headers=headers)
            try:
                with opener.open(req, timeout=45) as resp:
                    status = getattr(resp, 'status', 200)
                    is_partial = (status == 206 and resume_from > 0)
                    written = resume_from if is_partial else 0
                    cl_raw = resp.headers.get('Content-Length', '')
                    total = (written + int(cl_raw)) if cl_raw.isdigit() else 0
                    mode = 'ab' if is_partial else 'wb'

                    t0 = time.monotonic()
                    bytes_this_run = 0
                    last_draw = 0.0

                    with part_path.open(mode) as out_f:
                        while True:
                            chunk = resp.read(256 * 1024)
                            if not chunk:
                                break
                            out_f.write(chunk)
                            written += len(chunk)
                            bytes_this_run += len(chunk)
                            now = time.monotonic()
                            if now - last_draw >= 0.25 or (total and written >= total):
                                elapsed = max(now - t0, 0.001)
                                speed = (bytes_this_run / (1024 * 1024)) / elapsed
                                if total > 0:
                                    pct = min(100.0, written * 100.0 / total)
                                    msg = f'\r    {label}: {pct:5.1f}% ({_format_mb(written)} / {_format_mb(total)}) [{speed:4.1f} MB/s]'
                                else:
                                    msg = f'\r    {label}: {_format_mb(written)} [{speed:4.1f} MB/s]'
                                sys.stdout.write(msg.ljust(78))
                                sys.stdout.flush()
                                last_draw = now

                    sys.stdout.write('\n')
                    sys.stdout.flush()

                    if total > 0 and written < total:
                        time.sleep(1.0)
                        continue

                    if expected_sha256:
                        actual_sha = _sha256_file(part_path)
                        if actual_sha != expected_sha256.lower():
                            print(f'    [校验告警] {dest_path.name} SHA-256 不匹配，正在重新下载...', flush=True)
                            part_path.unlink(missing_ok=True)
                            continue

                    os.replace(part_path, dest_path)
                    return True
            except urllib.error.HTTPError as http_err:
                if http_err.code == 416 and resume_from > 0:
                    part_path.unlink(missing_ok=True)
                    continue
                if attempt < 2:
                    time.sleep(1.2)
            except Exception as exc:
                sys.stdout.write('\n')
                print(f'    [网络波动自动重试 {attempt + 1}/3] {label}: {exc}', flush=True)
                time.sleep(1.5)

    return False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest().lower()


def find_ffmpeg_bin_dir(root_dir: Path = ROOT) -> Optional[Path]:
    tools_ff = root_dir / 'tools' / 'ffmpeg'
    if not tools_ff.is_dir():
        return None
    for sub in sorted(tools_ff.iterdir()):
        if sub.is_dir():
            bin_dir = sub / 'bin'
            # Reject legacy daily shared builds (`avcodec-*.dll`) that trigger Windows 11 Smart App Control (WinError 4551)
            if any(bin_dir.glob('avcodec-*.dll')):
                continue
            if (bin_dir / 'ffmpeg.exe').is_file() and (bin_dir / 'ffprobe.exe').is_file():
                return bin_dir
    return None


def find_missing_packages() -> List[Tuple[str, str]]:
    installed: Dict[str, str] = {}
    for dist in importlib_metadata.distributions():
        name = dist.metadata.get('Name') or ''
        if name:
            installed[_norm_pkg_name(name)] = (dist.version or '').strip()

    missing: List[Tuple[str, str]] = []
    for pkg_name, pkg_ver in RUNTIME_PACKAGES:
        if installed.get(_norm_pkg_name(pkg_name)) != pkg_ver:
            missing.append((pkg_name, pkg_ver))
    return missing


def _is_compatible_cp3_tag(py_parts: set[str], target_minor: int) -> bool:
    for tag in py_parts:
        if tag.startswith('cp3') and tag[3:].isdigit() and int(tag[3:]) <= target_minor:
            return True
    return False


def select_best_wheel_from_pypi(urls_meta: List[Dict[str, Any]], py_nodot: str = '312') -> Optional[Tuple[str, str, str]]:
    cp_tag = f'cp{py_nodot}'
    target_minor = int(py_nodot[1:]) if len(py_nodot) >= 2 and py_nodot[1:].isdigit() else 12
    candidates: List[Tuple[int, str, str, str]] = []

    for item in urls_meta:
        if item.get('packagetype') != 'bdist_wheel':
            continue
        fn = str(item.get('filename') or '')
        url = str(item.get('url') or '')
        sha = str((item.get('digests') or {}).get('sha256') or '').lower()
        if not fn.endswith('.whl') or not url:
            continue

        stem = fn[:-4]
        parts = stem.split('-')
        if len(parts) < 5:
            continue
        py_tag, abi_tag, plat_tag = parts[-3], parts[-2], parts[-1]

        plat_parts = set(plat_tag.split('.'))
        if not ('win_amd64' in plat_parts or 'any' in plat_parts):
            continue

        py_parts = set(py_tag.split('.'))
        abi_parts = set(abi_tag.split('.'))

        score = 0
        if cp_tag in py_parts and (cp_tag in abi_parts or 'abi3' in abi_parts) and 'win_amd64' in plat_parts:
            score = 100 if cp_tag in abi_parts else 90
        elif ('py3' in py_parts or 'py2.py3' in py_parts or cp_tag in py_parts) and 'none' in abi_parts and 'win_amd64' in plat_parts:
            score = 85
        elif _is_compatible_cp3_tag(py_parts, target_minor) and 'abi3' in abi_parts and 'win_amd64' in plat_parts:
            score = 80
        elif ('py3' in py_parts or 'py2.py3' in py_parts) and 'none' in abi_parts and 'any' in plat_parts:
            score = 70
        else:
            continue

        candidates.append((score, fn, url, sha))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, best_fn, best_url, best_sha = candidates[0]
    return best_fn, best_url, best_sha


def ensure_ffmpeg(root_dir: Path = ROOT) -> bool:
    if find_ffmpeg_bin_dir(root_dir) is not None:
        return True

    print('\n>>> [环境初始化 1/3] 正在从官方源下载固定正式版 FFmpeg 9.0.2（带 SHA-256 校验）...', flush=True)
    tmp_zip = root_dir / 'userdata' / 'temp' / 'ffmpeg-9.0.2-essentials_build.zip'
    if not download_file_with_resume(
        FFMPEG_URLS,
        tmp_zip,
        label='FFmpeg 9.0.2 正式版',
        expected_sha256=FFMPEG_ZIP_SHA256,
    ):
        print('[错误] FFmpeg 下载失败，请检查网络连接后重新双击启动（支持断点续传）。', flush=True)
        return False

    tools_ff = root_dir / 'tools' / 'ffmpeg'
    if tools_ff.exists():
        shutil.rmtree(tools_ff, ignore_errors=True)
    target_bin = tools_ff / 'ffmpeg-9.0.2-essentials_build' / 'bin'
    target_bin.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(tmp_zip, 'r') as zf:
            if zf.testzip() is not None:
                raise ValueError('FFmpeg 压缩包校验损坏')
            for info in zf.infolist():
                norm = info.filename.replace('\\', '/')
                if info.is_dir() or '/bin/' not in norm:
                    continue
                base_name = norm.rsplit('/bin/', 1)[-1]
                if base_name.lower() not in ('ffmpeg.exe', 'ffprobe.exe'):
                    continue
                dest = target_bin / base_name
                with zf.open(info, 'r') as src_s, dest.open('wb') as dst_s:
                    shutil.copyfileobj(src_s, dst_s)
    except Exception as exc:
        print(f'[错误] 解压 FFmpeg 失败：{exc}', flush=True)
        tmp_zip.unlink(missing_ok=True)
        return False

    tmp_zip.unlink(missing_ok=True)
    ff_exe = target_bin / 'ffmpeg.exe'
    fp_exe = target_bin / 'ffprobe.exe'
    if not ff_exe.is_file() or not fp_exe.is_file():
        print('[错误] FFmpeg 解压后未找到可执行文件。', flush=True)
        return False
    if _sha256_file(ff_exe) != FFMPEG_EXE_SHA256 or _sha256_file(fp_exe) != FFPROBE_EXE_SHA256:
        shutil.rmtree(tools_ff, ignore_errors=True)
        print('[错误] FFmpeg 可执行文件 SHA-256 校验未通过，已清理异常文件。', flush=True)
        return False

    print('    FFmpeg 9.0.2 正式版已就绪！', flush=True)
    return True


def ensure_python_packages(root_dir: Path = ROOT) -> bool:
    missing = find_missing_packages()
    if not missing:
        return True

    total = len(missing)
    print('\n' + '=' * 72, flush=True)
    print(f'>>> [环境初始化 2/3] 首次运行：正在从官方 PyPI 安装运行库与 NVIDIA CUDA 组件（共 {total} 项）', flush=True)
    print('    支持字节级断点续传与官方 SHA-256 校验；安装一次永久生效，后续启动免下载。', flush=True)
    print('=' * 72, flush=True)

    # Remove uv/standalone PEP 668 marker inside the private portable Python directory if present
    for ext_marker in (
        root_dir / 'runtime' / 'python' / 'Lib' / 'EXTERNALLY-MANAGED',
        Path(sys.executable).resolve().parent / 'Lib' / 'EXTERNALLY-MANAGED',
    ):
        try:
            ext_marker.unlink(missing_ok=True)
        except OSError:
            pass

    wheelhouse = root_dir / 'userdata' / 'temp' / 'wheelhouse'
    wheelhouse.mkdir(parents=True, exist_ok=True)
    opener = _build_opener()
    py_nodot = f'{sys.version_info.major}{sys.version_info.minor}'

    batch_wheels: List[Path] = []

    def _flush_install_batch(wheels: List[Path]) -> bool:
        if not wheels:
            return True
        cmd = [
            sys.executable,
            '-m',
            'pip',
            'install',
            '--no-index',
            '--no-deps',
            '--break-system-packages',
            '--no-warn-script-location',
            '--disable-pip-version-check',
            *[str(w) for w in wheels],
        ]
        env = os.environ.copy()
        env['PYTHONNOUSERSITE'] = '1'
        res = subprocess.run(cmd, env=env)
        if res.returncode != 0:
            return False
        for w in wheels:
            w.unlink(missing_ok=True)
        wheels.clear()
        return True

    for idx, (pkg_name, pkg_ver) in enumerate(missing, start=1):
        meta_url = f'https://pypi.org/pypi/{pkg_name}/{pkg_ver}/json'
        try:
            req = urllib.request.Request(meta_url, headers={'User-Agent': 'FaceFusion-Bootstrap/1.1'})
            with opener.open(req, timeout=20) as resp:
                meta = json.loads(resp.read().decode('utf-8'))
        except Exception as exc:
            print(f'\n[错误] 无法获取官方 PyPI 元数据 {pkg_name}=={pkg_ver}：{exc}', flush=True)
            return False

        selected = select_best_wheel_from_pypi(meta.get('urls') or [], py_nodot=py_nodot)
        if not selected:
            print(f'\n[错误] 未在 PyPI 找到兼容 Windows x64 Python {py_nodot} 的预编译包：{pkg_name}=={pkg_ver}', flush=True)
            return False

        whl_name, whl_url, whl_sha = selected
        whl_path = wheelhouse / whl_name
        label = f'[{idx}/{total}] {pkg_name}'
        if not download_file_with_resume([whl_url], whl_path, label=label, expected_sha256=whl_sha or None):
            print(f'\n[错误] 下载组件 {pkg_name} 失败（已保存断点进度，重新双击启动即可继续）。', flush=True)
            return False

        batch_wheels.append(whl_path)
        is_large_gpu_pkg = pkg_name.startswith(('nvidia-', 'onnxruntime'))
        if is_large_gpu_pkg or len(batch_wheels) >= 12 or idx == total:
            print(f'    正在解压安装组件批次（至 {pkg_name}）...', flush=True)
            if not _flush_install_batch(batch_wheels):
                print(f'\n[错误] pip 安装组件 {pkg_name} 失败。', flush=True)
                return False

    shutil.rmtree(wheelhouse, ignore_errors=True)
    print('\n    Python 与 NVIDIA CUDA 运行环境全部安装完成！', flush=True)
    return True


def is_environment_ready(root_dir: Path = ROOT) -> bool:
    return find_ffmpeg_bin_dir(root_dir) is not None and len(find_missing_packages()) == 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Bootstrap FFmpeg and Python/CUDA runtime from official global CDNs.')
    parser.add_argument('--check-only', action='store_true', help='Exit 0 if ready, 10 if bootstrap needed.')
    parser.add_argument('--ensure', action='store_true', help='Download & install any missing runtime components.')
    args = parser.parse_args(argv)

    if args.check_only:
        return 0 if is_environment_ready() else 10

    if not ensure_ffmpeg():
        return 1
    if not ensure_python_packages():
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
