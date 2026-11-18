import json
import unittest
from unittest.mock import patch

from facefusion import gpu_status
from facefusion.gpu_status import GpuStatus
from facefusion.uis.components.gpu_status import format_status
from facefusion.workflows import staged_video


class FakeCollector:
    def __init__(self):
        self.amount = 2000
        self.hook = None

    def read(self, tasks):
        if self.hook:
            self.hook()
        return {0: dict(name='NVIDIA <test>', total_mib=6144, used_mib=4100, free_mib=2044)}, {key: self.amount for key in tasks}

    def close(self):
        pass


class TestGpuStatus(unittest.TestCase):
    def setUp(self):
        self.collector = FakeCollector()
        self.status = GpuStatus(lambda: self.collector, autostart=False)

    def tearDown(self):
        self.status.close()

    def test_peak_survives_completion_and_encoder_retry(self):
        first = self.status.begin('sheet', 'job1', 100)
        self.status.sample_once()
        self.collector.amount = 1200
        self.status.sample_once()
        self.status.finish('sheet', first)
        self.assertEqual(self.status.snapshot('sheet', False)['peak_mib'], 2000)
        second = self.status.begin('sheet', 'job1', 101)
        self.status.finish('sheet', first)
        record = self.status.snapshot('sheet', False)
        self.assertTrue(record['active'])
        self.assertEqual(record['peak_mib'], 2000)
        self.status.finish('sheet', second)
        self.assertIsNone(self.status.snapshot('sheet', False)['current_mib'])
        self.status.begin('sheet', 'job2', 102)
        self.assertIsNone(self.status.snapshot('sheet', False)['peak_mib'])

    def test_late_samples_cannot_overwrite_new_or_finished_job(self):
        token = self.status.begin('manual', 'old', 100)
        self.collector.hook = lambda: self.status.finish('manual', token)
        self.status.sample_once()
        self.assertIsNone(self.status.snapshot('manual', False)['peak_mib'])
        self.collector.hook = lambda: self.status.begin('manual', 'new', 101)
        self.status.begin('manual', 'old', 100)
        self.status.sample_once()
        self.assertIsNone(self.status.snapshot('manual', False)['peak_mib'])

    def test_sources_are_independent_and_payload_validated(self):
        self.status.begin('manual', 'local', 100)
        self.status.begin('sheet', 'remote', 101)
        workers = dict(tier='中', swap=4, restore=2, enhance=2, temple=4)
        line = gpu_status.TIER_MARKER + json.dumps(dict(workers=workers, free_mib=3300, total_mib=6144))
        with patch.object(gpu_status, 'GPU_STATUS', self.status):
            gpu_status.consume_tier('wrong-job', line)
            self.assertIsNone(self.status.snapshot('sheet', False)['tier'])
            gpu_status.consume_tier('remote', line)
            self.assertEqual(self.status.snapshot('sheet', False)['tier'], '中')
            self.assertIsNone(self.status.snapshot('manual', False)['tier'])
            for bad in ['{}', '{', '{"workers":{"tier":"高","swap":"<script>"}}', '{"workers":{"tier":"高"},"free_mib":NaN}']:
                self.assertTrue(gpu_status.consume_tier('remote', gpu_status.TIER_MARKER + bad))
            self.assertEqual(self.status.snapshot('sheet', False)['tier'], '中')

    def test_format_escapes_and_never_substitutes_whole_device(self):
        self.status.begin('manual', '<script>evil</script>', 100)
        self.collector.amount = None
        self.status.sample_once()
        page = format_status(self.status.snapshot('manual', False))
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('3200', page)
        self.assertIn('任务进程显存<br><strong>暂不可读取', page)
        self.assertIn('整卡已用 / 总显存', page)
        self.assertIn('不会把整卡占用当作任务占用', page)

    def test_preparation_clears_previous_job_and_has_no_fake_usage(self):
        self.status.begin('sheet', 'downloading', None)
        self.status.sample_once()
        page = format_status(self.status.snapshot('sheet', False))
        self.assertIn('GPU阶段未开始', page)
        self.status.finish_task('sheet', 'different')
        self.assertTrue(self.status.snapshot('sheet', False)['active'])
        self.status.finish_task('sheet', 'downloading')
        self.assertFalse(self.status.snapshot('sheet', False)['active'])

    def test_gradio_panels_share_timer_and_rebuild_cleanly(self):
        import gradio
        from facefusion.uis.components import gpu_status as panel
        with patch.object(panel, 'GPU_STATUS', self.status):
            for _ in range(2):
                with gradio.Blocks() as ui:
                    panel.render('manual')
                    panel.render('sheet')
                    panel.listen()
                config = ui.get_config_file()
                self.assertEqual(sum(c['type'] == 'timer' for c in config['components']), 1)
                self.assertEqual(len(config['dependencies']), 1)
                self.assertEqual(len(config['dependencies'][0]['outputs']), 2)


class TestSixGbPolicy(unittest.TestCase):
    def test_six_gb_boundaries_and_oom_cap(self):
        for total in (6016, 6144):
            for free, tier in ((3199, '低'), (3200, '中'), (3599, '中'), (5000, '中'), (6000, '中')):
                self.assertEqual(staged_video.choose_workers(free, total_mib=total)['tier'], tier)
            self.assertEqual(staged_video.choose_workers(5000, max_tier=0, total_mib=total)['tier'], '低')

    def test_other_hardware_thresholds_unchanged(self):
        for total in (0, 4096, 8188, 12288):
            self.assertEqual(staged_video.choose_workers(3300, total_mib=total)['tier'], '低')
            self.assertEqual(staged_video.choose_workers(3600, total_mib=total)['tier'], '中')
        self.assertEqual(staged_video.choose_workers(5000, total_mib=8188)['tier'], '高')
        self.assertEqual(staged_video.choose_workers(8500, total_mib=12288)['tier'], '高+')

    def test_selected_devices_and_bad_query_fall_back(self):
        def state(key):
            return {'execution_providers': ['cuda'], 'execution_device_ids': ['1']}[key]
        with patch.object(staged_video.state_manager, 'get_item', side_effect=state), patch.object(staged_video.subprocess, 'check_output', return_value=b'6000, 8188\n3300, 6144\n'):
            self.assertEqual(staged_video.available_vram_info(), (3300, 6144))
            self.assertEqual(staged_video.available_vram(), 3300)
        with patch.object(staged_video.state_manager, 'get_item', side_effect=state), patch.object(staged_video.subprocess, 'check_output', return_value=b'N/A'):
            self.assertEqual(staged_video.available_vram_info(), (0, 0))


if __name__ == '__main__':
    unittest.main()
