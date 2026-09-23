"""Блокировка загрузок в Chrome/Edge через политики (браузер работает, скачивание — нет)."""

import winreg

from logging_config import logger

DOWNLOAD_RESTRICT_ALL = 3

_POLICY_APPS = (
    (winreg.HKEY_CURRENT_USER, r"Software\Policies\Google\Chrome", "Chrome"),
    (winreg.HKEY_CURRENT_USER, r"Software\Policies\Microsoft\Edge", "Edge"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Policies\Google\Chrome", "Chrome"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Policies\Microsoft\Edge", "Edge"),
)

_WRITE_ACCESS = winreg.KEY_SET_VALUE | winreg.KEY_CREATE_SUB_KEY
_active = False


def _set_dword(root, path, name, value):
    try:
        key = winreg.CreateKeyEx(root, path, 0, _WRITE_ACCESS)
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
        winreg.CloseKey(key)
        return True
    except PermissionError:
        logger.debug("Нет прав на запись политики: %s\\%s", path, name)
        return False
    except OSError as e:
        logger.debug("Политика %s: %s", path, e)
        return False


def _delete_value(root, path, name):
    try:
        key = winreg.OpenKeyEx(root, path, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return True
    except PermissionError:
        return False
    except OSError as e:
        logger.debug("delete policy %s: %s", path, e)
        return False


def enable():
    global _active
    if _active:
        return True

    applied = 0

    for root, path, label in _POLICY_APPS:
        if _set_dword(root, path, "DownloadRestrictions", DOWNLOAD_RESTRICT_ALL):
            applied += 1
            logger.info("Блок загрузок: %s (DownloadRestrictions=3)", label)

    _active = applied > 0
    if not _active:
        logger.warning(
            "Не удалось применить политики Chrome/Edge — остаётся мониторинг папок"
        )
    return _active


def disable():
    global _active

    for root, path, _label in _POLICY_APPS:
        _delete_value(root, path, "DownloadRestrictions")

    if _active:
        logger.info("Блок загрузок браузеров снят")
    _active = False
