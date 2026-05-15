import logging
import os
from logging.handlers import TimedRotatingFileHandler

LOG_DIR = "/root/contact-sync/logs"

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
