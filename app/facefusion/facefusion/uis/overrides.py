import os
from pathlib import Path
from typing import Any, List

import gradio
import gradio.processing_utils
from gradio.exceptions import InvalidPathError

from facefusion import ffmpeg_builder, state_manager
from facefusion.ffmpeg import run_ffmpeg
from facefusion.filesystem import get_file_size
from facefusion.temp_helper import create_temp_directory, get_temp_file_path
from facefusion.uis.types import MockArgs
from facefusion.userdata_paths import get_package_root, get_userdata_dir

_ORIG_GRADIO_CHECK_ALLOWED = gradio.processing_utils._check_allowed

_SENSITIVE_FILENAMES = {
	'facefusion-google-token.json',
	'facefusion-google-client-secret.json',
	'facefusion-sheet-worker.json',
	'credentials.json',
	'token.json',
	'.env',
}


def is_sensitive_local_path(raw_path : str | Path) -> bool:
	raw_str = str(raw_path or '').strip()
	if raw_str.startswith(('\\\\', '//')):
		return True
	p = Path(raw_str)
	lower_name = p.name.lower()
	if lower_name in _SENSITIVE_FILENAMES or (lower_name.startswith('.env.') and lower_name != '.env.example'):
		return True
	if lower_name.endswith(('.pem', '.key', '.pfx', '.p12')):
		return True
	if lower_name.endswith('.json') and any(k in lower_name for k in ('client_secret', 'google-token', 'service_account', 'service-account')):
		return True
	lower_parts = {part.lower() for part in p.parts}
	if '.git' in lower_parts:
		return True
	return False


def get_blocked_ui_paths() -> List[str]:
	ud = get_userdata_dir()
	pkg = get_package_root()
	return [
		str(ud / 'facefusion-google-token.json'),
		str(ud / 'facefusion-google-client-secret.json'),
		str(ud / 'facefusion-sheet-worker.json'),
		str(pkg / '.git'),
	]


def _get_server_allowed_media_roots() -> List[Path]:
	ud = get_userdata_dir()
	roots : List[Path] = [
		ud / 'manual_outputs',
		ud / 'sheet_outputs',
		ud / 'sheet_failed_outputs',
	]
	for key in ('temp_path', 'output_path', 'target_path'):
		val = state_manager.get_item(key)
		if isinstance(val, str) and val.strip():
			p = Path(val.strip())
			roots.append(p if (p.exists() and p.is_dir()) or not p.suffix else p.parent)
	src_paths = state_manager.get_item('source_paths')
	if isinstance(src_paths, list):
		for item in src_paths:
			if isinstance(item, str) and item.strip():
				roots.append(Path(item.strip()).parent)
	resolved : List[Path] = []
	for r in roots:
		try:
			resolved.append(r.resolve())
		except OSError:
			continue
	return resolved


def check_allowed_path(path : str | Path, check_in_upload_folder : bool = False) -> None:
	if is_sensitive_local_path(path):
		raise InvalidPathError(f'Access to protected file or UNC path is forbidden: {path}')
	if check_in_upload_folder:
		_ORIG_GRADIO_CHECK_ALLOWED(path, True)
		return
	try:
		_ORIG_GRADIO_CHECK_ALLOWED(path, False)
		return
	except InvalidPathError:
		try:
			abs_target = Path(os.path.abspath(str(path))).resolve()
		except OSError:
			raise
		for allowed_root in _get_server_allowed_media_roots():
			try:
				abs_target.relative_to(allowed_root)
				return
			except ValueError:
				continue
		raise


def convert_video_to_playable_mp4(video_path : str) -> str:
	video_file_size = get_file_size(video_path)
	max_file_size = 512 * 1024 * 1024

	create_temp_directory(video_path)
	temp_video_path = get_temp_file_path(video_path)
	commands = ffmpeg_builder.set_input(video_path)

	if video_file_size > max_file_size:
		commands.extend(ffmpeg_builder.set_video_duration(10))

	commands.extend(ffmpeg_builder.force_output(temp_video_path))

	process = run_ffmpeg(commands)
	process.communicate()

	if process.returncode == 0:
		return temp_video_path

	return video_path


def mock(*args : MockArgs, **kwargs : MockArgs) -> None:
	pass
