"""Local, portable presets for face swapping, expression restoration and masks."""
import copy
import json
import math
import os
import tempfile
import threading
from pathlib import Path

import facefusion.choices as choices
from facefusion import process_manager, state_manager
from facefusion.processors.core import get_processors_modules
from facefusion.processors.modules.expression_restorer import choices as expression_choices
from facefusion.processors.modules.face_enhancer import choices as enhancer_choices
from facefusion.processors.modules.face_swapper import choices as swapper_choices
from facefusion.userdata_paths import get_userdata_dir

PRESET_PATH = get_userdata_dir() / 'facefusion-presets.json'
STORE_LOCK = threading.RLock()
DEFAULTS = {
    'processors': ['face_swapper', 'expression_restorer', 'face_enhancer'],
    'face_swapper_model': 'inswapper_128_fp16',
    'face_swapper_pixel_boost': '128x128',
    'face_swapper_weight': 0.5,
    'expression_restorer_model': 'live_portrait',
    'expression_restorer_factor': 80,
    'expression_restorer_areas': ['upper-face', 'lower-face'],
    'face_enhancer_model': 'gfpgan_1.4',
    'face_enhancer_blend': 65,
    'face_enhancer_weight': 0.5,
    'face_mask_types': ['box'],
    'face_mask_areas': list(choices.face_mask_areas),
    'face_mask_regions': list(choices.face_mask_regions),
    'face_mask_blur': 0.65,
    'face_mask_padding': [0, 0, 0, 0],
    'face_parser_model': 'bisenet_resnet_34',
    'face_occluder_model': 'xseg_1'
}
PRESET_KEYS = tuple(DEFAULTS)


def preset(label, description, **overrides):
    values = copy.deepcopy(DEFAULTS)
    values.update(overrides)
    return {'label': label, 'description': description, 'values': values}


BUILTINS = {
    'clear': preset('没问题选这个', '之前的正常参数：InSwapper FP16换脸，LivePortrait恢复上下半脸80，GFPGAN 1.4混合65。'),
    'mouth_restore': preset('解决嘴巴张不开', 'LivePortrait只恢复下半脸，强度100；GFPGAN 1.4混合30。用于改善换脸后口型偏小的情况。', expression_restorer_factor=100, expression_restorer_areas=['lower-face'], face_enhancer_blend=30),
    'eye_restore': preset('解决眼睛斜', '在“没问题选这个”的基础上，只关闭上半脸表情恢复；保留下半脸恢复80和GFPGAN 1.4混合65。', expression_restorer_areas=['lower-face'])
}


def validate_values(values):
    if not isinstance(values, dict) or set(values) != set(PRESET_KEYS):
        raise ValueError('预设字段不完整或版本不兼容。')
    enums = {
        'face_swapper_model': swapper_choices.face_swapper_models,
        'face_enhancer_model': enhancer_choices.face_enhancer_models,
        'expression_restorer_model': expression_choices.expression_restorer_models,
        'face_parser_model': choices.face_parser_models,
        'face_occluder_model': choices.face_occluder_models
    }
    for key, allowed in enums.items():
        if values[key] not in allowed:
            raise ValueError('预设中的模型不受支持：' + key)
    groups = {
        'processors': ['face_swapper', 'expression_restorer', 'face_enhancer'],
        'expression_restorer_areas': expression_choices.expression_restorer_areas,
        'face_mask_types': choices.face_mask_types,
        'face_mask_areas': choices.face_mask_areas,
        'face_mask_regions': choices.face_mask_regions
    }
    for key, allowed in groups.items():
        value = values[key]
        if not isinstance(value, list) or not value or any(item not in allowed for item in value) or len(value) != len(set(value)):
            raise ValueError('预设中的选项无效：' + key)
    if 'face_swapper' not in values['processors']:
        raise ValueError('此预设功能需要启用人脸替换；支持同时保存表情恢复、人脸增强和遮罩。')
    if values['face_swapper_pixel_boost'] not in swapper_choices.face_swapper_set[values['face_swapper_model']]:
        raise ValueError('Pixel Boost与换脸模型不匹配。')
    for key, maximum in [('face_swapper_weight', 1), ('face_enhancer_weight', 1), ('face_mask_blur', 1), ('expression_restorer_factor', 100), ('face_enhancer_blend', 100)]:
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= maximum:
            raise ValueError('预设中的数值超出范围：' + key)
        if key in ['expression_restorer_factor', 'face_enhancer_blend'] and value != int(value):
            raise ValueError('强度与混合度需要使用整数。')
    padding = values['face_mask_padding']
    if not isinstance(padding, (list, tuple)) or len(padding) != 4 or any(type(v) is not int or not 0 <= v <= 100 for v in padding):
        raise ValueError('遮罩边距需要四个0到100的整数。')
    return copy.deepcopy(values)


def read_custom():
    if not PRESET_PATH.exists():
        return {}
    try:
        data = json.loads(PRESET_PATH.read_text(encoding='utf-8'))
        if data.get('version') != 1 or not isinstance(data.get('presets'), dict):
            raise ValueError('预设文件格式不正确。')
        for name, values in data['presets'].items():
            if not isinstance(name, str) or not name.strip() or len(name) > 40:
                raise ValueError('自定义预设名称无效。')
            validate_values(values)
        return data['presets']
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ValueError('无法读取自定义预设，请检查 facefusion-presets.json；原文件已保留。') from error


def list_choices():
    return [(value['label'], key) for key, value in BUILTINS.items()] + [(name + '（自定义）', 'custom:' + name) for name in read_custom()]


def resolve_preset(key):
    if key in BUILTINS:
        return copy.deepcopy(BUILTINS[key])
    if isinstance(key, str) and key.startswith('custom:'):
        name = key[len('custom:'):]
        values = read_custom().get(name)
        if values is not None:
            return {'label': name, 'description': '已应用自定义的人脸替换、表情恢复、人脸增强和遮罩参数。', 'values': values}
    raise ValueError('请先选择一个有效的预设。')


def snapshot():
    return {key: copy.deepcopy(state_manager.get_item(key)) for key in PRESET_KEYS}


def require_idle():
    if not process_manager.is_pending():
        raise ValueError('正在检查模型或处理视频，请完成或停止当前任务后再切换、保存预设。')


def apply_preset(key):
    require_idle()
    item = resolve_preset(key)
    values = validate_values(item['values'])
    previous = snapshot()
    previous_modules = get_processors_modules(previous['processors'])
    try:
        for name, value in values.items():
            state_manager.set_item(name, value)
        for module in get_processors_modules(values['processors']):
            if not module.pre_check():
                raise ValueError('预设所需模型检查失败，已恢复原参数。请查看运行日志。')
    except BaseException:
        for name, value in previous.items():
            state_manager.set_item(name, value)
        raise
    for module in previous_modules:
        module.clear_inference_pool()
    return item


def save_current(name):
    require_idle()
    if not isinstance(name, str):
        raise ValueError('请输入1到40个字符的预设名称。')
    name = name.strip()
    if not name or len(name) > 40 or any(ord(c) < 32 for c in name):
        raise ValueError('请输入1到40个字符的预设名称。')
    values = validate_values(snapshot())
    with STORE_LOCK:
        customs = read_custom()
        customs[name] = values
        PRESET_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix='.facefusion-presets-', suffix='.tmp', dir=PRESET_PATH.parent)
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                json.dump({'version': 1, 'presets': customs}, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            os.replace(temporary, PRESET_PATH)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return 'custom:' + name
