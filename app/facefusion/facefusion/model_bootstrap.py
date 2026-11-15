"""First-run automatic model bootstrapper from official FaceFusion global CDNs (GitHub / HuggingFace)."""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from typing import Dict, List, Tuple

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

# Cryptographic SHA-256 digests of official FaceFusion weights (prevents CRC32 collision tampering)
PINNED_MODEL_SHA256: Dict[str, str] = {
    '2dfan4.onnx': '678c6fa539d52335a31c980feefdf4a6e02d781d83dce00af8a894f114557285',
    'arcface_w600k_r50.onnx': 'f1f79dc3b0b79a69f94799af1fffebff09fbd78fd96a275fd8f0cbbea23270d1',
    'bisenet_resnet_34.onnx': '4a0b8c958a3c938913bd06a8365dbb3c8761afba6ecbf0d14b3b1f77eb230c96',
    'fairface.onnx': 'b176b1e4b0739496e7943af7ba4ff306271fecf837bca5ec5d9a48201959db37',
    'fan_68_5.onnx': 'ea05f7d9d014ae1d9cdc6e9c72643d8f00d198b5db3fdd384148ddff499a613e',
    'gfpgan_1.4.onnx': 'accc4757b26bdb89b32b4d3500d4f79c9dff97c1dd7c7104bf9dcb95e3311385',
    'inswapper_128_fp16.onnx': 'c4eccca86ad177586c85c28bf1a64a9d9ed237e283a15818d831f7facfd3f420',
    'live_portrait_eye_retargeter.onnx': '13cf8b06f0a314e6e65b4a47fea5ce270edb79cd54359c4d273f2c139820276b',
    'live_portrait_feature_extractor.onnx': '980d26afc9af6d1b6c946329df08b89a4a8582c9bb668d9475f5254e69434d15',
    'live_portrait_generator.onnx': 'db9274114fed504226c5921fc736b9558505e68682e7448631aea7b62cb3048a',
    'live_portrait_lip_retargeter.onnx': '0f5b76344d0bfbb44c8a0b1ec57a4a7c36fd38db530338777a48c4a33a02d71f',
    'live_portrait_motion_extractor.onnx': 'ef4a00ff50d5d864994eeecb226dc3b298e62afacec8fa4fc8b912835ec4b0ab',
    'live_portrait_stitcher.onnx': 'c6683427edf3e0c3a86e61b1dea1e6dad56c557f5e711fec93966cfb34fd5f87',
    'nsfw_1.onnx': '3debfd72763bc931dd308f57d1e9299ae13bb309d14ddbdc17ce8f1fd42f96af',
    'nsfw_2.onnx': '5aae1806ce44e3c27fab0a7d44eed581dd25ca71a4387ff809a191c98239c1be',
    'nsfw_3.onnx': '778139fb930240ac39812a1480295bfc5dc2cec8a8e46ee6b453481d600c0fa2',
    'peppa_wutz.onnx': 'ff2de0fa5ceb8aad8c8d037ba2fab8afe90ace34626f3268aae5a56965015776',
    'xseg_1.onnx': 'c4d1498b8a03b5fe2a3a5d2ef2a0402ab03bd51edaf5b2d8d5fb764702a97dd3',
    'yoloface_8n.onnx': '821cdbb1e65fbbabdde7dd0933f754797a343e56fd962729c61ffcefcd135929',
}


def validate_model_sha256(file_path: str) -> bool:
    """Verify pinned SHA-256 digest when `file_path` is one of the 19 official preset ONNX models."""
    if not is_file(file_path):
        return False
    expected = PINNED_MODEL_SHA256.get(os.path.basename(file_path))
    if not expected:
        return True
    hasher = hashlib.sha256()
    try:
        with open(file_path, 'rb') as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
    except OSError:
        return False
    return hasher.hexdigest() == expected


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
        if verify_crc and (not validate_hash(onnx_path) or not validate_model_sha256(onnx_path)):
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

        if is_file(onnx_path) and validate_hash(onnx_path) and validate_model_sha256(onnx_path):
            print(f'[{idx}/{total}] {onnx_name} 已校验就绪', flush=True)
            continue

        print(f'\n[{idx}/{total}] 正在下载 {onnx_name} ...', flush=True)
        onnx_url = resolve_download_url(base_name, onnx_name)
        if not onnx_url or not _stream_download_with_failover(onnx_url, onnx_path):
            print(f'\n[错误] 下载模型 {onnx_name} 失败（支持断点续传，重新双击启动即可继续）。', flush=True)
            return False

        if not validate_hash(onnx_path) or not validate_model_sha256(onnx_path):
            remove_file(onnx_path)
            print(f'\n[错误] 模型 {onnx_name} 完整性/SHA-256 防篡改校验未通过，已清理异常文件，请重新启动重试。', flush=True)
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
