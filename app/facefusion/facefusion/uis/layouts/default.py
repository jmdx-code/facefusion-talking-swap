import html

import gradio

from facefusion import metadata
from facefusion.uis.components import about, age_modifier_options, background_remover_options, deep_swapper_options, download, execution, execution_thread_count, expression_restorer_options, face_debugger_options, face_detector, face_editor_options, face_enhancer_options, face_landmarker, face_masker, face_selector, face_swapper_options, face_tracker, frame_colorizer_options, frame_enhancer_options, instant_runner, job_manager, job_runner, lip_syncer_options, memory, output, output_options, presets, preview, preview_options, processors, sheet_worker, source, target, temp_frame, terminal, trim_frame, ui_workflow, updater_bar, voice_extractor, workflow
from facefusion.uis.core import launch_gradio_ui
from facefusion.uis.components import gpu_status


def pre_check() -> bool:
	return True


def _step_header(num: str, title: str, hint: str = '') -> None:
	hint_html = f'<span class="ff-step-hint">{html.escape(hint)}</span>' if hint else ''
	gradio.HTML(
		f'<div class="ff-step-head">'
		f'<div class="ff-step-head-left">'
		f'<span class="ff-step-badge">{html.escape(num)}</span>'
		f'<span class="ff-step-title">{html.escape(title)}</span>'
		f'</div>'
		f'{hint_html}'
		f'</div>',
		padding = False
	)


def render() -> gradio.Blocks:
	with gradio.Blocks() as layout:
		updater_bar.render()

		with gradio.Tabs(elem_classes = [ 'ff-main-tabs' ]):
			with gradio.Tab('🎬 手动精细换脸'):
				with gradio.Row():
					# Left column: 3 compact steps that fit above the fold on 1280x800 laptops
					with gradio.Column(scale = 5, min_width = 360):
						with gradio.Column(elem_classes = [ 'ff-card-panel' ]):
							_step_header('1', '上传换脸素材', '清晰正脸照 + 目标视频')
							with gradio.Row():
								with gradio.Column(scale = 1, min_width = 145):
									source.render()
								with gradio.Column(scale = 1, min_width = 145):
									target.render()

						with gradio.Column(elem_classes = [ 'ff-card-panel' ]):
							_step_header('2', '选择场景效果', '默认第 1 个即可，异常再切换')
							presets.render()

						with gradio.Column(elem_classes = [ 'ff-card-panel' ]):
							_step_header('3', '开始制作与导出', '完成后右侧直接播放或打开目录')
							instant_runner.render()
							gpu_status.render('manual')
							terminal.render_progress()
							output.render_path()

					# Right column: Live single-frame preview + finished output media
					with gradio.Column(scale = 7, min_width = 400):
						with gradio.Column(elem_classes = [ 'ff-card-panel' ]):
							_step_header('预览', '单帧换脸效果实时预览', '上传素材或切换预设后自动刷新')
							preview.render()
							with gradio.Accordion('预览画面调节（切换对比模式 / 拖动预览帧）', open = False):
								preview_options.render()

						with gradio.Column(elem_classes = [ 'ff-card-panel' ]):
							_step_header('成品', '生成结果预览与下载', '换脸完成后在此播放')
							output.render_result()

				# Collapsed professional settings drawer — keeps 100% power-user controls while eliminating clutter
				with gradio.Accordion('🛠️ 高级参数微调（多人画面选脸 / 片段截取 / 模型与编码器设置 — 日常使用无需展开）', open = False):
					with gradio.Tabs(elem_classes = [ 'ff-sub-tabs' ]):
						with gradio.Tab('🎯 多人选脸与片段裁剪'):
							with gradio.Row():
								with gradio.Column(scale = 1):
									trim_frame.render()
									face_selector.render()
								with gradio.Column(scale = 1):
									face_masker.render()
									face_detector.render()
									face_landmarker.render()
									face_tracker.render()

						with gradio.Tab('🧠 模型与画质参数'):
							with gradio.Row():
								with gradio.Column(scale = 1):
									processors.render()
									face_swapper_options.render()
								with gradio.Column(scale = 1):
									expression_restorer_options.render()
									face_enhancer_options.render()
							with gradio.Accordion('其他扩展处理器参数（年龄 / 背景 / 唇形同步等）', open = False):
								age_modifier_options.render()
								background_remover_options.render()
								deep_swapper_options.render()
								face_debugger_options.render()
								face_editor_options.render()
								frame_colorizer_options.render()
								frame_enhancer_options.render()
								lip_syncer_options.render()
								voice_extractor.render()
								workflow.render()

						with gradio.Tab('⚡ 输出编码与硬件设置'):
							with gradio.Row():
								with gradio.Column(scale = 1):
									output_options.render()
								with gradio.Column(scale = 1):
									execution.render()
									execution_thread_count.render()
									memory.render()
									temp_frame.render()
									download.render()
							with gradio.Accordion('批处理任务流与日志级别', open = False):
								ui_workflow.render()
								job_runner.render()
								job_manager.render()
								terminal.render_log_level()
								about.render()

			with gradio.Tab('📋 云表格任务中心'):
				gpu_status.render('sheet')
				sheet_worker.render()
	return layout


def listen() -> None:
	processors.listen()
	age_modifier_options.listen()
	background_remover_options.listen()
	deep_swapper_options.listen()
	expression_restorer_options.listen()
	face_debugger_options.listen()
	face_editor_options.listen()
	face_enhancer_options.listen()
	face_swapper_options.listen()
	frame_colorizer_options.listen()
	frame_enhancer_options.listen()
	lip_syncer_options.listen()
	execution.listen()
	execution_thread_count.listen()
	download.listen()
	memory.listen()
	temp_frame.listen()
	output_options.listen()
	source.listen()
	target.listen()
	output.listen()
	instant_runner.listen()
	job_runner.listen()
	job_manager.listen()
	terminal.listen()
	preview.listen()
	preview_options.listen()
	trim_frame.listen()
	face_selector.listen()
	face_tracker.listen()
	face_masker.listen()
	face_detector.listen()
	face_landmarker.listen()
	voice_extractor.listen()
	workflow.listen()
	presets.listen()
	sheet_worker.listen()
	updater_bar.listen()
	gpu_status.listen()


def run(ui : gradio.Blocks) -> None:
	from facefusion.gpu_status import GPU_STATUS
	GPU_STATUS.enable()
	launch_gradio_ui(ui)
