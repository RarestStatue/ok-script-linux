"""POSIX single-instance locking, in place of a Windows named mutex.

`ok/util/process.py:check_mutex` creates a named kernel mutex keyed on the working
directory to keep a second copy of the app from starting. `flock(2)` on a file with the
same key is the direct equivalent, and is better behaved in one respect: the kernel drops
the lock when the holder exits or is killed, so a crashed instance never leaves a stale
lock the way an abandoned pid file would.

Only the locking primitive lives here. The wait / identify-owner / terminate policy stays
in `process.py`, shared with the Windows path.
"""

import errno
import fcntl
import os
import tempfile

from ok.util.logger import Logger

logger = Logger.get_logger("process")


def lock_path(mutex_name):
    """Where the lock for `mutex_name` lives.

    `XDG_RUNTIME_DIR` is the correct home for this: it is per-user, on tmpfs, and cleaned
    up at logout. It is not guaranteed to exist (a bare `su`, some containers), so fall
    back to the temp dir that the Windows path already uses for its pid marker.
    """
    runtime_dir = os.environ.get('XDG_RUNTIME_DIR')
    if runtime_dir and os.path.isdir(runtime_dir):
        return os.path.join(runtime_dir, f'ok-script-{mutex_name}.lock')
    return os.path.join(tempfile.gettempdir(), f'ok-script-{mutex_name}.lock')


def acquire(mutex_name):
    """Take the lock, or return None if another live process holds it.

    The returned handle is an open file descriptor; hold it for the lifetime of the
    process and pass it to `release()`. flock is tied to the open file description, so a
    second `acquire()` conflicts even from within this same process -- matching
    `CreateMutexW` reporting ERROR_ALREADY_EXISTS.
    """
    path = lock_path(mutex_name)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        logger.error(f'Could not open the single-instance lock {path}: {error}')
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(fd)
        if error.errno not in (errno.EACCES, errno.EAGAIN):
            logger.error(f'Unexpected error locking {path}: {error}')
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode('ascii'))
    except OSError:
        pass       # the pid here is a debugging aid; process.py keeps the real marker
    return fd


def release(handle):
    """Drop the lock. Closing the descriptor releases the flock; this is belt and braces."""
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(handle)
    except OSError:
        pass
