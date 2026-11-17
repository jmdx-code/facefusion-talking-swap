import html
import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional, Tuple

import gradio

from facefusion import metadata

HEADER_HTML : Optional[gradio.HTML] = None
UPDATE_BUTTON : Optional[gradio.Button] = None


def _load_auto_updater() -> Any:
	repo_root = Path(__file__).resolve().parents[5]
	script_path = repo_root / 'app' / 'scripts' / 'auto_updater.py'
	if not script_path.is_file():
		return None
	spec = importlib.util.spec_from_file_location('ff_root_auto_updater', script_path)
	if spec is None or spec.loader is None:
		return None
	mod = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = mod
	spec.loader.exec_module(mod)
	return mod


def _render_header_html(status_note : str = '') -> str:
	engine_ver = html.escape(str(metadata.get('version') or '3.9.0'))
	updater = _load_auto_updater()
	pkg_ver = html.escape(updater.read_local_version() if updater else '1.0.0')
	status_badge = ''
	if status_note:
		status_badge = (
			f'<span style="margin-left:0.6rem;padding:0.15rem 0.55rem;border-radius:999px;'
			f'background:#eef2ff;color:#4338ca;font-size:0.78rem;font-weight:600;">'
			f'{html.escape(status_note)}</span>'
		)
	return (
		f'<div class="ff-studio-bar" style="margin-bottom:0;">'
		f'<div class="ff-studio-brand">'
		f'<span class="ff-studio-logo">FF</span>'
		f'<div>'
		f'<div class="ff-studio-title">FaceFusion 口播换脸工作台 '
		f'<span style="font-weight:600;font-size:0.8rem;color:#4f46e5;">v{pkg_ver}</span>'
		f'<span style="font-weight:400;font-size:0.76rem;color:#94a3b8;margin-left:0.35rem;">(内核 {engine_ver})</span>'
		f'{status_badge}'
		f'</div>'
		f'<div class="ff-studio-sub">日常单条快速换脸 · Google 云表格团队协同接单 · 启动自动同步热更新</div>'
		f'</div>'
		f'</div>'
		f'</div>'
	)


def render() -> None:
	global HEADER_HTML
	global UPDATE_BUTTON

	with gradio.Row(equal_height = True):
		with gradio.Column(scale = 9, min_width = 320):
			HEADER_HTML = gradio.HTML(_render_header_html(), padding = False)
		with gradio.Column(scale = 2, min_width = 150):
			UPDATE_BUTTON = gradio.Button('🔄 检查在线热更新', size = 'sm', variant = 'secondary')


def on_check_hot_update() -> Tuple[str, gradio.Button]:
	updater = _load_auto_updater()
	if updater is None:
		gradio.Warning('未找到内置热更新模块 auto_updater.py。')
		return _render_header_html('未检测到更新模块'), gradio.Button(value = '🔄 检查在线热更新')

	res = updater.check_and_apply_update(apply_update = False, timeout_sec = 10.0)
	if res.status == 'update_available':
		gradio.Info(f'发现 v{res.remote_version}，请停止后重新启动软件；启动时自动安装，个人数据和模型保留。')
		return _render_header_html('发现新版，重启自动安装'), gradio.Button(value = '🔄 重启安装更新')
	if res.status == 'updated':
		gradio.Info(f'{res.message}（重启软件后新界面生效）')
		return _render_header_html(f'已热更新至 v{res.remote_version}（重启生效）'), gradio.Button(value = '✅ 已更新（重启生效）')
	if res.status == 'up_to_date':
		gradio.Info(res.message)
		return _render_header_html(f'已是最新版 v{res.local_version}'), gradio.Button(value = '🔄 检查在线热更新')
	gradio.Warning(res.message)
	return _render_header_html('检查更新暂不可用'), gradio.Button(value = '🔄 检查在线热更新')


def listen() -> None:
	if UPDATE_BUTTON is not None and HEADER_HTML is not None:
		UPDATE_BUTTON.click(
			on_check_hot_update,
			inputs = [],
			outputs = [ HEADER_HTML, UPDATE_BUTTON ],
			show_progress = 'minimal'
		)
