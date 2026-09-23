import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launcher


class LauncherTests(unittest.TestCase):
    def test_expected_count_follows_saved_dataset_selection(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, 'ROOT', Path(directory)):
            data = Path(directory)/'data'
            data.mkdir()
            (data/'dataset.json').write_text(json.dumps({'items': [1, 2, 3]}), encoding='utf-8')
            (data/'uploaded-state.json').write_text(json.dumps({'dataset': {'items': [1, 2]}}), encoding='utf-8')
            for mode, expected in [('partner', 3), ('upload', 2), ('demo', 6)]:
                (data/'active-dataset.json').write_text(json.dumps({'mode': mode}), encoding='utf-8')
                with self.subTest(mode=mode):
                    self.assertEqual(launcher.expected_products(), expected)

    def test_fresh_clone_starts_with_demo_count(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, 'ROOT', Path(directory)):
            self.assertEqual(launcher.expected_products(), 6)


if __name__ == '__main__':
    unittest.main()
