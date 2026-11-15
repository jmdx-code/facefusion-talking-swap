from typing import List, Optional, Tuple

import gradio

from facefusion import state_manager, translator
from facefusion.filesystem import get_file_name, resolve_file_paths
from facefusion.processors.core import get_processors_modules
from facefusion.uis.core import register_ui_component

PROCESSORS_CHECKBOX_GROUP : Optional[gradio.CheckboxGroup] = None


def render() -> None:
	global PROCESSORS_CHECKBOX_GROUP

	PROCESSORS_CHECKBOX_GROUP = gradio.CheckboxGroup(
		label = translator.get('uis.processors_checkbox_group'),
		choices = sort_processors(state_manager.get_item('processors')),
		value = state_manager.get_item('processors')
	)
	register_ui_component('processors_checkbox_group', PROCESSORS_CHECKBOX_GROUP)


def listen() -> None:
	PROCESSORS_CHECKBOX_GROUP.input(update_processors, inputs = PROCESSORS_CHECKBOX_GROUP, outputs = PROCESSORS_CHECKBOX_GROUP)


def update_processors(processors : List[str]) -> gradio.CheckboxGroup:
	for processor_module in get_processors_modules(state_manager.get_item('processors')):
		if hasattr(processor_module, 'clear_inference_pool'):
			processor_module.clear_inference_pool()

	for processor_module in get_processors_modules(processors):
		if not processor_module.pre_check():
			return gradio.CheckboxGroup()

	state_manager.set_item('processors', processors)
	return gradio.CheckboxGroup(value = state_manager.get_item('processors'), choices = sort_processors(state_manager.get_item('processors')))


PROCESSOR_LABELS =\
{
	'face_swapper': '人脸替换（必选）',
	'face_enhancer': '人脸增强',
	'frame_enhancer': '整个画面增强',
	'lip_syncer': '口型同步',
	'expression_restorer': '表情恢复',
	'background_remover': '移除背景',
	'age_modifier': '修改年龄',
	'frame_colorizer': '画面上色',
	'face_editor': '面部编辑',
	'face_debugger': '人脸检测调试',
	'deep_swapper': '深度换脸'
}


def sort_processors(processors : List[str]) -> List[Tuple[str, str]]:
	available_processors = [ get_file_name(file_path) for file_path in resolve_file_paths('facefusion/processors/modules') ]
	current_processors = []

	for processor in processors + available_processors:
		if processor in available_processors and processor not in current_processors:
			current_processors.append(processor)

	return [ (PROCESSOR_LABELS.get(processor, processor), processor) for processor in current_processors ]
