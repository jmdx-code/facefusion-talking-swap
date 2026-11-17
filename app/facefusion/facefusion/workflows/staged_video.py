"""Bounded staged portrait pipeline shared by manual jobs and headless sheet jobs.

Only geometry metadata stays in RAM. Lossless intermediates live in the job temp
folder; stages release ORT pools, and enhancement overlaps local temple repair.
"""
from facefusion.video_options import output_resolution as resolve_output_resolution, processing_resolution
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import gc
from pathlib import Path
import shutil
import subprocess
from facefusion import face_store, inference_manager, logger, process_manager, state_manager
from facefusion.vision import restrict_video_fps


from facefusion.roi_cache import PipelineCancelled as Cancelled


def check_running():
    if process_manager.is_stopping() or process_manager.is_pending():
        raise Cancelled()


def supported():
    s = state_manager.get_item
    return (s('workflow_strategy') == 'disk'
        and s('processors') == ['face_swapper', 'expression_restorer', 'face_enhancer']
        and s('face_swapper_model') in ['hyperswap_1a_256', 'inswapper_128_fp16']
        and s('face_swapper_weight') == .5 and s('face_selector_mode') == 'one'
        and not s('face_tracker_score')
        and not any(s(key) for key in ['face_selector_gender', 'face_selector_race', 'face_selector_age_start', 'face_selector_age_end'])
        and s('expression_restorer_model') == 'live_portrait'
        and s('face_enhancer_model') == 'gfpgan_1.4'
        and s('temp_pixel_format') == 'bgr24')


def choose_workers(free_mib):
    # Modest 25% increase for cards with ample free VRAM (typically 12 GiB+).
    # Above the total VRAM of an 8 GiB card, so its measured tiers stay intact.
    # This tier still needs 12 GiB hardware benchmarking; metadata stays at 8
    # to avoid increasing CPU/decode contention alongside GPU concurrency.
    if free_mib >= 8500:
        return dict(metadata=8, swap=10, restore=5, enhance=5, temple=10, tier='高+')
    # Cold measured baseline 2475 MiB, peak 6873 MiB: ~4.4 GiB incremental,
    # plus headroom. Budget the job's increment, not the whole system's peak.
    if free_mib >= 5000:
        return dict(metadata=8, swap=8, restore=4, enhance=4, temple=8, tier='高')
    if free_mib >= 3600:
        return dict(metadata=4, swap=4, restore=2, enhance=2, temple=4, tier='中')
    return dict(metadata=2, swap=1, restore=1, enhance=1, temple=2, tier='低')


def available_vram():
    if state_manager.get_item('execution_providers') != ['cuda']:
        return 0
    try:
        rows = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits'], timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).decode().splitlines()
        ids = state_manager.get_item('execution_device_ids') or [0]
        return min(int(rows[int(device)].strip()) for device in ids)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0


def release_pools():
    with inference_manager.POOL_LOCK:
        for pools in inference_manager.INFERENCE_POOL_SET.values():
            pools.clear()
    face_store.clear_faces()
    gc.collect()


def bounded_map(function, items, workers, label):
    """Warm the first frame serially, then keep at most 2*workers in flight."""
    items = iter(items)
    first = next(items, None)
    if first is None:
        return
    check_running()
    yield function(first)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = deque()
        try:
            for item in items:
                check_running()
                pending.append(pool.submit(function, item))
                if len(pending) >= workers * 2:
                    yield pending.popleft().result()
            while pending:
                check_running()
                yield pending.popleft().result()
        finally:
            for future in pending:
                future.cancel()


def has_disk_budget(include_stage=False, include_source=True):
    """Conservative full-frame estimate for advanced compatibility workflows only."""
    from facefusion.filesystem import is_video
    from facefusion.vision import predict_video_frame_total, restrict_trim_frame
    target=state_manager.get_item('target_path')
    if not is_video(target):return True
    resolution=processing_resolution(target,resolve_output_resolution(target))
    start,end=restrict_trim_frame(target,state_manager.get_item('trim_frame_start'),state_manager.get_item('trim_frame_end'))
    fps=restrict_video_fps(target,state_manager.get_item('output_video_fps'))
    count=predict_video_frame_total(target,fps,start,end)
    raw=resolution[0]*resolution[1]*3
    required=raw*count*(int(include_source)+int(include_stage))+raw*32+512*1024**2
    root=Path(state_manager.get_item('temp_path'));root.mkdir(parents=True,exist_ok=True)
    free=shutil.disk_usage(root).free
    if free<required:
        logger.warn('兼容流程临时帧空间预计上限 %.1f GiB，当前空闲 %.1f GiB；改用低并发流式处理，不保存整段帧，保留原画质参数。' % (required/1024**3,free/1024**3),__name__)
        return False
    return True


def process_staged_frames():
    from facefusion.workflows.roi_video import process_roi_video
    return process_roi_video()
