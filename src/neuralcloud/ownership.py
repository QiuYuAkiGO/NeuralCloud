"""OS-held ownership; a crashed process cannot leave a stale logical lease."""

from pathlib import Path
import os


class Ownership:
    def __init__(self, path: Path):
        self.file = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0, 2)
                if self.file.tell() == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise RuntimeError("INSTANCE_ALREADY_OWNED") from None

    def close(self):
        # Never unlink: replacing a lock inode can create two owners.
        self.file.close()
