"""First-run automatic model bootstrapper from official FaceFusion global CDNs (GitHub / HuggingFace)."""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Tuple

from facefusion import state_manager
from facefusion.download import _stream_download_with_failover, resolve_download_url
from facefusion.filesystem import get_file_size, is_file, remove_file, resolve_models_dir, resolve_relative_path
from facefusion.hash_helper import validate_hash

# Exact 19 models required by UI startup + all 3 built-in presets (normal / mouth / eye)
PRESET_MODELS: Tuple[Tuple[str, str], ...] = (
    ('models-3.0.0', 'yoloface_8n.onnx'),
    ('models-3.0.0', '2dfan4.onnx'),
    ('models-3.0.0', 'peppa_wutz.onnx'),
    ('models-3.0.0', 'fan_68_5.onnx'),
    ('models-3.0.0', 'arcface_w600k_r50.onnx'),
    ('models-3.0.0', 'fairface.onnx'),
    ('models-3.1.0', 'xseg_1.onnx'),
    ('models-3.0.0', 'bisenet_resnet_34.onnx'),
    ('models-3.0.0', 'inswapper_128_fp16.onnx'),
    ('models-3.0.0', 'live_portrait_feature_extractor.onnx'),
    ('models-3.0.0', 'live_portrait_motion_extractor.onnx'),
    ('models-3.0.0', 'live_portrait_eye_retargeter.onnx'),
    ('models-3.0.0', 'live_portrait_lip_retargeter.onnx'),
    ('models-3.0.0', 'live_portrait_stitcher.onnx'),
    ('models-3.0.0', 'live_portrait_generator.onnx'),
    ('models-3.0.0', 'gfpgan_1.4.onnx'),
    ('models-3.3.0', 'nsfw_1.onnx'),
    ('models-3.3.0', 'nsfw_2.onnx'),
    ('models-3.3.0', 'nsfw_3.onnx'),
)


def _init_default_download_state() -> None:
    if not state_manager.get_item('download_providers'):
        state_manager.init_item('download_providers', ['huggingface', 'github'])
    if not state_manager.get_item('log_level'):
        state_manager.init_item('log_level', 'info')


def find_missing_preset_models(verify_crc: bool = False) -> List[Tuple[str, str]]:
    missing: List[Tuple[str, str]] = []
    for base_name, onnx_name in PRESET_MODELS:
        stem, _ = os.path.splitext(onnx_name)
        hash_path = resolve_relative_path(f'../.assets/models/{stem}.hash')
        onnx_path = resolve_relative_path(f'../.assets/models/{onnx_name}')
        if not is_file(hash_path) or get_file_size(hash_path) == 0:
            missing.append((base_name, onnx_name))
            continue
        if not is_file(onnx_path) or get_file_size(onnx_path) == 0:
            missing.append((base_name, onnx_name))
            continue
        if verify_crc and not validate_hash(onnx_path):
            missing.append((base_name, onnx_name))
    return missing


def ensure_preset_models() -> bool:
    _init_default_download_state()
    missing = find_missing_preset_models(verify_crc=False)
    if not missing:
        return True

    target_dir = resolve_models_dir()
    total = len(missing)
    print('=' * 68, flush=True)
    print(f' 首次运行初始化：正在从官方源自动下载所需 AI 模型（共 {total} 个）', flush=True)
    print(f' 下载源：HuggingFace / GitHub Official Releases', flush=True)
    print(f' 本地目录：{target_dir}（下载一次永久缓存，后续启动免下载）', flush=True)
    print('=' * 68, flush=True)

    for idx, (base_name, onnx_name) in enumerate(missing, start=1):
        stem, _ = os.path.splitext(onnx_name)
        hash_name = f'{stem}.hash'
        hash_path = resolve_relative_path(f'../.assets/models/{hash_name}')
        onnx_path = resolve_relative_path(f'../.assets/models/{onnx_name}')

        if not is_file(hash_path) or get_file_size(hash_path) == 0:
            hash_url = resolve_download_url(base_name, hash_name)
            if not hash_url or not _stream_download_with_failover(hash_url, hash_path):
                print(f'\n[错误] 无法下载校验文件 {hash_name}，请检查网络连接后重试。', flush=True)
                return False

        if is_file(onnx_path) and validate_hash(onnx_path):
            print(f'[{idx}/{total}] {onnx_name} 已校验就绪', flush=True)
            continue

        print(f'\n[{idx}/{total}] 正在下载 {onnx_name} ...', flush=True)
        onnx_url = resolve_download_url(base_name, onnx_name)
        if not onnx_url or not _stream_download_with_failover(onnx_url, onnx_path):
            print(f'\n[错误] 下载模型 {onnx_name} 失败（支持断点续传，重新双击启动即可继续）。', flush=True)
            return False

        if not validate_hash(onnx_path):
            remove_file(onnx_path)
            print(f'\n[错误] 模型 {onnx_name} 完整性校验未通过，已清理损坏文件，请重新启动重试。', flush=True)
            return False

    print('\n' + '=' * 68, flush=True)
    print(' 所有模型下载与完整性校验已完成！正在启动 FaceFusion 服务...', flush=True)
    print('=' * 68, flush=True)
    return True


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Bootstrap FaceFusion preset models from official CDNs.')
    parser.add_argument('--check-only', action='store_true', help='Exit 0 if all preset models exist, 10 if missing.')
    parser.add_argument('--ensure', action='store_true', help='Download and verify any missing preset models.')
    args = parser.parse_args(argv)

    if args.check_only:
        return 0 if not find_missing_preset_models(verify_crc=False) else 10

    return 0 if ensure_preset_models() else 1


if __name__ == '__main__':
    sys.exit(main())
