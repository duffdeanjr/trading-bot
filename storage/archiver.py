"""
storage/archiver.py — Rolling window cleanup for downloads directory.

Removes data files older than RETENTION_DAYS to prevent disk bloat.
Called by ref_library after each full cache load.
"""

import os
import time
import logging
from config import settings

logger = logging.getLogger(__name__)

RETENTION_DAYS = 7


def run_cleanup():
    """Delete download files older than RETENTION_DAYS."""
    cutoff = time.time() - (RETENTION_DAYS * 86400)
    removed = 0

    for root, dirs, files in os.walk(settings.DOWNLOADS_DIR):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    removed += 1
            except OSError:
                pass

    if removed:
        logger.info(f"archiver: cleaned up {removed} files older than {RETENTION_DAYS} days")
    else:
        logger.debug("archiver: no stale files to clean up")
