import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


def load(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).parent/'payload'/name)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AnyDeskIdTests(unittest.TestCase):
    def test_valid_id_is_saved_and_read(self):
        helper=load('ember-anydesk-id.py')
        status=load('ember-status.py')
        with tempfile.TemporaryDirectory() as temporary:
            target=Path(temporary)/'id'
            with patch.object(helper.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'1234567890\n','')):
                helper.refresh(target)
            self.assertEqual(status.anydesk_id(target),'1234567890')

    def test_error_text_is_never_saved(self):
        helper=load('ember-anydesk-id.py')
        with tempfile.TemporaryDirectory() as temporary:
            target=Path(temporary)/'id'
            with patch.object(helper.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'private error text','')):
                helper.refresh(target)
            self.assertFalse(target.exists())

    def test_status_rejects_non_id_cache_contents(self):
        status=load('ember-status.py')
        with tempfile.TemporaryDirectory() as temporary:
            target=Path(temporary)/'id'
            target.write_text('password=must-not-be-shown')
            self.assertEqual(status.anydesk_id(target),'not available yet')


if __name__=='__main__':
    unittest.main()
