"""Date-based release metadata shared by Studio and its installer."""
import json
from datetime import datetime
from pathlib import Path

RELEASE_FILE = Path(__file__).resolve().parent / 'qbg_studio/static/release.json'


def next_release():
    now = datetime.now().astimezone()
    prefix = f'{now.year}.{now.month}.{now.day}.'
    try:
        previous = json.loads(RELEASE_FILE.read_text(encoding='utf-8'))['version']
        revision = int(previous[len(prefix):]) + 1 if previous.startswith(prefix) else 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        revision = 1
    return {'version': prefix + str(revision),
            'updated_at': now.isoformat(timespec='seconds')}


def write_release(path, release):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix('.json.pending')
    pending.write_text(json.dumps(release, indent=2) + '\n', encoding='utf-8')
    pending.replace(path)
