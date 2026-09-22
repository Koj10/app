import os
import sys
import winreg

from logging_config import logger


def add_to_autostart():
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "GameSense"
        exe_path = os.path.join(os.path.dirname(sys.executable), "GameSense.exe")

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            try:
                current, _ = winreg.QueryValueEx(key, app_name)
                if current == exe_path:
                    return
            except FileNotFoundError:
                pass

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, exe_path)
    except Exception as e:
        logger.warning("Автозагрузка не настроена: %s", e)
