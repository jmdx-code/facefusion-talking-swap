from functools import partial

from facefusion import process_manager
from facefusion.types import ErrorCode
from facefusion.workflows.core import clear, setup
from facefusion.workflows.to_image import analyse_image, finalize_image, prepare_image, process_image


def process(start_time : float) -> ErrorCode:
	tasks =\
	[
		analyse_image,
		clear,
		setup,
		prepare_image,
		process_image,
		partial(finalize_image, start_time),
		clear
	]
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
