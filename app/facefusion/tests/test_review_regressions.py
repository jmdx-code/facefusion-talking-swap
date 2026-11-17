from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

import numpy as np
from facefusion import state_manager, model_bootstrap
from facefusion.workflows import core, image_to_video, staged_video
from facefusion.uis.components import preview
from facefusion import temple_correction


class ReviewRegressions(unittest.TestCase):
    def test_normal_pipeline_executes_temple_branch_with_multiple_faces(self):
        frame=np.zeros((64,64,3),np.uint8)
        with ExitStack() as stack:
            for name,value in [('conditional_get_reference_vision_frame',frame),('read_static_images',[]),('conditional_get_source_audio_frame',None),('conditional_get_source_voice_frame',None),('get_processors_modules',[])]:
                stack.enter_context(patch.object(core,name,return_value=value))
            stack.enter_context(patch.object(state_manager,'get_item',side_effect=lambda key:'on' if key=='temple_correction' else None))
            stack.enter_context(patch('facefusion.face_selector.select_faces',return_value=[object(),object()]))
            scale=stack.enter_context(patch('facefusion.face_creator.scale_face',side_effect=lambda f,a,b:f))
            repair=stack.enter_context(patch.object(temple_correction,'correct_temple',side_effect=lambda a,b,f:b))
            self.assertEqual(core.process_temp_frame([frame],frame,0).shape,frame.shape)
            self.assertEqual(repair.call_count,2);self.assertEqual(scale.call_count,2)

    def test_target_only_preview_runs_real_rgba_temple_adapter(self):
        from facefusion import face_landmarker
        frame=np.full((64,64,4),127,np.uint8);frame[:,:,3]=200
        face=SimpleNamespace(landmark_set={'68':np.zeros((68,2))},bounding_box=np.array([0,0,64,64]),angle=0)
        with patch.object(preview,'analyse_frame',return_value=False),patch.object(preview,'get_processors_modules',return_value=[]),patch.object(preview,'select_faces',return_value=[face]),patch('facefusion.face_creator.scale_face',return_value=face),patch.object(face_landmarker,'detect_face_landmark',return_value=(np.zeros((68,2)),0)) as detector,patch.object(state_manager,'get_item',side_effect=lambda key:'on' if key=='temple_correction' else [0,0,0,0] if key=='background_remover_fill_color' else None):
            output=preview.process_preview_frame(frame,[],None,None,[frame],'default','640x640')
            self.assertEqual(output.shape,frame.shape)
            self.assertTrue(np.array_equal(output[:,:,3],frame[:,:,3]))
            self.assertEqual(detector.call_args.args[0].shape[2],3)

    def test_low_disk_routes_before_extraction_and_limits_concurrency(self):
        state_manager.init_item('workflow_strategy','disk');state_manager.init_item('execution_thread_count',8)
        with ExitStack() as stack:
            for name in ['analyse_video','clear','setup','restore_audio','finalize_video']:
                stack.enter_context(patch.object(image_to_video,name,return_value=0))
            stack.enter_context(patch.object(image_to_video,'supported',return_value=False))
            stack.enter_context(patch.object(image_to_video,'has_disk_budget',return_value=False))
            extract=stack.enter_context(patch.object(image_to_video,'extract_frames'))
            def stream():
                self.assertEqual(state_manager.get_item('execution_thread_count'),1)
                return 0
            renderer=stack.enter_context(patch.object(image_to_video,'process_memory_frames',side_effect=stream))
            stack.enter_context(patch('facefusion.video_manager.clear_video_pool'))
            self.assertEqual(image_to_video.process(0),0)
            renderer.assert_called_once();extract.assert_not_called()
            self.assertEqual(state_manager.get_item('execution_thread_count'),8)

    def test_disk_estimate_rejects_three_minute_bmp_on_five_gib(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            values={'target_path':'fake.mp4','temp_path':temp,'output_video_scale':1.0,'upscale_1080p':True,'output_video_fps':30}
            stack.enter_context(patch.object(state_manager,'get_item',side_effect=lambda key:values.get(key)))
            stack.enter_context(patch('facefusion.filesystem.is_video',return_value=True))
            stack.enter_context(patch('facefusion.vision.detect_video_resolution',return_value=(1920,1080)))
            stack.enter_context(patch('facefusion.vision.restrict_trim_frame',return_value=(0,5400)))
            stack.enter_context(patch('facefusion.vision.predict_video_frame_total',return_value=5400))
            stack.enter_context(patch.object(staged_video,'restrict_video_fps',return_value=30))
            disk=stack.enter_context(patch.object(staged_video.shutil,'disk_usage',return_value=SimpleNamespace(free=5*1024**3)))
            self.assertFalse(staged_video.has_disk_budget())
            disk.return_value=SimpleNamespace(free=100*1024**3)
            self.assertTrue(staged_video.has_disk_budget())

    def test_changed_model_invalidates_integrity_cache(self):
        with tempfile.TemporaryDirectory() as temp,patch('facefusion.userdata_paths.get_userdata_dir',return_value=Path(temp)):
            path=Path(temp)/'sample.onnx';path.write_bytes(b'weights');path.with_suffix('.hash').write_text('00000000')
            with patch.object(model_bootstrap,'validate_hash',return_value=True) as crc,patch.object(model_bootstrap,'validate_model_sha256',return_value=True) as sha:
                self.assertTrue(model_bootstrap.validate_cached_model(path))
                self.assertTrue(model_bootstrap.validate_cached_model(path))
                self.assertEqual(crc.call_count,1);self.assertEqual(sha.call_count,1)
                path.write_bytes(b'changed weights')
                self.assertTrue(model_bootstrap.validate_cached_model(path))
                self.assertEqual(crc.call_count,2)
                path.with_suffix('.hash').write_text('11111111')
                self.assertTrue(model_bootstrap.validate_cached_model(path))
                self.assertEqual(crc.call_count,3)
                self.assertTrue(model_bootstrap.validate_cached_model(path,force=True))
                self.assertEqual(crc.call_count,4)


if __name__=='__main__':unittest.main()
