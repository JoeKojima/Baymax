"""Cross-process exclusive lock for the existing people-store transaction."""
import os


def open_lock_file(path):
    fd = os.open(path, os.O_CREAT | (os.O_RDWR if os.name == 'nt' else os.O_RDONLY), 0o644)
    if os.name == 'nt':
        # Windows locks one byte; preserve the POSIX read-only lock-file behavior.
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'\0')
        os.lseek(fd, 0, os.SEEK_SET)
    return fd


def lock_exclusive(fd):
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
    # Closing fd releases the lock on both platforms, including after exceptions.
