"""Start NoteCraft from this folder (development). Double-click "Start NoteCraft.bat"."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from note_studio.launch import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
