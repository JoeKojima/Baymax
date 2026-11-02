import ast
import os
from pathlib import Path
import tempfile
import unittest

class ImagePermissionsTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'Requires Linux permissions')
    def test_private_builder_umask_keeps_runtime_directories_traversable(self):
        tree = ast.parse((Path(__file__).parent / 'inject_image.py').read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'image_directory')
        scope = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<image_directory>', 'exec'), scope)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = os.umask(0o077)
            try:
                path = root / 'etc/X11/Xsession.d'
                scope['image_directory'](path)
                for name in ['etc', 'etc/X11', 'etc/X11/Xsession.d']:
                    self.assertEqual((root/name).stat().st_mode & 0o777, 0o755)
                secret = root/'private'
                secret.write_text('test-secret')
                self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
                self.assertEqual(os.umask(0o077), 0o077)
            finally:
                os.umask(old)

if __name__ == '__main__':
    unittest.main()
