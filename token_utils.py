import os
import requests
from logging_config import logger

try:
    from config import API_BASE
except ImportError:
    API_BASE = "https://api.gamesense-club.ru"

REQUEST_TIMEOUT = 10


def create_token(dir_path):
    file_path = os.path.join(dir_path, "token.txt")
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            logger.info("Используется существующий токен.")
            return token

    url = f"{API_BASE}/pc/register"
    try:
        response = requests.get(
            url,
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        response_data = response.json()
        token = response_data.get("token")
        if not token:
            raise ValueError("API не вернул token")
    except Exception as e:
        logger.error("Не удалось получить токен ПК: %s", e)
        raise

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(token)
    logger.info("Создан новый токен ПК.")
    return token
