"""Small filesystem helpers shared by Runtime, GUI, and deploy tools."""

from __future__ import annotations

import os
import time
from pathlib import Path


def replace_path(source: Path | str, destination: Path | str, *, attempts: int = 20) -> None:
    """Atomically replace ``destination`` with ``source``.

    Windows can briefly deny ``os.replace`` while a scanner still holds the
    destination or the just-written temporary. Retrying keeps the same atomic
    contract without leaving a half-written target.
    """
    delay = 0.02
    last: OSError | None = None
    src, dest = os.fspath(source), os.fspath(destination)
    for _ in range(max(1, attempts)):
        try:
            os.replace(src, dest)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(delay)
            delay = min(0.4, delay * 1.7)
        except OSError as exc:
            # Sharing violations sometimes surface as EINVAL/EACCES on Windows.
            if getattr(exc, "winerror", None) not in {5, 32} and exc.errno not in {13, 16}:
                raise
            last = exc
            time.sleep(delay)
            delay = min(0.4, delay * 1.7)
    assert last is not None
    raise last
