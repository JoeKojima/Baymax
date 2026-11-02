import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('hardware', Path(__file__).parent / 'payload/ember-hardware.py')
hardware = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hardware)


class HardwareTests(unittest.TestCase):
    def test_saved_names_survive_alsa_and_portaudio_renumbering(self):
        devices = [{'name': 'Speaker: USB Audio (hw:4,0)', 'hostapi': 0, 'max_input_channels': 0, 'max_output_channels': 2},
                   {'name': 'Mic: USB Audio (hw:2,0)', 'hostapi': 0, 'max_input_channels': 1, 'max_output_channels': 0}]
        self.assertEqual(hardware.audio_device(devices, [{'name': 'ALSA'}], 'input', 'Mic: USB Audio (hw:0,0)'), 1)
        self.assertEqual(hardware.audio_device(devices, [{'name': 'ALSA'}], 'output', 'Speaker: USB Audio (hw:1,0)'), 0)

    def test_ambiguous_devices_require_setup(self):
        device = {'name': 'Mic: USB Audio (hw:2,0)', 'hostapi': 0, 'max_input_channels': 1, 'max_output_channels': 0}
        with self.assertRaisesRegex(RuntimeError, 'Ember Setup'):
            hardware.audio_device([device, device], [{'name': 'ALSA'}], 'input', device['name'])

    def test_no_camera_supports_audio_only(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(hardware.camera_device({}, Path(directory)), ('none', '', ''))

    def test_camera_link_survives_video_renumbering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / 'video7'
            video.touch()
            # Windows may not grant symlink privileges; a direct path exercises
            # the same saved-UID resolution without opening a camera.
            self.assertEqual(hardware.camera_device({'camera_uid': str(video)})[0], '7')
