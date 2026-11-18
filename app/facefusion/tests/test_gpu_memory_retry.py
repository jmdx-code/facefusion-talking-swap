from collections import deque
from contextlib import ExitStack
import gc
import io
from pathlib import Path
import tempfile
import unittest
import weakref
from unittest.mock import Mock, patch, call

from facefusion import gpu_memory, inference_manager, process_manager, sheet_worker, state_manager
from facefusion.workflows import image_to_video, staged_video


class GpuMemoryRetryTests(unittest.TestCase):
    def setUp(self):
        process_manager.start()

    def tearDown(self):
        process_manager.end()

    def test_recognizes_cuda_ort_bfc_and_caused_allocation_failures(self):
        for message in ['CUDA failure 2: out of memory','CUDA_ERROR_OUT_OF_MEMORY','cudaErrorMemoryAllocation','CUDNN_STATUS_ALLOC_FAILED','CUBLAS_STATUS_ALLOC_FAILED','bfc_arena.cc BFCArena::AllocateRawInternal Failed to allocate memory for requested buffer of size 1024']:
            with self.subTest(message=message):
                self.assertTrue(gpu_memory.is_gpu_memory_error(RuntimeError(message),['cuda']))
        wrapped=RuntimeError('inference failed')
        wrapped.__cause__=RuntimeError('CUDA failure 2: out of memory')
        self.assertTrue(gpu_memory.is_gpu_memory_error(wrapped))

    def test_does_not_misclassify_ram_disk_encoder_or_model_failure(self):
        for error in [MemoryError('out of memory'),RuntimeError('CPUAllocator out of memory'),RuntimeError('host memory out of memory'),OSError('No space left on device'),OSError('FFmpeg out of memory'),BrokenPipeError('stream_encoder_failed'),ValueError('invalid model'),RuntimeError('CUDA illegal memory access')]:
            with self.subTest(error=error):
                self.assertFalse(gpu_memory.is_gpu_memory_error(error,['cuda']))
        self.assertFalse(gpu_memory.is_gpu_memory_error(RuntimeError('BFCArena Failed to allocate memory'),['cpu']))

    def test_releases_traceback_locals_before_retry(self):
        refs=[]
        class Session:
            pass
        def fail():
            session=Session();refs.append(weakref.ref(session))
            raise RuntimeError('CUDA out of memory')
        try:
            fail()
        except RuntimeError as error:
            self.assertIsNotNone(refs[0]())
            gpu_memory.forget_exception_frames(error)
            gc.collect()
            self.assertIsNone(refs[0]())

    def test_every_retry_has_a_strictly_lower_ceiling_and_stops_at_lowest(self):
        with self._patch_dispatcher() as mocks:
            attempts=[]
            def fail(max_tier=None):
                workers=staged_video.choose_workers(20000,max_tier)
                attempts.append(workers['tier'])
                raise gpu_memory.GpuMemoryError('CUDA out of memory',workers)
            mocks['render'].side_effect=fail
            self.assertEqual(image_to_video.process_staged_or_fallback(),1)
            self.assertEqual(attempts,['高+','高','中','低'])
            self.assertEqual(mocks['clear'].call_count,3)
            mocks['fallback'].assert_not_called()

    def test_retry_keeps_ceiling_even_if_free_memory_increases_and_next_job_resets(self):
        with self._patch_dispatcher() as mocks:
            mocks['render'].side_effect=[gpu_memory.GpuMemoryError('CUDA out of memory',staged_video.choose_workers(6000)),0,0]
            self.assertEqual(image_to_video.process_staged_or_fallback(),0)
            self.assertEqual(image_to_video.process_staged_or_fallback(),0)
            self.assertEqual(mocks['render'].call_args_list,[call(),call(max_tier=1),call()])
            self.assertEqual(staged_video.choose_workers(20000,max_tier=1)['tier'],'中')
            self.assertEqual(staged_video.choose_workers(2000,max_tier=1)['tier'],'低')

    def test_model_load_failure_before_worker_selection_uses_available_memory(self):
        with self._patch_dispatcher() as mocks:
            mocks['render'].side_effect=[gpu_memory.GpuMemoryError('CUDA out of memory'),0]
            with patch.object(staged_video,'available_vram_info',return_value=(6000,8188)):
                self.assertEqual(image_to_video.process_staged_or_fallback(),0)
            self.assertEqual(mocks['render'].call_args_list,[call(),call(max_tier=1)])

    def test_cancel_takes_precedence_over_oom_retry(self):
        with self._patch_dispatcher() as mocks:
            def stop_then_fail():
                process_manager.stop()
                raise gpu_memory.GpuMemoryError('CUDA out of memory',staged_video.choose_workers(6000))
            mocks['render'].side_effect=stop_then_fail
            self.assertEqual(image_to_video.process_staged_or_fallback(),4)
            mocks['render'].assert_called_once();mocks['clear'].assert_not_called()

    def test_non_oom_result_does_not_retry(self):
        with self._patch_dispatcher() as mocks:
            mocks['render'].return_value=1
            self.assertEqual(image_to_video.process_staged_or_fallback(),1)
            mocks['render'].assert_called_once();mocks['clear'].assert_not_called()

    def test_loading_oom_propagates_without_fatal_process_exit(self):
        with patch.object(inference_manager,'InferenceSession',side_effect=RuntimeError('CUDA out of memory')),patch.object(inference_manager,'fatal_exit') as fatal:
            with self.assertRaisesRegex(RuntimeError,'CUDA out of memory'):
                inference_manager.create_inference_session('test.onnx',[('CUDAExecutionProvider',{})])
            fatal.assert_not_called()

    def test_worker_marks_oom_as_processing_not_content_block(self):
        task=sheet_worker.LocalTask('test',2,'https://example.com/s.png','https://example.com/t.mp4','','手动领取')
        for marker in ['gpu_memory_retry','gpu_memory_exhausted']:
            state={}
            sheet_worker.SheetWorkerManager._parse_headless_line(task,marker,deque(),state)
            self.assertTrue(state['saw_processing'])
            self.assertFalse(state.get('saw_merging',False))

    def test_terminal_gpu_oom_does_not_trigger_cpu_encoding_retry(self):
        from facefusion import presets
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            manager=sheet_worker.SheetWorkerManager(config_path=root/'config.json',token_path=root/'token.json')
            manager.log=Mock();manager.bump_ui_version=Mock()
            task=sheet_worker.LocalTask('test',2,'https://example.com/s.png','https://example.com/t.mp4','','手动领取')
            class Child:
                def __init__(self,*args,**kwargs):
                    self.stdout=io.BytesIO(b'stream_encoder_failed: stale encoder signal\ngpu_memory_exhausted: CUDA out of memory\n')
                def __enter__(self):return self
                def __exit__(self,*args):self.stdout.close()
                def wait(self,timeout=None):return 1
            with patch.object(sheet_worker.subprocess,'Popen',side_effect=Child) as run,patch.object(sheet_worker,'terminate_process_tree'),patch.object(sheet_worker,'get_available_execution_providers',return_value=['cuda']):
                with self.assertRaisesRegex(ValueError,'最低并发档仍显存不足'):
                    manager._execute_headless_swap(task,root/'s.png',root/'t.mp4',root/'out.mp4',root,root/'jobs',presets.BUILTINS['default']['values'])
                run.assert_called_once()

    def _patch_dispatcher(self):
        class DispatchPatches(ExitStack):
            def __enter__(inner):
                super().__enter__()
                result={}
                for key,name in [('render','process_staged_frames'),('clear','clear'),('setup','setup'),('fallback','process_stream_fallback')]:
                    result[key]=inner.enter_context(patch.object(image_to_video,name,return_value=0))
                inner.enter_context(patch('facefusion.video_manager.clear_video_pool'))
                inner.enter_context(patch.object(staged_video,'release_pools'))
                return result
        return DispatchPatches()
