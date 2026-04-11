# storage/archiver.py
import logging
from config import settings
from storage import database

logger = logging.getLogger(__name__)

def run_cleanup():
    """Delete data older than RETENTION_DAYS from large tables."""
    logger.info(f"archiver: running cleanup (retention={settings.RETENTION_DAYS} days)")
    database.purge_old_data(days=settings.RETENTION_DAYS)
    logger.info("archiver: cleanup complete")