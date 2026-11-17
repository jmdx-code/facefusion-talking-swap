from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from facefusion.processors.modules.face_enhancer import core


class EnhancerRegionsTest(unittest.TestCase):
    def render(self, types):
        frame = np.full((64, 64, 3), 40, dtype=np.uint8)
        protected = np.ones((64, 64), dtype=np.float32)
        protected[30:45, 24:40] = 0
        settings = dict(face_mask_types=types, face_mask_regions=['skin', 'upper-lip', 'lower-lip'],
                        face_mask_blur=.3, face_enhancer_weight=.5, face_enhancer_blend=65)
        def composite(original, enhanced, mask, matrix):
            return (original * (1-mask[..., None]) + enhanced * mask[..., None]).astype(np.uint8)
        with ExitStack() as stack:
            def mock(name, **kwargs):
                return stack.enter_context(patch.object(core, name, **kwargs))
            mock('get_model_options', return_value={'template': 'ffhq_512', 'size': (64,64)})
            mock('warp_face_by_face_landmark_5', return_value=(frame.copy(), np.eye(2,3)))
            mock('create_box_mask', return_value=np.ones((64,64), dtype=np.float32))
            region = mock('create_region_mask', return_value=protected)
            mock('prepare_crop_frame', side_effect=lambda x:x)
            mock('forward', return_value=np.full_like(frame, 200))
            mock('normalize_crop_frame', side_effect=lambda x:x)
            mock('paste_back', side_effect=composite)
            stack.enter_context(patch.object(core.state_manager, 'get_item', side_effect=settings.get))
            result = core.enhance_face(SimpleNamespace(landmark_set={'5/68':None}), frame)
            return frame, result, region.call_count, region.call_args

    def test_protected_mouth_unchanged_while_surrounding_face_is_enhanced(self):
        original, result, count, args = self.render(['box','region'])
        np.testing.assert_array_equal(result[30:45,24:40], original[30:45,24:40])
        self.assertTrue(np.all(result[5:20,5:20] > original[5:20,5:20]))
        self.assertEqual(count, 1)
        self.assertEqual(args.args[1], ['skin','upper-lip','lower-lip'])

    def test_box_only_keeps_existing_full_face_enhancement(self):
        original, result, count, _ = self.render(['box'])
        self.assertTrue(np.all(result > original))
        self.assertEqual(count, 0)


if __name__ == '__main__':
    unittest.main()
