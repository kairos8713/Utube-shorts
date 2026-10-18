"""
log_setup.py — rotating file logs for every module.

    from log_setup import get_logger
    log = get_logger("publisher")       # → logs/publisher.log (5 MB × 5)
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from config import settings

_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_configured = set()


def get_logger(name: str, filename: str = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if name in _configured:
        return logger
    logger.setLevel(logging.INFO)
    log_dir = settings.LOG_DIR
    os.makedirs(log_dir, exist_ok=True)
    fh = RotatingFileHandler(
        os.path.join(log_dir, filename or f"{name.split('.')[0]}.log"),
        maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
               for h in logger.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(_FMT))
        logger.addHandler(sh)
    logger.propagate = False
    _configured.add(name)
    return logger
