"""No module may name something that no longer exists.

Deleting the classic Translation Bot (20 September 2026) removed _PREVIEW_CACHE
while invalidate_sheet_caches() still named it. Every sheet write then raised
NameError inside a best-effort step, so for a day no created question reached
the team's shared duplicate index, and nothing failed loudly.
"""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'qbg_studio'), str(ROOT), str(ROOT / '2_Question_Creation_Pipeline')]


class UndefinedNames(unittest.TestCase):
    def test_every_module_only_names_what_exists(self):
        try:
            import pyflakes  # noqa: F401
        except ImportError:
            self.skipTest('pyflakes is not installed')
        files = [str(p) for folder in ('qbg_studio', '2_Question_Creation_Pipeline', '.')
                 for p in sorted((ROOT / folder).glob('*.py'))]
        result = subprocess.run([sys.executable, '-m', 'pyflakes', *files], capture_output=True, text=True)
        undefined = [line for line in result.stdout.splitlines() if 'undefined name' in line]
        self.assertEqual(undefined, [])

    def test_sheet_caches_can_be_dropped(self):
        import pipeline_bridge as pb
        pb.invalidate_sheet_caches()
        pb.invalidate_translation_preview()


if __name__ == '__main__':
    unittest.main()
