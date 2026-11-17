import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
from facefusion import runtime_cleanup as cleanup, state_manager, process_manager, video_options, sheet_worker
from facefusion.temp_helper import get_temp_directory_path
from facefusion.workflows import image_to_video
from facefusion.processors.modules.expression_restorer import core as restorer


class VideoResolutionTest(unittest.TestCase):
    def test_aspect_ratio_orientation_and_no_downscale(self):
        for original,expected in [((640,360),(1920,1080)),((360,640),(1080,1920)),((640,480),(1440,1080)),((1080,1920),(1080,1920)),((3840,2160),(3840,2160)),((1920,800),(1920,800))]:
            self.assertEqual(video_options.upscale_resolution(original),expected)
            self.assertEqual(video_options.upscale_resolution(original,False),original)

    def test_preference_defaults_on_and_headless_jobs_obey_switch(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(video_options,'preference_path',return_value=Path(temp)/'settings.json'):
            self.assertTrue(video_options.load_upscale_preference())
            for enabled in [False,True]:
                video_options.save_upscale_preference(enabled)
                self.assertEqual(video_options.load_upscale_preference(),enabled)
                cmd=sheet_worker.build_headless_command(sys.executable,Path('facefusion.py'),Path('s.png'),Path('t.mp4'),Path('o.mp4'),Path('temp'),Path('jobs'),sheet_worker.presets.BUILTINS['default']['values'])
                self.assertIn('--upscale-1080p' if enabled else '--no-upscale-1080p',cmd)
                self.assertNotIn('--no-upscale-1080p' if enabled else '--upscale-1080p',cmd)

    def test_expression_restore_uses_matching_canvas_after_upscale(self):
        original=np.zeros((64,36,3),np.uint8);generated=np.zeros((192,108,3),np.uint8)
        face=object()
        def restore(selected,target,temp):
            self.assertEqual(target.shape,temp.shape)
            return temp
        with patch.object(restorer,'select_faces',return_value=[face]),patch.object(restorer,'scale_face',return_value=face),patch.object(restorer,'restore_expression',side_effect=restore) as mocked:
            restorer.process_frame({'target_vision_frames':[original],'temp_vision_frame':generated})
            mocked.assert_called_once()


class CleanupTest(unittest.TestCase):
    def test_success_failure_cancel_and_exception_always_remove_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            state_manager.init_item('target_path','input.mp4')
            state_manager.init_item('temp_path',temp)
            state_manager.init_item('workflow_strategy','disk')
            directory=Path(get_temp_directory_path('input.mp4'))
            def extract(result):
                (directory/'00000000.png').write_bytes(b'frame')
                if isinstance(result,Exception):raise result
                return result
            for result in [0,1,4,RuntimeError('inference failed')]:
                with patch.object(image_to_video,'supported',return_value=False),patch.object(image_to_video,'analyse_video',return_value=0),patch.object(image_to_video,'extract_frames',side_effect=lambda:extract(result)),patch.object(image_to_video,'process_disk_frames',return_value=0),patch.object(image_to_video,'merge_frames',return_value=0),patch.object(image_to_video,'restore_audio',return_value=0),patch.object(image_to_video,'finalize_video',return_value=0),patch('facefusion.video_manager.clear_video_pool'):
                    if isinstance(result,Exception):
                        with self.assertRaises(RuntimeError):image_to_video.process(0)
                    else:self.assertEqual(image_to_video.process(0),result)
                self.assertFalse(directory.exists())
                self.assertTrue(process_manager.is_pending())

    def test_startup_removes_dead_work_but_keeps_live_work_and_personal_data(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(cleanup,'get_userdata_dir',return_value=Path(temp)),patch.object(cleanup,'pid_alive',side_effect=lambda pid:pid==os.getpid()):
            root=Path(temp)/'temp/facefusion'
            dead=root/'dead';live=root/'live';old=root/'legacy';fresh=root/'fresh'
            for directory in [dead,live,old,fresh]:
                cleanup.mark_workspace(directory)
                (directory/'frame.bmp').write_bytes(b'frames')
            owner=json.loads((dead/cleanup.MARKER).read_text());owner['pid']=999999
            (dead/cleanup.MARKER).write_text(json.dumps(owner))
            (old/cleanup.MARKER).unlink();os.utime(old,(time.time()-90000,)*2)
            (fresh/cleanup.MARKER).unlink()
            for name in ['models/keep.onnx','manual_outputs/keep.mp4','source.png','token.json']:
                file=Path(temp)/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(b'keep')
            self.assertEqual(cleanup.sweep_workspaces(startup=True),2)
            self.assertFalse(dead.exists());self.assertFalse(old.exists())
            self.assertTrue(live.exists());self.assertTrue(fresh.exists())
            for name in ['models/keep.onnx','manual_outputs/keep.mp4','source.png','token.json']:
                self.assertEqual((Path(temp)/name).read_bytes(),b'keep')

    def test_preflight_failure_and_completion_delete_only_derived_video(self):
        from facefusion import manual_preflight
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temp,patch.object(manual_preflight,'get_userdata_dir',return_value=Path(temp)):
            original=Path(temp)/'original.mp4';original.write_bytes(b'user video')
            def normalize(source,work_dir,**kwargs):
                (work_dir/'partial.mp4').write_bytes(b'partial')
                raise RuntimeError('ffmpeg failure')
            with patch.object(manual_preflight,'is_video',return_value=True),patch.object(manual_preflight.media_normalizer,'inspect_video',return_value=SimpleNamespace(reasons=['codec'],avg_fps=30)),patch.object(manual_preflight.media_normalizer,'inspect_and_normalize_video',side_effect=normalize):
                with self.assertRaises(RuntimeError):manual_preflight.prepare_manual_video({'target_path':str(original)})
            parent=Path(temp)/'temp/manual_preflight'
            self.assertEqual(list(parent.iterdir()),[])
            derived=parent/'finished';cleanup.mark_workspace(derived);(derived/'normalized.mp4').write_bytes(b'generated')
            manual_preflight.cleanup_prepared_video({'target_path':str(original)},{'target_path':str(derived/'normalized.mp4')})
            self.assertFalse(derived.exists());self.assertEqual(original.read_bytes(),b'user video')

    def test_delete_rejects_outside_root_and_root_itself(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'owned';root.mkdir()
            outside=Path(temp)/'original';outside.mkdir();(outside/'source.mp4').write_bytes(b'keep')
            self.assertFalse(cleanup.safe_remove(root,root))
            self.assertFalse(cleanup.safe_remove(outside,root))
            self.assertTrue((outside/'source.mp4').exists())

    def test_windows_forced_exit_is_reclaimed_at_next_sweep(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(cleanup,'get_userdata_dir',return_value=Path(temp)):
            directory=Path(temp)/'temp/facefusion/killed'
            package=Path(__file__).resolve().parents[1]
            code="import sys,time;sys.path.insert(0,sys.argv[1]);from facefusion.runtime_cleanup import mark_workspace;mark_workspace(sys.argv[2]);print('READY',flush=True);time.sleep(30)"
            child=subprocess.Popen([sys.executable,'-B','-c',code,str(package),str(directory)],stdout=subprocess.PIPE)
            try:
                self.assertEqual(child.stdout.readline().strip(),b'READY')
                self.assertTrue(cleanup.pid_alive(child.pid))
                self.assertEqual(cleanup.sweep_workspaces(startup=True),0)
                child.terminate();child.wait(timeout=5)
                self.assertFalse(cleanup.pid_alive(child.pid))
                self.assertEqual(cleanup.sweep_workspaces(startup=True),1)
                self.assertFalse(directory.exists())
            finally:
                if child.poll() is None:child.kill();child.wait()
                child.stdout.close()


if __name__=='__main__':unittest.main()
