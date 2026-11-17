"""Video canvas size, shared by manual processing and automatic sheet jobs."""
import json
import os
import tempfile
from facefusion import state_manager
from facefusion.userdata_paths import get_userdata_dir


def preference_path():
    return get_userdata_dir() / 'video-options.json'


def load_upscale_preference():
    try:
        value=json.loads(preference_path().read_text(encoding='utf-8')).get('upscale_1080p', True)
        return value if isinstance(value, bool) else True
    except (OSError, ValueError, AttributeError):
        return True


def save_upscale_preference(enabled):
    enabled=bool(enabled)
    path=preference_path();path.parent.mkdir(parents=True,exist_ok=True)
    handle, temporary=tempfile.mkstemp(prefix='.video-options-',dir=path.parent)
    try:
        with os.fdopen(handle,'w',encoding='utf-8') as stream:
            json.dump({'upscale_1080p':enabled},stream)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    state_manager.set_item('upscale_1080p',enabled)


def upscale_resolution(resolution, enabled=True):
    """Fit inside 1920x1080 (or portrait), only enlarge, preserve aspect ratio."""
    width,height=resolution
    if not enabled or min(width,height)<=0:
        return resolution
    factor=min(1920/max(width,height),1080/min(width,height))
    if factor<=1:
        return resolution
    return (round(width*factor/2)*2,round(height*factor/2)*2)


def output_resolution(path):
    from facefusion.vision import detect_video_resolution,scale_resolution
    base=upscale_resolution(detect_video_resolution(path),state_manager.get_item('upscale_1080p') is not False)
    return scale_resolution(base,state_manager.get_item('output_video_scale'))


def processing_resolution(path, output):
    from facefusion.vision import detect_video_resolution
    base=upscale_resolution(detect_video_resolution(path),state_manager.get_item('upscale_1080p') is not False)
    return output if output[0]<=base[0] and output[1]<=base[1] else base
