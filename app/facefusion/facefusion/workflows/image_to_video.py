from functools import partial

from facefusion import process_manager, state_manager
from facefusion.types import ErrorCode
from facefusion.workflows.core import clear, setup
from facefusion.workflows.to_video import analyse_video, extract_frames, finalize_video, merge_frames, process_disk_frames, process_memory_frames, restore_audio
from facefusion.workflows.staged_video import supported, process_staged_frames, has_disk_budget


def process_stream_fallback():
    previous=state_manager.get_item('execution_thread_count')
    state_manager.set_item('execution_thread_count',1)
    try:
        return process_memory_frames()
    finally:
        state_manager.set_item('execution_thread_count',previous)


def process_staged_or_fallback():
    from facefusion.roi_cache import CacheSpaceError
    try:
        return process_staged_frames()
    except CacheSpaceError:
        from facefusion import logger
        logger.warn('FFV1缓存磁盘余量不足，已释放临时缓存，切换低并发流式处理。', __name__)
        clear()
        setup()
        return process_stream_fallback()


def process(start_time : float) -> ErrorCode:
	tasks =\
	[
		analyse_video,
		clear,
		setup
	]

	if state_manager.get_item('workflow_strategy') == 'disk':
		use_staged = supported()
		if use_staged:
			tasks.append(process_staged_or_fallback)
		elif not has_disk_budget(include_stage=False):
			tasks.append(process_stream_fallback)
		else:
			tasks.extend([extract_frames, process_disk_frames, merge_frames])

	if state_manager.get_item('workflow_strategy') == 'memory':
		tasks.append(process_memory_frames)

	tasks.extend(
	[
		restore_audio,
		partial(finalize_video, start_time),
		clear
	])
	process_manager.start()

	try:
		for task in tasks:
			error_code = task()
			if error_code > 0:
				return error_code
		return 0
	finally:
		from facefusion import video_manager, logger
		try:
			video_manager.clear_video_pool()
		except (OSError, ValueError):
			pass
		try:
			clear()
		except OSError as error:
			logger.warn('临时目录暂未释放，下次启动会重试清理：' + str(error), __name__)
		finally:
			from facefusion.vision import read_static_image
			read_static_image.cache_clear()
			process_manager.end()
