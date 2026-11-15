"""
Build clean, GitHub-Release-ready distribution archives in `userdata/dist/`:
1) `FaceFusion-Update-CodeOnly.zip` (~1-2 MB): lightweight code/UI update patch for existing installations.
2) `FaceFusion-Portable-Windows-NVIDIA.zip` (~1.4 GB): self-contained Python + CUDA + FFmpeg environment
   (excludes `userdata/` and `.onnx` model weights, which auto-download on first launch from official GitHub/HuggingFace CDNs).
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
EXCLUDED_SUFFIXES = {'.pyc', '.pyo', '.onnx', '.part'}
FORBIDDEN_PERSONAL_FILENAMES = {
    '.facefusion.pid',
    'facefusion.log',
    'facefusion-error.log',
    'facefusion-presets.json',
    'facefusion-google-token.json',
    'facefusion-google-client-secret.json',
    'facefusion-sheet-worker.json',
}


def _is_secret_or_personal_file(path: Path) -> bool:
    lower = path.name.lower()
    if lower in FORBIDDEN_PERSONAL_FILENAMES:
        return True
    if 'client_secret' in lower and lower.endswith('.json'):
        return True
    if 'google-token' in lower and lower.endswith('.json'):
        return True
    return False


def collect_shareable_files(*, include_runtime_and_tools: bool) -> list[Path]:
    result: list[Path] = []
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        top = rel.parts[0]
        if top in EXCLUDED_TOP_NAMES:
            continue
        if not include_runtime_and_tools and top in {'runtime', 'tools'}:
            continue
        if any(part in EXCLUDED_PART_NAMES for part in rel.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        # Strip unused ffplay.exe (~105 MB) and HTML docs from bundled FFmpeg
        if top == 'tools':
            if path.name.lower() == 'ffplay.exe':
                continue
            if 'doc' in [p.lower() for p in rel.parts]:
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
            if idx % 1500 == 0 or idx == len(files) or time.monotonic() - last_print >= 12:
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
    parser = argparse.ArgumentParser(description='Create slim portable & update ZIP packages.')
    parser.add_argument('--update-only', action='store_true', help='Only build the ~2 MB code update package.')
    parser.add_argument('--full-only', action='store_true', help='Only build the slim full environment package.')
    args = parser.parse_args(argv)

    build_update = not args.full_only
    build_full = not args.update_only

    if build_update:
        update_files = collect_shareable_files(include_runtime_and_tools=False)
        build_zip_archive(
            update_files,
            UPDATE_ARCHIVE_PATH,
            arc_prefix=None,
            label='轻量代码更新包（给已安装成员覆盖升级用）'
        )

    if build_full:
        full_files = collect_shareable_files(include_runtime_and_tools=True)
        build_zip_archive(
            full_files,
            FULL_ARCHIVE_PATH,
            arc_prefix=FULL_FOLDER_NAME,
            label='完整运行环境包（不含 .onnx 模型，首次启动自动从官方源下载）'
        )

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
