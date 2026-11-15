"""
Lightweight GitHub Releases code hot-updater (~0.4 MB `FaceFusion-Update-CodeOnly.zip`).
Pure Python 3.12 stdlib only — works before third-party packages are installed and inside Gradio UI.
Strictly connects ONLY to official GitHub endpoints (`api.github.com` / `github.com`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Optional, Tuple

# File lives at `<ROOT>/app/scripts/auto_updater.py` -> parents[2] is `<ROOT>`
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GITHUB_REPO = 'jmdx-code/facefusion-talking-swap'
UPDATE_ASSET_NAME = 'FaceFusion-Update-CodeOnly.zip'
UPDATE_SHA_ASSET_NAME = 'FaceFusion-Update-CodeOnly.zip.sha256.txt'


class UpdateCheckResult:
    def __init__(
        self,
        status: str,
        local_version: str,
        remote_version: str,
        message: str,
        release_url: str = '',
    ) -> None:
        self.status = status
        self.local_version = local_version
        self.remote_version = remote_version
        self.message = message
        self.release_url = release_url


def read_local_version(root_dir: Path = ROOT) -> str:
    for candidate in (root_dir / 'app' / 'VERSION', root_dir / 'VERSION'):
        if candidate.is_file():
            raw = candidate.read_text(encoding='utf-8', errors='ignore').strip()
            if raw:
                return raw.lstrip('vV')
    return '1.0.0'


def parse_semver(ver_str: str) -> Tuple[int, ...]:
    cleaned = (ver_str or '').strip().lstrip('vV')
    parts = re.findall(r'\d+', cleaned)
    if not parts:
        return (0, 0, 0)
    nums = [int(x) for x in parts[:4]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def is_newer_version(remote_ver: str, local_ver: str) -> bool:
    return parse_semver(remote_ver) > parse_semver(local_ver)


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


def _http_get_bytes(url: str, *, timeout: float, accept: str = '*/*') -> bytes:
    opener = _build_opener()
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': f'FaceFusion-Talking-Swap-Updater/{read_local_version()}',
            'Accept': accept,
        },
    )
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def _resolve_api_url() -> str:
    override_url = os.environ.get('FACEFUSION_UPDATE_API_URL', '').strip()
    if override_url:
        return override_url
    repo = os.environ.get('FACEFUSION_UPDATE_REPO', '').strip() or DEFAULT_GITHUB_REPO
    return f'https://api.github.com/repos/{repo}/releases/latest'


def fetch_latest_release_metadata(timeout: float = 5.0) -> dict[str, Any]:
    api_url = _resolve_api_url()
    raw = _http_get_bytes(api_url, timeout=timeout, accept='application/vnd.github+json')
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict) or 'tag_name' not in data:
        raise ValueError('Invalid GitHub release payload.')
    return data


def _extract_expected_sha256(release_data: dict[str, Any], timeout: float) -> Optional[str]:
    assets = release_data.get('assets') or []
    for asset in assets:
        if asset.get('name') == UPDATE_ASSET_NAME:
            digest = str(asset.get('digest') or '').strip()
            if digest.lower().startswith('sha256:'):
                hex_part = digest.split(':', 1)[1].strip().lower()
                if re.fullmatch(r'[0-9a-f]{64}', hex_part):
                    return hex_part

    for asset in assets:
        if asset.get('name') == UPDATE_SHA_ASSET_NAME:
            dl_url = str(asset.get('browser_download_url') or '').strip()
            if dl_url:
                try:
                    txt = _http_get_bytes(dl_url, timeout=timeout).decode('utf-8', errors='ignore')
                    m = re.search(r'\b([0-9a-fA-F]{64})\b', txt)
                    if m:
                        return m.group(1).lower()
                except Exception:
                    pass
    return None


def validate_and_extract_update_zip(
    zip_path: Path,
    target_root: Path,
    expected_sha256: Optional[str] = None,
) -> int:
    if expected_sha256:
        actual = hashlib.sha256(zip_path.read_bytes()).hexdigest().lower()
        if actual != expected_sha256.lower():
            raise ValueError(f'更新包 SHA-256 校验不一致（期望 {expected_sha256[:12]}...，实际 {actual[:12]}...）')

    forbidden_roots = {'userdata', 'runtime', 'tools', '.git', '_维护者工具'}
    extracted_count = 0

    with zipfile.ZipFile(zip_path, 'r') as zf:
        bad_member = zf.testzip()
        if bad_member:
            raise ValueError(f'更新包 CRC 损坏：{bad_member}')

        members = zf.infolist()
        resolved_root = target_root.resolve()

        # First pass: strict path-traversal & protected-directory validation
        safe_entries: list[Tuple[zipfile.ZipInfo, Path]] = []
        for info in members:
            norm = info.filename.replace('\\', '/').strip('/')
            if not norm or info.is_dir():
                continue
            parts = norm.split('/')
            if any(p in ('', '.', '..') for p in parts):
                raise ValueError(f'拒绝不安全的更新包路径：{info.filename}')
            if parts[0].lower() in forbidden_roots:
                raise ValueError(f'拒绝覆盖受保护目录（{parts[0]}）：{info.filename}')
            dest = (resolved_root / Path(*parts)).resolve()
            if resolved_root not in dest.parents:
                raise ValueError(f'越界路径被拦截：{info.filename}')
            safe_entries.append((info, dest))

        # Second pass: atomic file write via .upd_tmp
        for info, dest in safe_entries:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp_dest = dest.with_name(dest.name + '.upd_tmp')
            with zf.open(info, 'r') as src_stream, tmp_dest.open('wb') as dst_stream:
                shutil.copyfileobj(src_stream, dst_stream)
            os.replace(tmp_dest, dest)
            extracted_count += 1

    return extracted_count


def check_and_apply_update(
    *,
    apply_update: bool = True,
    timeout_sec: float = 6.0,
    target_root: Path = ROOT,
) -> UpdateCheckResult:
    local_ver = read_local_version(target_root)
    try:
        rel = fetch_latest_release_metadata(timeout=timeout_sec)
    except Exception as exc:
        return UpdateCheckResult(
            status='error',
            local_version=local_ver,
            remote_version=local_ver,
            message=f'检查更新连接超时或暂不可用（当前 v{local_ver}）：{exc}',
        )

    raw_tag = str(rel.get('tag_name') or '').strip()
    remote_ver = raw_tag.lstrip('vV') or local_ver
    release_url = str(rel.get('html_url') or '').strip()

    if not is_newer_version(remote_ver, local_ver):
        return UpdateCheckResult(
            status='up_to_date',
            local_version=local_ver,
            remote_version=remote_ver,
            release_url=release_url,
            message=f'当前已是最新版本 v{local_ver}',
        )

    assets = rel.get('assets') or []
    asset_url = ''
    for asset in assets:
        if asset.get('name') == UPDATE_ASSET_NAME:
            asset_url = str(asset.get('browser_download_url') or '').strip()
            break

    if not asset_url:
        return UpdateCheckResult(
            status='update_available',
            local_version=local_ver,
            remote_version=remote_ver,
            release_url=release_url,
            message=f'检测到新版本 v{remote_ver}，但该版本未附带轻量更新包，请前往 Releases 页面查看。',
        )

    if not apply_update:
        return UpdateCheckResult(
            status='update_available',
            local_version=local_ver,
            remote_version=remote_ver,
            release_url=release_url,
            message=f'发现新版本 v{remote_ver}（当前 v{local_ver}），可一键热更新。',
        )

    try:
        expected_sha = _extract_expected_sha256(rel, timeout=timeout_sec)
        upd_dir = target_root / 'userdata' / 'temp' / 'update'
        upd_dir.mkdir(parents=True, exist_ok=True)
        zip_path = upd_dir / UPDATE_ASSET_NAME
        part_path = upd_dir / (UPDATE_ASSET_NAME + '.part')

        payload = _http_get_bytes(asset_url, timeout=max(timeout_sec * 3, 20.0))
        part_path.write_bytes(payload)
        os.replace(part_path, zip_path)

        file_cnt = validate_and_extract_update_zip(zip_path, target_root, expected_sha256=expected_sha)
        ver_dest = target_root / 'app' / 'VERSION'
        ver_dest.parent.mkdir(parents=True, exist_ok=True)
        ver_dest.write_text(f'{remote_ver}\n', encoding='utf-8')
        try:
            zip_path.unlink()
        except OSError:
            pass

        return UpdateCheckResult(
            status='updated',
            local_version=local_ver,
            remote_version=remote_ver,
            release_url=release_url,
            message=f'已自动热更新 v{local_ver} -> v{remote_ver}（共更新 {file_cnt} 个代码文件，个人数据与模型完好保留）',
        )
    except Exception as exc:
        return UpdateCheckResult(
            status='error',
            local_version=local_ver,
            remote_version=remote_ver,
            release_url=release_url,
            message=f'下载或应用 v{remote_ver} 更新包失败：{exc}',
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='FaceFusion lightweight GitHub Release hot-updater.')
    parser.add_argument('--startup-check', action='store_true', help='Silent quick hot-update check on startup.')
    parser.add_argument('--check-only', action='store_true', help='Only check without modifying files.')
    parser.add_argument('--interactive', action='store_true', help='Verbose interactive update run.')
    args = parser.parse_args(argv)

    if args.startup_check:
        res = check_and_apply_update(apply_update=True, timeout_sec=4.0)
        if res.status == 'updated':
            print(f'[自动热更新] {res.message}', flush=True)
        return 0

    if args.check_only:
        res = check_and_apply_update(apply_update=False, timeout_sec=8.0)
        print(res.message, flush=True)
        return 0

    print(f'当前本地版本：v{read_local_version()}，正在从 GitHub 官方源检查最新更新...', flush=True)
    res = check_and_apply_update(apply_update=True, timeout_sec=12.0)
    print(res.message, flush=True)
    return 0 if res.status in ('up_to_date', 'updated') else 1


if __name__ == '__main__':
    sys.exit(main())
