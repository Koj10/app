import logging
import os
import sys

try:
    from config import DEBUG
except ImportError:
    DEBUG = False

if DEBUG:
    DIR = "lib"
else:
    APPDATA_DIR = os.getenv("LOCALAPPDATA")
    DIR = os.path.join(APPDATA_DIR, "GameSense")

LOG_FILE = os.path.join(DIR, "app.log")


def setup_logging():
    logger = logging.getLogger()
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    log_level = logging.DEBUG if DEBUG else logging.INFO
    logger.setLevel(log_level)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if not DEBUG:
        os.makedirs(DIR, exist_ok=True)
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


logger = setup_logging()
