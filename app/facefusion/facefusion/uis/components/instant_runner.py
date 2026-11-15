import threading
from time import monotonic, sleep
from typing import Optional, Tuple

import gradio

from facefusion import logger, process_manager, state_manager, translator
from facefusion.args import collect_step_args
from facefusion.core import process_step
from facefusion.filesystem import has_image, is_directory, is_image, is_video
from facefusion.jobs import job_helper, job_manager, job_runner, job_store
from facefusion.temp_helper import clear_temp_directory
from facefusion.types import Args, UiWorkflow
from facefusion.uis.core import get_ui_component
from facefusion.uis.ui_helper import suggest_output_path

INSTANT_RUNNER_WRAPPER : Optional[gradio.Row] = None
INSTANT_RUNNER_START_BUTTON : Optional[gradio.Button] = None
INSTANT_RUNNER_STOP_BUTTON : Optional[gradio.Button] = None
INSTANT_RUNNER_CLEAR_BUTTON : Optional[gradio.Button] = None

_RUN_LOCK = threading.Lock()
_ACTIVE_RUNS = 0
_RUN_FINISHED_SEQ = 0


def render() -> None:
	global INSTANT_RUNNER_WRAPPER
	global INSTANT_RUNNER_START_BUTTON
	global INSTANT_RUNNER_STOP_BUTTON
	global INSTANT_RUNNER_CLEAR_BUTTON

	if job_manager.init_jobs(state_manager.get_item('jobs_path')):
		is_instant_runner = state_manager.get_item('ui_workflow') == 'instant_runner'

		with gradio.Row(visible = is_instant_runner) as INSTANT_RUNNER_WRAPPER:
			INSTANT_RUNNER_START_BUTTON = gradio.Button(
				value = '🚀 ' + translator.get('uis.start_button'),
				variant = 'primary',
				size = 'lg',
				scale = 3,
				min_width = 110
			)
			INSTANT_RUNNER_STOP_BUTTON = gradio.Button(
				value = '⏹ ' + translator.get('uis.stop_button'),
				variant = 'stop',
				size = 'lg',
				scale = 3,
				min_width = 110,
				visible = False
			)
			INSTANT_RUNNER_CLEAR_BUTTON = gradio.Button(
				value = translator.get('uis.clear_button'),
				size = 'lg',
				scale = 1,
				min_width = 76
			)


def listen() -> None:
	output_image = get_ui_component('output_image')
	output_video = get_ui_component('output_video')
	ui_workflow_dropdown = get_ui_component('ui_workflow_dropdown')

	if output_image and output_video:
		INSTANT_RUNNER_START_BUTTON.click(start, outputs = [ INSTANT_RUNNER_START_BUTTON, INSTANT_RUNNER_STOP_BUTTON ])
		INSTANT_RUNNER_START_BUTTON.click(run, outputs = [ INSTANT_RUNNER_START_BUTTON, INSTANT_RUNNER_STOP_BUTTON, output_image, output_video ])
		INSTANT_RUNNER_STOP_BUTTON.click(stop, outputs = [ INSTANT_RUNNER_START_BUTTON, INSTANT_RUNNER_STOP_BUTTON, output_image, output_video ])
		INSTANT_RUNNER_CLEAR_BUTTON.click(clear, outputs = [ output_image, output_video ])
	if ui_workflow_dropdown:
		ui_workflow_dropdown.change(remote_update, inputs = ui_workflow_dropdown, outputs = INSTANT_RUNNER_WRAPPER)


def remote_update(ui_workflow : UiWorkflow) -> gradio.Row:
	is_instant_runner = ui_workflow == 'instant_runner'

	return gradio.Row(visible = is_instant_runner)


def start() -> Tuple[gradio.Button, gradio.Button]:
	with _RUN_LOCK:
		seen_finished = _RUN_FINISHED_SEQ
	deadline = monotonic() + 1.5
	while not process_manager.is_processing():
		with _RUN_LOCK:
			active = _ACTIVE_RUNS
			finished_now = _RUN_FINISHED_SEQ
		if active == 0 and (finished_now != seen_finished or monotonic() >= deadline):
			return gradio.Button(), gradio.Button()
		sleep(0.05)
	return gradio.Button(visible = False), gradio.Button(visible = True)


def run() -> Tuple[gradio.Button, gradio.Button, gradio.Image, gradio.Video]:
	global _ACTIVE_RUNS
	global _RUN_FINISHED_SEQ

	with _RUN_LOCK:
		_ACTIVE_RUNS += 1
	try:
		step_args = collect_step_args()
		active_processors = step_args.get('processors') or state_manager.get_item('processors') or []
		needs_source = ('face_swapper' in active_processors) or ('deep_swapper' in active_processors)
		has_src = has_image(step_args.get('source_paths'))
		has_tgt = is_image(step_args.get('target_path')) or is_video(step_args.get('target_path'))
		if (needs_source and not has_src) or not has_tgt:
			if needs_source and not has_src and not has_tgt:
				warn_msg = '请先在【步骤 1】上传“源人脸图片”和“目标视频”，再点击开始处理！'
			elif needs_source and not has_src:
				warn_msg = '请先在【步骤 1】左侧上传一张清晰的“源人脸图片”！'
			else:
				warn_msg = '请先在【步骤 1】右侧上传需要换脸的“目标视频”！'
			logger.warn(warn_msg, __name__)
			gradio.Warning(warn_msg)
			return gradio.Button(visible = True), gradio.Button(visible = False), gradio.Image(value = None), gradio.Video(value = None)

		output_path = step_args.get('output_path')

		if is_directory(step_args.get('output_path')):
			step_args['output_path'] = suggest_output_path(step_args.get('output_path'), state_manager.get_item('target_path'))
		if job_manager.init_jobs(state_manager.get_item('jobs_path')):
			create_and_run_job(step_args)
			state_manager.set_item('output_path', output_path)
		if is_image(step_args.get('output_path')):
			return gradio.Button(visible = True), gradio.Button(visible = False), gradio.Image(value = step_args.get('output_path'), visible = True), gradio.Video(value = None, visible = False)
		if is_video(step_args.get('output_path')):
			return gradio.Button(visible = True), gradio.Button(visible = False), gradio.Image(value = None, visible = False), gradio.Video(value = step_args.get('output_path'), visible = True)
		return gradio.Button(visible = True), gradio.Button(visible = False), gradio.Image(value = None), gradio.Video(value = None)
	finally:
		with _RUN_LOCK:
			_ACTIVE_RUNS = max(0, _ACTIVE_RUNS - 1)
			_RUN_FINISHED_SEQ += 1


def create_and_run_job(step_args : Args) -> bool:
	job_id = job_helper.suggest_job_id('ui')

	for key in job_store.get_job_keys():
		state_manager.sync_item(key) #type:ignore[arg-type]

	return job_manager.create_job(job_id) and job_manager.add_step(job_id, step_args) and job_manager.submit_job(job_id) and job_runner.run_job(job_id, process_step)


def stop() -> Tuple[gradio.Button, gradio.Button, gradio.Image, gradio.Video]:
	process_manager.stop()
	return gradio.Button(visible = True), gradio.Button(visible = False), gradio.Image(value = None), gradio.Video(value = None)


def clear() -> Tuple[gradio.Image, gradio.Video]:
	while process_manager.is_processing():
		sleep(0.5)
	if state_manager.get_item('target_path'):
		clear_temp_directory(state_manager.get_item('target_path'))
	return gradio.Image(value = None), gradio.Video(value = None)
