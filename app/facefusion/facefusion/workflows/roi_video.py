"""Three complete video passes with native-size face regions and lossless FFV1.

Model changes happen between whole-video stages, never between cache segments.
Only small bounded frame queues and face geometry stay in RAM.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

import cv2
import numpy as np
from tqdm import tqdm

from facefusion import logger, process_manager, state_manager, thread_helper, video_manager
from facefusion.face_creator import get_many_faces, get_one_face
from facefusion.face_helper import calculate_paste_area, warp_face_by_face_landmark_5
from facefusion.face_selector import sort_faces_by_order
from facefusion.filesystem import filter_image_paths
from facefusion.gpu_memory import GpuMemoryError, is_gpu_memory_error
from facefusion.processors.modules.face_swapper import core as swapper
from facefusion.processors.modules.expression_restorer import core as restorer
from facefusion.processors.modules.face_enhancer import core as enhancer
from facefusion.roi_cache import CacheSpaceError, PipelineCancelled, ProcessScope, CacheStats, SegmentWriter, SegmentReader, check_cancelled, read_exact
from facefusion.runtime_cleanup import safe_remove
from facefusion.temp_helper import get_temp_directory_path
from facefusion.temple_correction import correct_temple
from facefusion.video_options import output_resolution, processing_resolution
from facefusion.vision import read_static_images, restrict_trim_frame, restrict_video_fps, predict_video_frame_total, unpack_resolution
from facefusion.workflows.staged_video import available_vram, choose_workers, release_pools, bounded_map


class OriginalFrames:
    def __init__(self, path, resolution, fps, start, end, scope, expected=None):
        self.path, self.resolution, self.fps = path, resolution, fps
        self.start, self.end, self.scope, self.expected = start, end, scope, expected

    def __iter__(self):
        width, height = self.resolution
        filters = [f'trim=start_frame={self.start}:end_frame={self.end}',
                   'setpts=PTS-STARTPTS', f'fps={self.fps}:start_time=0',
                   f'scale={width}:{height}:flags=bicubic']
        process = self.scope.start([
            shutil.which('ffmpeg'), '-v', 'error', '-threads', '2', '-filter_threads', '2',
            '-i', self.path, '-vf', ','.join(filters), '-pix_fmt', 'rgb24',
            '-f', 'rawvideo', '-fps_mode', 'passthrough', 'pipe:1'
        ], stdout=subprocess.PIPE)
        frame_number = 0
        while True:
            data = read_exact(process, width * height * 3)
            if data is None:
                break
            if self.expected is not None and frame_number >= self.expected:
                raise OSError('重复解码得到额外视频帧')
            rgb = np.frombuffer(data, np.uint8).reshape(height, width, 3)
            yield frame_number, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            frame_number += 1
        self.scope.finish(process)
        if self.expected is not None and frame_number != self.expected:
            raise OSError('重复解码帧数不一致')


def paste_box(frame, face, template, size):
    _, matrix = warp_face_by_face_landmark_5(frame, face.landmark_set['5/68'], template, size)
    box, _ = calculate_paste_area(frame, np.empty((size[1], size[0], 3), np.uint8), matrix)
    box[:2] = np.maximum(0, box[:2] - 4)
    box[2:] = np.minimum([frame.shape[1], frame.shape[0]], box[2:] + 4)
    return box


def crop_region(image, box):
    x1, y1, x2, y2 = box
    return np.ascontiguousarray(image[y1:y2, x1:x2])


def overlay_region(original, image, box, face):
    output = original.copy()
    if face is not None:
        x1, y1, x2, y2 = box
        if image.shape != (y2 - y1, x2 - x1, 3):
            raise OSError('缓存区域尺寸与原始画面不一致')
        output[y1:y2, x1:x2] = image
    return output


def smooth_points(faces):
    result, previous = [], None
    for face in faces:
        if face is None:
            result.append(None)
            previous = None
            continue
        points = face.landmark_set['68'].copy()
        width = max(1, float(np.linalg.norm(points[16] - points[0])))
        if previous is not None and np.max(np.linalg.norm(points - previous, axis=1)) < width * .25:
            points = points * .7 + previous * .3
        previous = points.copy()
        result.append(points)
    return result


def _progress(items, total, label):
    return tqdm(items, total=total, desc=label, unit='frame', ascii=' =',
                disable=state_manager.get_item('log_level') in ['warn', 'error'])


def process_roi_video(max_tier=None):
    target = state_manager.get_item('target_path')
    root = Path(get_temp_directory_path(target)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    cache = Path(tempfile.mkdtemp(prefix='roi-ffv1-', dir=root))
    stats = CacheStats()
    old_cv_threads = cv2.getNumThreads()
    writer = None
    workers = None
    try:
        cv2.setNumThreads(1)
        check_cancelled()
        # A failed attempt may already have swapped frames. Count only the
        # successful attempt, including when the same task is restarted.
        with swapper._SWAPPED_FRAME_LOCK:
            swapper._SWAPPED_FRAME_COUNT = 0
        video_manager.clear_video_pool()
        sources = read_static_images(filter_image_paths(state_manager.get_item('source_paths')))
        release_pools()
        source_face = swapper.extract_source_face(sources)
        if source_face is None:
            raise ValueError('源图片未检测到人脸')
        release_pools()
        free_mib = available_vram()
        workers = choose_workers(free_mib, max_tier=max_tier)
        logger.info('默认人脸区域FFV1无损流程：30秒分文件，空闲显存%s MiB，%s档；换脸%s / 表情%s / 增强%s / 太阳穴%s' % (
            free_mib, workers['tier'], workers['swap'], workers['restore'], workers['enhance'], workers['temple']), __name__)
        output_size = output_resolution(target)
        size = processing_resolution(target, output_size)
        fps = restrict_video_fps(target, state_manager.get_item('output_video_fps'))
        start, end = restrict_trim_frame(target, state_manager.get_item('trim_frame_start'), state_manager.get_item('trim_frame_end'))
        estimate = predict_video_frame_total(target, fps, start, end)
        swap_options, restore_options = swapper.get_model_options(), restorer.get_model_options()
        boost_size = unpack_resolution(state_manager.get_item('face_swapper_pixel_boost'))
        order = state_manager.get_item('face_selector_order') or 'large-small'
        faces, swap_boxes, restore_boxes = [], [], []
        with ProcessScope() as scope:
            def original_frames(expected=None):
                return OriginalFrames(target, size, fps, start, end, scope, expected)

            def metadata(item):
                check_cancelled()
                _, frame = item
                face = get_one_face(sort_faces_by_order(get_many_faces([frame], geometry_only=True), order))
                if face is None:
                    return None, np.array([0, 0, 1, 1]), np.array([0, 0, 1, 1])
                first = paste_box(frame, face, swap_options['template'], boost_size)
                second = paste_box(frame, face, restore_options['template'], restore_options['size'])
                combined = np.r_[np.minimum(first[:2], second[:2]), np.maximum(first[2:], second[2:])]
                return face, first, combined

            for face, first, combined in _progress(bounded_map(metadata, original_frames(), workers['metadata'], 'metadata'), estimate, 'face metadata'):
                faces.append(face)
                swap_boxes.append(first)
                restore_boxes.append(combined)
            count = len(faces)
            if not any(face is not None for face in faces):
                raise ValueError('目标视频未检测到可替换人脸')
            logger.info('roi_frame_count=%s; missing_faces=%s' % (count, sum(f is None for f in faces)), __name__)
            release_pools()
            swapper.get_inference_pool()
            first_cache = SegmentWriter(cache / 'swap', swap_boxes, fps, scope, stats)

            def swap(item):
                check_cancelled()
                number, original = item
                face = faces[number]
                if face is None:
                    return number, np.zeros((1, 1, 3), np.uint8)
                generated = swapper.swap_face(source_face, face, sources[0], original.copy())
                with swapper._SWAPPED_FRAME_LOCK:
                    swapper._SWAPPED_FRAME_COUNT += 1
                return number, crop_region(generated, swap_boxes[number])

            for number, image in _progress(bounded_map(swap, original_frames(count), workers['swap'], 'swap'), count, 'face swap'):
                first_cache.write(number, image)
            first_cache.close()
            release_pools()
            restorer.get_inference_pool()
            restore_limit = threading.Semaphore(workers['restore'])
            first_reader = SegmentReader(first_cache)
            second_cache = SegmentWriter(cache / 'restore', restore_boxes, fps, scope, stats,
                                         on_commit=first_cache.delete_segment)

            def restoration_inputs():
                for number, original in original_frames(count):
                    yield number, original, overlay_region(original, first_reader.read(number), swap_boxes[number], faces[number])

            def restore(item):
                check_cancelled()
                number, original, generated = item
                token = thread_helper.STAGE_SEMAPHORE.set(restore_limit)
                try:
                    if faces[number] is None:
                        return number, np.zeros((1, 1, 3), np.uint8)
                    image = restorer.restore_expression(faces[number], original, generated)
                    return number, crop_region(image, restore_boxes[number])
                finally:
                    thread_helper.STAGE_SEMAPHORE.reset(token)

            for number, image in _progress(bounded_map(restore, restoration_inputs(), workers['restore'], 'restore'), count, 'expression restore'):
                second_cache.write(number, image)
            first_reader.close()
            second_cache.close()
            release_pools()
            points = smooth_points(faces)
            enhancer.get_inference_pool()
            second_reader = SegmentReader(second_cache)
            enhance_limit = threading.Semaphore(workers['enhance'])
            temple_enabled = state_manager.get_item('temple_correction') == 'on'
            temple_workers = workers['temple'] if temple_enabled else 1
            writer = video_manager.get_writer(target, fps, size, output_size, state_manager.get_item('output_video_fps'))
            scope.attach(writer['process'])

            def final_inputs():
                for number, original in original_frames(count):
                    yield number, original, overlay_region(original, second_reader.read(number), restore_boxes[number], faces[number])

            def enhance(item):
                check_cancelled()
                number, original, generated = item
                token = thread_helper.STAGE_SEMAPHORE.set(enhance_limit)
                try:
                    image = enhancer.enhance_face(faces[number], generated) if faces[number] else generated
                    return number, original, image
                finally:
                    thread_helper.STAGE_SEMAPHORE.reset(token)

            def temple(item):
                check_cancelled()
                number, original, generated = item
                if temple_enabled and faces[number] is not None:
                    generated = correct_temple(original, generated, faces[number], points[number])
                return number, generated

            def write_final(item, progress):
                check_cancelled()
                number, image = item
                video_manager.write_video_frame(writer, image)
                progress.update()
                if (number + 1) % second_cache.frames_per_segment == 0 or number == count - 1:
                    second_cache.delete_segment(number // second_cache.frames_per_segment)

            # Warm common parser/landmarker/enhancer sessions serially, then keep
            # only 2*workers in flight. Segment boundaries do not reload models.
            with _progress(None, count, 'enhance temple encode') as progress:
                items = iter(final_inputs())
                write_final(temple(enhance(next(items))), progress)
                with ThreadPoolExecutor(max_workers=workers['enhance']) as producer, ThreadPoolExecutor(max_workers=temple_workers) as consumer:
                    ep, tp = deque(), deque()
                    try:
                        def consume():
                            tp.append(consumer.submit(temple, ep.popleft().result()))
                            if len(tp) >= temple_workers * 2:
                                write_final(tp.popleft().result(), progress)
                        for item in items:
                            check_cancelled()
                            ep.append(producer.submit(enhance, item))
                            if len(ep) >= workers['enhance'] * 2:
                                consume()
                        while ep:
                            consume()
                        while tp:
                            write_final(tp.popleft().result(), progress)
                    finally:
                        for pending in list(ep) + list(tp):
                            pending.cancel()
            second_reader.close()
            if not video_manager.close_video_writer(writer):
                check_cancelled()
                raise OSError('最终视频编码失败')
            scope.forget(writer['process'])
            video_manager.VIDEO_POOL_SET['writer'].pop(target, None)
            writer = None
            if stats.current_bytes:
                raise OSError('FFV1缓存未完整消费')
        logger.info('roi_cache_peak_bytes=%s; roi_cache_written_bytes=%s; roi_cache_remaining_bytes=0' % (
            stats.peak_bytes, stats.written_bytes), __name__)
        for module in [swapper, restorer, enhancer]:
            module.post_process()
        return 0
    except PipelineCancelled:
        return 4
    except CacheSpaceError:
        raise
    except Exception as error:
        if process_manager.is_stopping() or process_manager.is_pending():
            return 4
        if is_gpu_memory_error(error, state_manager.get_item('execution_providers')):
            raise GpuMemoryError(str(error), workers) from error
        logger.error('人脸区域FFV1处理失败：' + str(error), __name__)
        return 1
    finally:
        video_manager.VIDEO_POOL_SET['writer'].pop(target, None)
        release_pools()
        cv2.setNumThreads(old_cv_threads)
        if not safe_remove(cache, root):
            logger.warn('临时缓存暂未释放，下次启动会重试清理：' + str(cache), __name__)
