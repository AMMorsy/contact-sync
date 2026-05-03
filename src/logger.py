import logging
import os
from logging.handlers import TimedRotatingFileHandler

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.environ.get(
    "CONTACT_SYNC_LOG_DIR",
    os.path.join(PROJECT_ROOT, "logs")
)
os.makedirs(LOG_DIR, exist_ok=True)

def get_logger(name="contact-sync"):
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    sync_handler = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, "sync.log"),
        when="midnight",
        backupCount=7
    )
    sync_handler.setLevel(logging.INFO)
    sync_handler.setFormatter(formatter)
    error_handler = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, "errors.log"),
        when="midnight",
        backupCount=30
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(sync_handler)
    logger.addHandler(error_handler)
    logger.addHandler(console_handler)
    return logger
