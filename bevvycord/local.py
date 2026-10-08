"""Local secret loading and single-process ownership. Never execute env files."""
from contextlib import contextmanager
from pathlib import Path
import os
import shlex


def load_env_file(path):
    if not path:
        return
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not key.isidentifier() or not key.isascii():
            raise ValueError(f'Invalid local environment entry at line {number}')
        try:
            parsed = shlex.split(value, comments=False)
        except ValueError:
            raise ValueError(f'Invalid local environment value at line {number}') from None
        if len(parsed) != 1:
            raise ValueError(f'Invalid local environment value at line {number}')
        # Explicit process environment overrides the configured local file.
        os.environ.setdefault(key, parsed[0])


@contextmanager
def character_lock(storage_dir, character):
    import fcntl
    root = Path(storage_dir) / character
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.process.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f'Character {character} is already running against this storage directory') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
