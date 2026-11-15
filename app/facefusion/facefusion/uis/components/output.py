import os
import tempfile
from pathlib import Path
from typing import Optional

import gradio

from facefusion import state_manager, translator
from facefusion.uis.core import register_ui_component

OUTPUT_PATH_TEXTBOX : Optional[gradio.Textbox] = None
OPEN_OUTPUT_BUTTON : Optional[gradio.Button] = None
OUTPUT_IMAGE : Optional[gradio.Image] = None
OUTPUT_VIDEO : Optional[gradio.Video] = None


def _ensure_default_output_path() -> None:
	if not state_manager.get_item('output_path'):
		documents_directory = Path.home().joinpath('Documents')
		if documents_directory.exists():
			state_manager.set_item('output_path', str(documents_directory))
		else:
			state_manager.set_item('output_path', tempfile.gettempdir())


def render_path() -> None:
	global OUTPUT_PATH_TEXTBOX
	global OPEN_OUTPUT_BUTTON

	_ensure_default_output_path()
	OPEN_OUTPUT_BUTTON = gradio.Button(
		value = '📂 打开成品保存文件夹',
		size = 'sm'
	)
	with gradio.Accordion('修改成品保存目录（默认保存在 userdata\\manual_outputs）', open = False):
		OUTPUT_PATH_TEXTBOX = gradio.Textbox(
			label = translator.get('uis.output_path_textbox'),
			value = state_manager.get_item('output_path'),
			max_lines = 1
		)


def render_result() -> None:
	global OUTPUT_IMAGE
	global OUTPUT_VIDEO

	OUTPUT_IMAGE = gradio.Image(
		label = translator.get('uis.output_image_or_video'),
		height = 215,
		visible = False
	)
	OUTPUT_VIDEO = gradio.Video(
		label = translator.get('uis.output_image_or_video'),
		height = 215
	)


def render() -> None:
	render_path()
	render_result()


def listen() -> None:
	OUTPUT_PATH_TEXTBOX.change(update_output_path, inputs = OUTPUT_PATH_TEXTBOX)
	if OPEN_OUTPUT_BUTTON is not None:
		OPEN_OUTPUT_BUTTON.click(open_output_directory, inputs = OUTPUT_PATH_TEXTBOX)
	register_ui_component('output_image', OUTPUT_IMAGE)
	register_ui_component('output_video', OUTPUT_VIDEO)


def update_output_path(output_path : str) -> None:
	state_manager.set_item('output_path', output_path)


def open_output_directory(output_path : str) -> None:
	raw = (output_path or state_manager.get_item('output_path') or '').strip()
	if not raw:
		raise gradio.Error('请先设置输出路径。')
	if raw.startswith(('\\\\', '//')) or '://' in raw:
		raise gradio.Error('安全拦截：不支持直接打开网络共享（UNC）或远程协议路径。')
	target = Path(raw)
	folder = target if (target.exists() and target.is_dir()) or not target.suffix else target.parent
	try:
		folder.mkdir(parents = True, exist_ok = True)
		if os.name == 'nt':
			os.startfile(str(folder.resolve())) #type:ignore[attr-defined]
		gradio.Info(f'已打开目录：{folder}')
	except OSError as exc:
		raise gradio.Error(f'无法打开目录：{exc}') from exc
