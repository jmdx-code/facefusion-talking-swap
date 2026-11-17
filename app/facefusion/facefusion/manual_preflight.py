"""Correct irregular video timing before manual jobs, without mutating UI inputs."""
from pathlib import Path
import tempfile

from facefusion import media_normalizer, process_manager
from facefusion.filesystem import is_video
from facefusion.userdata_paths import get_userdata_dir


def prepare_manual_video(step_args):
    target = step_args.get('target_path')
    if not is_video(target):
        return step_args, ''
    inspection = media_normalizer.inspect_video(Path(target))
    if not inspection.reasons:
        return step_args, ''
    parent = get_userdata_dir() / 'temp' / 'manual_preflight'
    parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix='timing-', dir=parent))
    from facefusion.runtime_cleanup import mark_workspace, safe_remove, finish_workspace
    mark_workspace(work_dir)
    try:
        normalized, changed, note = media_normalizer.inspect_and_normalize_video(
            Path(target), work_dir, should_abort=process_manager.is_stopping
        )
        if not changed:
            finish_workspace(work_dir)
            safe_remove(work_dir,parent)
            return step_args, ''
        normalized_info = media_normalizer.inspect_video(normalized)
        updated = dict(step_args)
        updated['target_path'] = str(normalized)
        # A UI default FPS should follow normalization; preserve an explicitly
        # different output FPS chosen by the user.
        requested_fps = step_args.get('output_video_fps')
        if requested_fps is None or abs(requested_fps - inspection.avg_fps) < .15:
            updated['output_video_fps'] = normalized_info.avg_fps
        # Preserve the same selected times after the input's frame rate changes.
        if inspection.avg_fps > 0 and normalized_info.avg_fps > 0:
            ratio = normalized_info.avg_fps / inspection.avg_fps
            for key in ('trim_frame_start', 'trim_frame_end', 'reference_frame_number'):
                if isinstance(step_args.get(key), int):
                    updated[key] = round(step_args[key] * ratio)
        output = updated.get('output_path')
        if output and Path(output).suffix and Path(output).suffix.lower() != normalized.suffix.lower():
            updated['output_path'] = str(Path(output).with_suffix(normalized.suffix))
            updated['output_video_encoder'] = 'libx264'
        return updated, note
    except BaseException:
        finish_workspace(work_dir)
        safe_remove(work_dir,parent)
        raise


def cleanup_prepared_video(original, prepared):
    old_target=original.get('target_path')
    target=prepared.get('target_path')
    if not target or target==old_target:
        return
    parent=get_userdata_dir()/'temp/manual_preflight'
    directory=Path(target).parent
    if directory.resolve().parent!=parent.resolve():
        return
    from facefusion.runtime_cleanup import finish_workspace, safe_remove
    finish_workspace(directory)
    safe_remove(directory,parent)
