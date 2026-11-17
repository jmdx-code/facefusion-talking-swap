import html
from typing import Optional

import gradio

from facefusion import presets, state_manager
from facefusion.processors.core import load_processor_module
from facefusion.processors.modules.face_swapper import choices as swapper_choices
from facefusion.uis.components import face_masker, face_swapper_options, preview, processors
from facefusion.uis.core import get_ui_component, register_ui_component

PRESET_DROPDOWN: Optional[gradio.Dropdown] = None
QUICK_CLEAR_BUTTON: Optional[gradio.Button] = None
QUICK_MOUTH_BUTTON: Optional[gradio.Button] = None
QUICK_EYE_BUTTON: Optional[gradio.Button] = None
APPLY_BUTTON: Optional[gradio.Button] = None
SAVE_BUTTON: Optional[gradio.Button] = None
NAME_TEXTBOX: Optional[gradio.Textbox] = None
INFO_TEXTBOX: Optional[gradio.HTML] = None

FIELDS = [
    ('temple_correction', 'temple_correction_dropdown'),
    ('processors', 'processors_checkbox_group'),
    ('face_swapper_model', 'face_swapper_model_dropdown'),
    ('face_swapper_pixel_boost', 'face_swapper_pixel_boost_dropdown'),
    ('face_swapper_weight', 'face_swapper_weight_slider'),
    ('expression_restorer_model', 'expression_restorer_model_dropdown'),
    ('expression_restorer_factor', 'expression_restorer_factor_slider'),
    ('expression_restorer_areas', 'expression_restorer_areas_checkbox_group'),
    ('face_enhancer_model', 'face_enhancer_model_dropdown'),
    ('face_enhancer_blend', 'face_enhancer_blend_slider'),
    ('face_enhancer_weight', 'face_enhancer_weight_slider'),
    ('face_mask_types', 'face_mask_types_checkbox_group'),
    ('face_mask_areas', 'face_mask_areas_checkbox_group'),
    ('face_mask_regions', 'face_mask_regions_checkbox_group'),
    ('face_mask_blur', 'face_mask_blur_slider'),
    ('face_parser_model', 'face_parser_model_dropdown'),
    ('face_occluder_model', 'face_occluder_model_dropdown')
]
PADDING_COMPONENTS = ['face_mask_padding_top_slider', 'face_mask_padding_right_slider', 'face_mask_padding_bottom_slider', 'face_mask_padding_left_slider']


def _detect_initial_preset() -> Optional[str]:
    try:
        cur = presets.snapshot()
        if isinstance(cur.get('face_mask_padding'), tuple):
            cur['face_mask_padding'] = list(cur['face_mask_padding'])
        for key, item in presets.BUILTINS.items():
            if cur == item['values']:
                return key
    except Exception:
        pass
    return presets.DEFAULT_PRESET


def _format_info_html(title: str, desc: str) -> str:
    return (
        f'<div class="ff-preset-desc">'
        f'<b>当前生效：{html.escape(title)}</b> — {html.escape(desc)}'
        f'</div>'
    )


def render() -> None:
    global PRESET_DROPDOWN, QUICK_CLEAR_BUTTON, QUICK_MOUTH_BUTTON, QUICK_EYE_BUTTON
    global APPLY_BUTTON, SAVE_BUTTON, NAME_TEXTBOX, INFO_TEXTBOX

    init_key = _detect_initial_preset()
    default_item = presets.BUILTINS.get(init_key or presets.DEFAULT_PRESET, presets.BUILTINS[presets.DEFAULT_PRESET])
    info_html = _format_info_html(default_item['label'], default_item['description'])
    try:
        choices = presets.list_choices()
    except ValueError as error:
        choices = [(item['label'], key) for key, item in presets.BUILTINS.items()]
        info_html = _format_info_html('预设读取提示', str(error))

    from facefusion.video_options import save_upscale_preference
    upscale = gradio.Checkbox(label='低分辨率视频提升至1080p（手动与自动接单共用）', value=state_manager.get_item('upscale_1080p') is not False, info='等比放大，保持竖屏；1080p及以上不缩小。关闭后按原分辨率处理。')
    upscale.input(save_upscale_preference, inputs=upscale)

    with gradio.Row(elem_classes=['ff-preset-row']):
        QUICK_CLEAR_BUTTON = gradio.Button(
            '✨ 没问题选这个',
            variant='primary' if init_key == presets.DEFAULT_PRESET else 'secondary',
            size='sm',
            min_width=88
        )
        QUICK_MOUTH_BUTTON = gradio.Button(
            '👄 解决嘴巴张不开',
            variant='primary' if init_key == 'mouth_restore' else 'secondary',
            size='sm',
            min_width=88
        )
        QUICK_EYE_BUTTON = gradio.Button(
            '👀 解决眼睛斜',
            variant='primary' if init_key == 'eye_restore' else 'secondary',
            size='sm',
            min_width=88
        )

    INFO_TEXTBOX = gradio.HTML(value=info_html, padding=False)

    with gradio.Accordion('更多预设 / 保存自定义预设', open=False):
        temple_option = gradio.Dropdown(label='太阳穴局部修正（增强后执行）', choices=[('开启', 'on'), ('关闭', 'off')], value=state_manager.get_item('temple_correction') or 'off')
        register_ui_component('temple_correction_dropdown', temple_option)
        temple_option.change(lambda value: state_manager.set_item('temple_correction', value), inputs=temple_option)
        with gradio.Row():
            PRESET_DROPDOWN = gradio.Dropdown(
                label='全部预设列表（含自定义预设，选择即生效）',
                choices=choices,
                value=init_key,
                scale=3,
                min_width=160
            )
            APPLY_BUTTON = gradio.Button('重新应用', size='sm', scale=1, min_width=76)
        with gradio.Row():
            NAME_TEXTBOX = gradio.Textbox(
                label='保存当前参数为自定义预设',
                placeholder='输入名称，例如：室内口播自然款',
                max_length=40,
                max_lines=1,
                scale=3,
                min_width=160
            )
            SAVE_BUTTON = gradio.Button('保存预设', size='sm', scale=1, min_width=76)


def _button_variants(active_key: Optional[str]):
    return (
        gradio.Button(variant='primary' if active_key == presets.DEFAULT_PRESET else 'secondary'),
        gradio.Button(variant='primary' if active_key == 'mouth_restore' else 'secondary'),
        gradio.Button(variant='primary' if active_key == 'eye_restore' else 'secondary'),
    )


def listen() -> None:
    outputs = [get_ui_component(name) for _, name in FIELDS]
    outputs += [get_ui_component(name) for name in PADDING_COMPONENTS]
    outputs += [
        face_masker.FACE_MASK_BOX_WRAPPER,
        INFO_TEXTBOX,
        PRESET_DROPDOWN,
        QUICK_CLEAR_BUTTON,
        QUICK_MOUTH_BUTTON,
        QUICK_EYE_BUTTON,
    ]

    preview_inputs = [
        get_ui_component('preview_mode_dropdown'),
        get_ui_component('preview_resolution_dropdown'),
        get_ui_component('preview_frame_slider')
    ]
    preview_out = get_ui_component('preview_image')

    for event in [PRESET_DROPDOWN.input, APPLY_BUTTON.click]:
        dep = event(apply_preset, inputs=PRESET_DROPDOWN, outputs=outputs, concurrency_id='face_presets', concurrency_limit=1, show_progress='minimal')
        dep.success(preview.clear_and_update_preview_image, inputs=preview_inputs, outputs=preview_out, show_progress='minimal')

    for btn, preset_key in [
        (QUICK_CLEAR_BUTTON, presets.DEFAULT_PRESET),
        (QUICK_MOUTH_BUTTON, 'mouth_restore'),
        (QUICK_EYE_BUTTON, 'eye_restore')
    ]:
        dep_q = btn.click(
            lambda k=preset_key: apply_preset(k),
            outputs=outputs,
            concurrency_id='face_presets',
            concurrency_limit=1,
            show_progress='minimal'
        )
        dep_q.success(preview.clear_and_update_preview_image, inputs=preview_inputs, outputs=preview_out, show_progress='minimal')

    SAVE_BUTTON.click(
        save_preset,
        inputs=NAME_TEXTBOX,
        outputs=[PRESET_DROPDOWN, INFO_TEXTBOX, QUICK_CLEAR_BUTTON, QUICK_MOUTH_BUTTON, QUICK_EYE_BUTTON],
        concurrency_id='face_presets',
        concurrency_limit=1,
        show_progress='minimal'
    )


def apply_preset(key):
    try:
        item = presets.apply_preset(key)
    except (ValueError, OSError) as error:
        gradio.Warning(str(error))
        return tuple(gradio.skip() for _ in range(len(FIELDS) + 9))
    values = item['values']
    selected = values['processors']
    types = values['face_mask_types']
    updates = []
    for name, _ in FIELDS:
        options = {'value': state_manager.get_item(name)}
        if name == 'processors':
            options['choices'] = processors.sort_processors(selected)
        elif name.startswith('expression_restorer_'):
            options['visible'] = 'expression_restorer' in selected
        elif name.startswith('face_enhancer_'):
            options['visible'] = 'face_enhancer' in selected
            if name == 'face_enhancer_weight':
                options['visible'] = options['visible'] and load_processor_module('face_enhancer').has_weight_input()
        elif name.startswith('face_swapper_'):
            options['visible'] = True
            if name == 'face_swapper_pixel_boost':
                options['choices'] = swapper_choices.face_swapper_set[values['face_swapper_model']]
            if name == 'face_swapper_weight':
                options['visible'] = face_swapper_options.has_face_swapper_weight()
        elif name == 'face_mask_regions':
            options['visible'] = 'region' in types
        elif name == 'face_mask_areas':
            options['visible'] = 'area' in types
        elif name == 'face_mask_blur':
            options['visible'] = 'box' in types
        updates.append(gradio.update(**options))
    updates += [gradio.update(value=value) for value in values['face_mask_padding']]
    updates += [
        gradio.update(visible='box' in types),
        gradio.update(value=_format_info_html(item['label'], item['description'])),
        gradio.update(value=key),
        *_button_variants(key),
    ]
    return tuple(updates)


def save_preset(name):
    try:
        key = presets.save_current(name)
        gradio.Info(f'已保存自定义预设：{name.strip()}')
        return (
            gradio.update(choices=presets.list_choices(), value=key),
            gradio.update(value=_format_info_html(f'{name.strip()}（已保存）', '下次启动仍可在“更多预设”列表中直接选用。')),
            *_button_variants(key),
        )
    except (ValueError, OSError) as error:
        gradio.Warning(str(error))
        return tuple(gradio.skip() for _ in range(5))
