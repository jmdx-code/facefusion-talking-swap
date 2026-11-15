"""
Build ultra-lightweight GitHub-Release-ready distribution archives in `userdata/dist/` (Scheme A + Scheme C):
1) `FaceFusion-Update-CodeOnly.zip` (~0.4 MB): instant hot-update patch for existing installations (auto-applied on startup or UI click).
2) `FaceFusion-Portable-Windows-NVIDIA.zip` (~14 MB): micro-portable package bundling minimal Python 3.12 stdlib + `pip` + app code.
   On first launch, `env_bootstrap.py` and `model_bootstrap.py` automatically download FFmpeg, Python/CUDA 12/cuDNN 9 wheels,
   and `.onnx` models from official international CDNs (GitHub / PyPI / HuggingFace) with byte-level resume and SHA-256 / CRC32 verification.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
USERDATA = ROOT / 'userdata'
DIST_DIR = USERDATA / 'dist'

FULL_FOLDER_NAME = 'FaceFusion-Portable-Windows-NVIDIA'
FULL_ARCHIVE_PATH = DIST_DIR / f'{FULL_FOLDER_NAME}.zip'
UPDATE_ARCHIVE_PATH = DIST_DIR / 'FaceFusion-Update-CodeOnly.zip'

EXCLUDED_TOP_NAMES = {'userdata', '.git', '.github', '.vs', '.vscode', '.idea'}
EXCLUDED_PART_NAMES = {'__pycache__', '.caches', '.pytest_cache', '.ruff_cache'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo', '.onnx', '.part', '.pdb'}
FORBIDDEN_PERSONAL_FILENAMES = {
    '.facefusion.pid',
    'facefusion.log',
    'facefusion-error.log',
    'facefusion-presets.json',
    'facefusion-google-token.json',
    'facefusion-google-client-secret.json',
    'facefusion-sheet-worker.json',
}

# Excluded unused stdlib / Tcl-Tk / test modules from minimal portable Python core
_EXCLUDED_PYTHON_SUBDIRS = {'include', 'libs', 'tcl', 'scripts'}
_EXCLUDED_STDLIB_DIRS = {'test', 'tkinter', 'idlelib', 'turtledemo', 'ensurepip'}


def _is_secret_or_personal_file(path: Path) -> bool:
    lower = path.name.lower()
    if lower in ('.env.example', 'cacert.pem'):
        return False
    if lower == '.env' or lower.startswith('.env.'):
        return True
    if lower in FORBIDDEN_PERSONAL_FILENAMES or lower == 'credentials.json':
        return True
    if path.suffix.lower() in {'.pem', '.key', '.pfx', '.p12'}:
        return True
    if lower.endswith('.json') and any(
        kw in lower for kw in ('client_secret', 'google-token', 'service_account', 'service-account')
    ):
        return True
    return False


def _keep_in_minimal_python_core(rel: Path) -> bool:
    """Keep minimal Python 3.12 interpreter + stdlib + SSL/SQLite/ctypes DLLs + `pip` (~14 MB compressed).
    All heavy third-party `site-packages` (CUDA/cuDNN/ONNXRuntime/Gradio/etc.) are auto-bootstrapped on first launch."""
    # Expected rel: runtime/python/...
    if len(rel.parts) < 3 or rel.parts[1].lower() != 'python':
        return False
    sub_parts = rel.parts[2:]
    sub_low = [p.lower() for p in sub_parts]

    if sub_low[0] in _EXCLUDED_PYTHON_SUBDIRS:
        return False

    if sub_low[0] == 'dlls' and len(sub_low) >= 2:
        fn = sub_low[1]
        if fn.startswith(('tcl', 'tk', '_tkinter', '_test', '_ctypes_test')):
            return False
        return True

    if sub_low[0] == 'lib':
        if len(sub_low) >= 2 and sub_low[1] in _EXCLUDED_STDLIB_DIRS:
            return False
        if len(sub_low) >= 2 and sub_low[1] == 'site-packages':
            # Keep ONLY `pip` and `pip-*.dist-info` so `python -m pip install --no-index` works offline on downloaded wheels!
            if len(sub_low) < 3:
                return False
            pkg_entry = sub_low[2]
            return pkg_entry == 'pip' or pkg_entry.startswith('pip-')
        return True

    return True


def collect_shareable_files(*, include_runtime_and_tools: bool) -> list[Path]:
    result: list[Path] = []
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        top = rel.parts[0]
        if top in EXCLUDED_TOP_NAMES:
            continue
        # FFmpeg (`tools/`) is excluded from both archives and auto-downloaded on first run by `env_bootstrap.py`
        if top == 'tools':
            continue
        if top == 'runtime':
            if not include_runtime_and_tools:
                continue
            if not _keep_in_minimal_python_core(rel):
                continue
        if any(part in EXCLUDED_PART_NAMES for part in rel.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        if _is_secret_or_personal_file(path):
            raise RuntimeError(f'检测到根目录外存在个人配置或密钥文件，已中止打包：{rel}')
        result.append(path)
    return result


def build_zip_archive(
    files: list[Path],
    output_zip: Path,
    *,
    arc_prefix: str | None,
    label: str
) -> None:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    temp_zip = output_zip.with_suffix('.tmp.zip')
    if temp_zip.exists():
        temp_zip.unlink()

    total_bytes = sum(p.stat().st_size for p in files)
    written_bytes = 0
    last_print = time.monotonic()
    size_disp = (
        f'{total_bytes / (1024 ** 3):.2f} GB'
        if total_bytes >= 100 * 1024 * 1024
        else f'{total_bytes / (1024 ** 2):.2f} MB'
    )
    print(f'\n>>> 开始生成【{label}】：共 {len(files)} 个文件（原始体积 {size_disp}）', flush=True)

    with zipfile.ZipFile(
        temp_zip,
        mode='w',
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as zf:
        for idx, file_path in enumerate(files, start=1):
            rel_posix = file_path.relative_to(ROOT).as_posix()
            arc_name = f'{arc_prefix}/{rel_posix}' if arc_prefix else rel_posix
            zf.write(file_path, arc_name)
            written_bytes += file_path.stat().st_size
            if idx % 500 == 0 or idx == len(files) or time.monotonic() - last_print >= 8:
                pct = (written_bytes / total_bytes * 100.0) if total_bytes else 100.0
                print(f'    进度：{idx}/{len(files)} ({pct:.1f}%)', flush=True)
                last_print = time.monotonic()

    print('    正在校验压缩包 CRC 完整性...', flush=True)
    with zipfile.ZipFile(temp_zip) as check:
        bad = check.testzip()
        if bad:
            raise RuntimeError(f'压缩包校验失败：{bad}')

    os.replace(temp_zip, output_zip)

    digest = hashlib.sha256()
    with output_zip.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    sha_hex = digest.hexdigest().upper()
    sha_file = DIST_DIR / f'{output_zip.name}.sha256.txt'
    sha_file.write_text(f'{sha_hex}  {output_zip.name}\n', encoding='ascii')

    out_bytes = output_zip.stat().st_size
    out_disp = (
        f'{out_bytes / (1024 ** 3):.2f} GB'
        if out_bytes >= 100 * 1024 * 1024
        else f'{out_bytes / (1024 ** 2):.2f} MB'
    )
    print(f'    完成：{output_zip}（压缩后体积 {out_disp}）', flush=True)
    print(f'    SHA256：{sha_hex}', flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Create micro-portable & hot-update ZIP packages.')
    parser.add_argument('--update-only', action='store_true', help='Only build the ~0.4 MB code hot-update package.')
    parser.add_argument('--full-only', action='store_true', help='Only build the ~14 MB micro-portable package.')
    args = parser.parse_args(argv)

    build_update = not args.full_only
    build_full = not args.update_only

    if build_update:
        update_files = collect_shareable_files(include_runtime_and_tools=False)
        build_zip_archive(
            update_files,
            UPDATE_ARCHIVE_PATH,
            arc_prefix=None,
            label='轻量代码热更新包（~0.4 MB，启动自动同步 / 覆盖升级用）'
        )

    if build_full:
        full_files = collect_shareable_files(include_runtime_and_tools=True)
        build_zip_archive(
            full_files,
            FULL_ARCHIVE_PATH,
            arc_prefix=FULL_FOLDER_NAME,
            label='极简便携初始包（~14 MB，首次启动自动从官方源安装环境与模型）'
        )

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
