import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from facefusion import presets, process_manager, state_manager


class PresetTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(presets, 'PRESET_PATH', Path(self.directory.name) / 'presets.json')
        self.path_patch.start()
        for key, value in presets.DEFAULTS.items():
            state_manager.init_item(key, copy.deepcopy(value))
        state_manager.init_item('source_paths', ['source.jpg'])
        state_manager.init_item('target_path', 'target.mp4')
        state_manager.init_item('output_path', 'result.mp4')
        state_manager.init_item('execution_providers', ['cuda'])
        process_manager.end()
        self.module = Mock()
        self.module.pre_check.return_value = True
        self.modules_patch = patch.object(presets, 'get_processors_modules', return_value=[self.module])
        self.modules_patch.start()

    def tearDown(self):
        self.modules_patch.stop()
        self.path_patch.stop()
        self.directory.cleanup()
        process_manager.end()

    def test_transition_restores_mouth_mask_and_processing_order(self):
        presets.apply_preset('clear')
        normal = presets.snapshot()
        presets.apply_preset('mouth_restore')
        self.assertEqual(state_manager.get_item('processors'), ['face_swapper', 'expression_restorer', 'face_enhancer'])
        self.assertEqual(state_manager.get_item('expression_restorer_areas'), ['lower-face'])
        self.assertEqual(state_manager.get_item('expression_restorer_factor'), 100)
        self.assertEqual(state_manager.get_item('face_enhancer_blend'), 30)
        self.assertEqual(state_manager.get_item('face_mask_types'), ['box'])
        self.assertIn('mouth', state_manager.get_item('face_mask_regions'))
        presets.apply_preset('eye_restore')
        expected = copy.deepcopy(normal)
        expected['expression_restorer_areas'] = ['lower-face']
        self.assertEqual(presets.snapshot(), expected)
        presets.apply_preset('clear')
        self.assertEqual(presets.snapshot(), normal)
        for key, value in [('source_paths', ['source.jpg']), ('target_path', 'target.mp4'), ('output_path', 'result.mp4'), ('execution_providers', ['cuda'])]:
            self.assertEqual(state_manager.get_item(key), value)

    def test_all_builtins_validate(self):
        self.assertEqual([p['label'] for p in presets.BUILTINS.values()], ['没问题选这个', '解决嘴巴张不开', '解决眼睛斜'])
        for key in presets.BUILTINS:
            with self.subTest(key=key):
                presets.apply_preset(key)
                presets.validate_values(presets.snapshot())

    def test_failed_model_check_rolls_back_every_setting(self):
        before = presets.snapshot()
        self.module.pre_check.return_value = False
        with self.assertRaises(ValueError):
            presets.apply_preset('mouth_restore')
        self.assertEqual(presets.snapshot(), before)

    def test_custom_roundtrip_and_overwrite_with_nondefault_pixel_boost(self):
        state_manager.set_item('face_swapper_pixel_boost', '256x256')
        state_manager.set_item('face_mask_padding', (1, 2, 3, 4))
        key = presets.save_current('我的口播')
        presets.apply_preset('clear')
        presets.apply_preset(key)
        self.assertEqual(state_manager.get_item('face_swapper_pixel_boost'), '256x256')
        self.assertEqual(state_manager.get_item('face_mask_padding'), [1, 2, 3, 4])
        state_manager.set_item('face_enhancer_blend', 25)
        presets.save_current('我的口播')
        self.assertEqual(len(presets.read_custom()), 1)
        self.assertEqual(presets.read_custom()['我的口播']['face_enhancer_blend'], 25)

    def test_invalid_store_is_preserved(self):
        presets.PRESET_PATH.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            presets.save_current('test')
        self.assertEqual(presets.PRESET_PATH.read_text(), '{broken')

    def test_missing_name_does_not_write_a_store(self):
        for name in [None, '', '   ']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                presets.save_current(name)
        self.assertFalse(presets.PRESET_PATH.exists())

    def test_processing_blocks_switch_and_save(self):
        before = presets.snapshot()
        process_manager.start()
        for action in [lambda: presets.apply_preset('clear'), lambda: presets.save_current('test')]:
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(presets.snapshot(), before)
        self.assertFalse(presets.PRESET_PATH.exists())

    def test_invalid_custom_values_are_rejected_before_mutation(self):
        before = presets.snapshot()
        values = copy.deepcopy(presets.DEFAULTS)
        values['face_swapper_pixel_boost'] = '255x255'
        presets.PRESET_PATH.write_text(json.dumps({'version':1, 'presets':{'bad':values}}))
        with self.assertRaises(ValueError):
            presets.apply_preset('custom:bad')
        self.assertEqual(presets.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
