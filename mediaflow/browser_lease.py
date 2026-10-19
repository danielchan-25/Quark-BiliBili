"""Non-blocking OS lease shared by every client of a browser endpoint.

The OS releases the lease on process exit. Keep the file: unlinking a locked
file would allow another process to lock a different inode on Unix.
"""
import hashlib
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from mediaflow.config import ROOT


class BrowserBusy(RuntimeError):
    pass


class BrowserLease:
    def __init__(self, endpoint, directory=None):
        parsed = urlparse(endpoint)
        host = parsed.hostname or ''
        if host in ('localhost', '127.0.0.1', '::1'):
            host = 'loopback'
        identity = f'{host}:{parsed.port}' if host else endpoint.rstrip('/')
        name = hashlib.sha256(identity.encode()).hexdigest() + '.lock'
        self.path = Path(directory or ROOT/'data/browser-locks') / name
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            if handle.seek(0, 2) == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            deadline = time.monotonic() + 1
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)

        except OSError as exc:
            handle.close()
            raise BrowserBusy('Browser is already in use by another project task') from exc
        self.handle = handle
        return self

    def release(self):
        handle, self.handle = self.handle, None
        if handle is not None:
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
