from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from facefusion import core, inference_manager, state_manager, face_store
from facefusion.sheet_worker import GPU_EXECUTION_LOCK
from facefusion.uis.components import preview
from facefusion.workflows import staged_video, image_to_video


class PreviewMemoryTests(unittest.TestCase):
    def tearDown(self):
        staged_video.release_pools()

    def test_stages_release_sessions_without_changing_pixels_or_masks(self):
        frame = np.full((32,32,3), 40, np.uint8)
        def render(managed):
            staged_video.release_pools()
            events = []
            def analysis(image):
                inference_manager.INFERENCE_POOL_SET['ui']['analysis'] = object()
                return False
            def module(name, increment):
                def pre(mode):
                    if managed:
                        self.assertFalse(inference_manager.INFERENCE_POOL_SET['ui'])
                    inference_manager.INFERENCE_POOL_SET['ui'][name] = object()
                    events.append(name)
                    return True
                return SimpleNamespace(pre_process=pre,process_frame=lambda values:(values['temp_vision_frame']+increment,values['temp_vision_mask']))
            with ExitStack() as stack:
                stack.enter_context(patch.object(preview,'analyse_frame',side_effect=analysis))
                stack.enter_context(patch.object(preview,'get_processors_modules',return_value=[module('swap',1),module('restore',2),module('enhance',3)]))
                stack.enter_context(patch.object(state_manager,'get_item',side_effect=lambda key:[0,0,0,0] if key=='background_remover_fill_color' else None))
                if not managed:
                    stack.enter_context(patch.object(staged_video,'release_pools'))
                result = preview.process_preview_frame(frame,[],None,None,[frame],'default','640x640')
            return result, events
        before, order_before = render(False)
        after, order_after = render(True)
        self.assertTrue(np.array_equal(before, after))
        self.assertEqual(order_after,order_before)
        self.assertFalse(any(inference_manager.INFERENCE_POOL_SET.values()))

    def test_preserved_face_cache_is_not_recomputed_between_stages(self):
        frame=np.ones((8,8,3),np.uint8);faces=[object()]
        face_store.set_faces(frame,faces)
        inference_manager.INFERENCE_POOL_SET['ui']['model']=object()
        staged_video.release_pools(preserve_faces=True)
        self.assertIs(face_store.get_faces(frame), faces)
        self.assertFalse(any(inference_manager.INFERENCE_POOL_SET.values()))
        staged_video.release_pools()
        self.assertIsNone(face_store.get_faces(frame))

    def test_preview_oom_cleans_and_unlocks_without_changing_settings(self):
        with patch.object(preview.process_manager,'is_pending',return_value=True), patch.object(preview,'_update_preview_image',side_effect=RuntimeError('CUDA out of memory')), patch('gradio.Warning') as warning, patch('facefusion.video_manager.clear_video_pool') as close:
            preview.update_preview_image('default','640x640')
        warning.assert_called_once();close.assert_called_once()
        self.assertFalse(GPU_EXECUTION_LOCK.locked())
        self.assertFalse(any(inference_manager.INFERENCE_POOL_SET.values()))

    def test_busy_preview_does_not_clear_an_active_task(self):
        with patch.object(preview.process_manager,'is_pending',return_value=False), patch.object(staged_video,'release_pools') as release:
            preview.update_preview_image('default','640x640')
        release.assert_not_called()

    def test_initial_preview_oom_keeps_ui_available(self):
        with patch.object(preview,'process_preview_frame',side_effect=RuntimeError('CUDA out of memory')), patch('gradio.Warning') as warning:
            self.assertIsNone(preview._initial_preview_frame())
        warning.assert_called_once()

    def test_task_releases_preview_before_preprocess_and_after_failure(self):
        inference_manager.INFERENCE_POOL_SET['ui']['preview']=object()
        def pre(mode):
            self.assertFalse(any(inference_manager.INFERENCE_POOL_SET.values()))
            inference_manager.INFERENCE_POOL_SET['cli']['partially_loaded']=object()
            raise ValueError('failed preparation')
        values={'command':'run','workflow_mode':'image-to-video','processors':['face_swapper']}
        with patch.object(state_manager,'get_item',side_effect=values.get), patch.object(core,'detect_workflow_mode',return_value='image-to-video'), patch.object(core,'get_processors_modules',return_value=[SimpleNamespace(pre_process=pre)]):
            with self.assertRaises(ValueError):core.conditional_process()
        self.assertFalse(GPU_EXECUTION_LOCK.locked())
        self.assertFalse(any(inference_manager.INFERENCE_POOL_SET.values()))

    def test_analysis_release_precedes_processing_for_compatibility_preset(self):
        seen=[]
        values={'command':'headless-run','target_path':'fake.mp4','workflow_strategy':'memory'}
        def analyse():
            inference_manager.INFERENCE_POOL_SET['cli']['analysis']=object();seen.append('analysis');return 0
        def process():
            self.assertFalse(any(inference_manager.INFERENCE_POOL_SET.values()));seen.append('process');return 0
        with ExitStack() as stack:
            stack.enter_context(patch.object(state_manager,'get_item',side_effect=values.get))
            stack.enter_context(patch.object(image_to_video,'analyse_video',side_effect=analyse))
            stack.enter_context(patch.object(image_to_video,'process_memory_frames',side_effect=process))
            for name in ['clear','setup','restore_audio','finalize_video']:
                stack.enter_context(patch.object(image_to_video,name,return_value=0))
            self.assertEqual(image_to_video.process(0),0)
        self.assertEqual(seen,['analysis','process'])

    def test_low_memory_compatibility_policy_restores_threads_and_values(self):
        from facefusion import presets
        values=dict(presets.BUILTINS['default']['values'],execution_providers=['cuda'],execution_thread_count=4,workflow_strategy='disk',face_selector_mode='one',temp_pixel_format='bgr24',target_path='input.mp4')
        with patch.object(state_manager,'get_item',side_effect=values.get), patch.object(state_manager,'set_item',side_effect=values.__setitem__), patch.object(staged_video,'available_vram_info',return_value=(3300,6144)), patch.object(staged_video.logger,'info'):
            with staged_video.task_memory_policy():
                self.assertFalse(staged_video.SERIAL_STAGES.is_set())
                self.assertEqual(values['execution_thread_count'],4)
            values['face_swapper_weight']=.7
            before=dict(values)
            with self.assertRaises(ValueError):
                with staged_video.task_memory_policy():
                    self.assertTrue(staged_video.SERIAL_STAGES.is_set())
                    self.assertEqual(values['execution_thread_count'],1)
                    raise ValueError('cancelled')
            self.assertEqual(values,before)
            self.assertFalse(staged_video.SERIAL_STAGES.is_set())

    def test_preset_change_cannot_clear_a_busy_preview(self):
        from facefusion import presets
        with patch.object(presets.process_manager,'is_pending',return_value=True), patch.object(staged_video,'release_pools') as release:
            GPU_EXECUTION_LOCK.acquire()
            try:
                with self.assertRaises(ValueError):presets.apply_preset('default')
                release.assert_not_called()
            finally:GPU_EXECUTION_LOCK.release()
