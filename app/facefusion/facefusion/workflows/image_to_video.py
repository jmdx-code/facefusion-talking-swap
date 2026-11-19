from functools import partial

from facefusion import process_manager, state_manager
from facefusion.types import ErrorCode
from facefusion.workflows.core import clear, setup
from facefusion.workflows.to_video import analyse_video, extract_frames, finalize_video, merge_frames, process_disk_frames, process_memory_frames, restore_audio
from facefusion.workflows.staged_video import supported, process_staged_frames, has_disk_budget


def release_analysis_models():
    from facefusion.workflows.staged_video import release_pools
    release_pools()
    return 0


def process_stream_fallback():
    previous=state_manager.get_item('execution_thread_count')
    state_manager.set_item('execution_thread_count',1)
    try:
        return process_memory_frames()
    finally:
        state_manager.set_item('execution_thread_count',previous)


def process_staged_or_fallback():
    from facefusion.roi_cache import CacheSpaceError
    from facefusion.gpu_memory import GpuMemoryError, forget_exception_frames
    from facefusion import logger, video_manager
    from facefusion.workflows.staged_video import WORKER_TIERS, available_vram_info, choose_workers, release_pools, worker_tier_index
    max_tier = None
    while True:
        try:
            return process_staged_frames(max_tier=max_tier) if max_tier is not None else process_staged_frames()
        except CacheSpaceError:
            if process_manager.is_stopping() or process_manager.is_pending():
                return 4
            logger.warn('FFV1缓存磁盘余量不足，已释放临时缓存，切换低并发流式处理。', __name__)
            clear()
            setup()
            return process_stream_fallback()
        except GpuMemoryError as error:
            failed_workers = error.workers
            detail = ' '.join(str(error).split())[:2000]
            # Unwind worker tracebacks before collecting released ORT sessions.
            forget_exception_frames(error)
        if process_manager.is_stopping() or process_manager.is_pending():
            return 4
        video_manager.clear_video_pool()
        release_pools()
        if process_manager.is_stopping() or process_manager.is_pending():
            return 4
        if failed_workers is None:
            free_mib, total_mib = available_vram_info()
            failed_workers = choose_workers(free_mib, max_tier=max_tier, total_mib=total_mib)
        next_tier = worker_tier_index(failed_workers) - 1
        if next_tier < 0:
            logger.error('gpu_memory_exhausted: 最低并发档仍显存不足，请释放其他程序的显存后重试。' + detail, __name__)
            return 1
        max_tier = next_tier
        if state_manager.get_item('command') in (None, 'run'):
            from facefusion.gpu_status import GPU_STATUS
            GPU_STATUS.note_retry('manual')
        logger.warn('gpu_memory_retry: %s档显存不足，已释放模型与临时缓存，降为%s档从头重试；画质参数不变。' % (
            failed_workers['tier'], WORKER_TIERS[max_tier]['tier']), __name__)
        clear()
        if process_manager.is_stopping() or process_manager.is_pending():
            return 4
        setup()


def process(start_time : float) -> ErrorCode:
	tasks =\
	[
		analyse_video,
		release_analysis_models,
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
	telemetry_token = None
	succeeded = False
	error_code = None
	if state_manager.get_item('command') in (None, 'run'):
		from facefusion.gpu_status import GPU_STATUS
		from pathlib import Path
		import os
		telemetry_token = GPU_STATUS.begin('manual', Path(state_manager.get_item('target_path')).name,
			os.getpid(), state_manager.get_item('execution_device_ids'))
	if process_staged_or_fallback not in tasks:
		from facefusion.gpu_status import publish_tier
		publish_tier(dict(tier='兼容流程', swap=state_manager.get_item('execution_thread_count') or 1), None, None)

	try:
		for task in tasks:
			error_code = task()
			if error_code > 0:
				return error_code
		succeeded = True
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
			if telemetry_token is not None:
				GPU_STATUS.finish('manual', telemetry_token, '已完成' if succeeded else ('已停止' if error_code == 4 else '处理未完成'))
