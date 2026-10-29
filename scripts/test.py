"""Run the test suite with temporary project lock and log directories."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from mediaflow import browser_lease
    pattern = sys.argv[1] if len(sys.argv) > 1 else 'test_*.py'
    with tempfile.TemporaryDirectory(prefix='mediaflow-tests-') as temp:
        directory = Path(temp)
        with patch.object(browser_lease, 'ROOT', directory), patch.dict(os.environ, {'MEDIAFLOW_LOG_DIR': str(directory / 'logs')}):
            tests = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=pattern)
            return 0 if unittest.TextTestRunner(verbosity=2).run(tests).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
