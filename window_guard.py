import threading

import win32con
import win32gui

from logging_config import logger

_lock = threading.Lock()
_protected_hwnd = None
_close_stripped = False


def _remove_close_button(hwnd):
    if not hwnd or not win32gui.IsWindow(hwnd):
        return
    try:
        menu = win32gui.GetSystemMenu(hwnd, False)
        if menu:
            win32gui.DeleteMenu(menu, win32con.SC_CLOSE, win32con.MF_BYCOMMAND)

        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        style |= win32con.WS_MINIMIZEBOX
        style &= ~win32con.WS_MAXIMIZEBOX
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
        win32gui.SetWindowPos(
            hwnd,
            None,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_FRAMECHANGED,
        )
    except Exception as e:
        logger.debug("remove_close_button: %s", e)


def _restore_close_button(hwnd):
    if not hwnd or not win32gui.IsWindow(hwnd):
        return
    try:
        win32gui.GetSystemMenu(hwnd, True)
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        style |= win32con.WS_SYSMENU | win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
        win32gui.SetWindowPos(
            hwnd,
            None,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_FRAMECHANGED,
        )
    except Exception as e:
        logger.debug("restore_close_button: %s", e)


def protect(hwnd, block_close=True):
    global _protected_hwnd, _close_stripped
    if not hwnd or not block_close:
        return
    with _lock:
        if _protected_hwnd == hwnd and _close_stripped:
            return
        _protected_hwnd = hwnd
        _close_stripped = True
    _remove_close_button(hwnd)
    logger.debug("WindowGuard: кнопка закрытия скрыта")


def release(hwnd=None):
    global _protected_hwnd, _close_stripped
    with _lock:
        target = hwnd or _protected_hwnd
        _protected_hwnd = None
        _close_stripped = False
    if target:
        _restore_close_button(target)
    logger.debug("WindowGuard: защита окна снята")


def set_topmost(hwnd, enabled):
    if not hwnd:
        return
    flag = win32con.HWND_TOPMOST if enabled else win32con.HWND_NOTOPMOST
    win32gui.SetWindowPos(
        hwnd,
        flag,
        0,
        0,
        0,
        0,
        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
    )
