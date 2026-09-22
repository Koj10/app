import threading
import time

import win32con
import win32gui

from logging_config import logger

_lock = threading.Lock()
_thread = None
_stop = threading.Event()
_protected_hwnd = None


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


def _loop():
    while not _stop.is_set():
        with _lock:
            hwnd = _protected_hwnd
        if hwnd and win32gui.IsWindow(hwnd):
            _remove_close_button(hwnd)
        _stop.wait(2.0)


def protect(hwnd):
    global _protected_hwnd, _thread
    if not hwnd:
        return
    with _lock:
        _protected_hwnd = hwnd
    _remove_close_button(hwnd)

    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="window-guard")
        _thread.start()
    logger.debug("WindowGuard: защита окна включена")


def release(hwnd=None):
    global _protected_hwnd, _thread
    _stop.set()
    with _lock:
        target = hwnd or _protected_hwnd
        _protected_hwnd = None
        if _thread and _thread.is_alive():
            _thread.join(timeout=2.0)
        _thread = None
    _stop.clear()

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
