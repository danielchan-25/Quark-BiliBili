import tempfile
import unittest
from pathlib import Path

from tools.check_public_tree import inspect_paths


class PublicTreeTests(unittest.TestCase):
    def test_private_files_and_a_credential_are_rejected_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'data').mkdir()
            secret = 'gh' + 'p_' + 'x' * 40
            (root/'source.py').write_text('TOKEN="'+secret+'"', encoding='utf-8')
            (root/'.env').write_text('SECRET=private', encoding='utf-8')
            (root/'data/mediaflow.db').write_bytes(b'db')
            result = inspect_paths(root, ['source.py', '.env', 'data/mediaflow.db'])
            self.assertEqual(len(result), 3)
            self.assertNotIn(secret, str(result))
            self.assertNotIn('SECRET=private', str(result))

    def test_empty_template_is_allowed_and_configured_template_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root/'.env.example'
            path.write_text('# template\nAPI_KEY=\n', encoding='utf-8')
            self.assertEqual(inspect_paths(root, ['.env.example']), [])
            path.write_text('API_KEY=private', encoding='utf-8')
            self.assertEqual(inspect_paths(root, ['.env.example'])[0]['reason'], 'configured_environment_example')
