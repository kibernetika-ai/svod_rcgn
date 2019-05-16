import cv2
import numpy as np
from ml_serving.drivers import driver as sdrv

from svod_rcgn.tools import print_fun


class Driver(object):

    def __init__(self, bg_remove_path):
        print_fun('Load BG_REMOVE model')
        drv = sdrv.load_driver('tensorflow')
        self.drv = drv()
        self.drv.load_model(bg_remove_path)

    def mask(self, frame):
        inp = cv2.resize(frame[:, :, ::-1].astype(np.float32), (160, 160)) / 255.0
        outputs = self.drv.predict({'image': np.expand_dims(inp, 0)})
        return cv2.resize(outputs['output'][0], (frame.shape[1], frame.shape[0]))

    def apply_mask(self, frame):
        return frame * np.expand_dims(self.mask(frame), 2)


def get_driver(bg_remove_path):
    if bg_remove_path:
        return Driver(bg_remove_path)
    else:
        return None


def add_bg_remove_args(parser):
    parser.add_argument(
        '--bg_remove_path',
        help='Path to Tensorflow background remove model.',
        default=None,
    )
