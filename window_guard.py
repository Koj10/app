import ctypes
import threading
from ctypes import wintypes

import win32con
import win32gui

from logging_config import logger

WM_CLOSE = 0x0010
WM_SYSCOMMAND = 0x0112
SC_CLOSE = 0xF060
GWL_WNDPROC = -4

_lock = threading.Lock()
_thread = None
_stop = threading.Event()
_protected_hwnd = None
_block_close = False
_old_wndproc = None
_wndproc_ref = None

user32 = ctypes.windll.user32
_WndProcType = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


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


def _install_wndproc_hook(hwnd):
    global _old_wndproc, _wndproc_ref

    if not hwnd or _old_wndproc:
        return

    def wndproc(h, msg, wparam, lparam):
        if _block_close:
            if msg == WM_CLOSE:
                return 0
            if msg == WM_SYSCOMMAND and (wparam & 0xFFF0) == SC_CLOSE:
                return 0
        return user32.CallWindowProcW(_old_wndproc, h, msg, wparam, lparam)

    _wndproc_ref = _WndProcType(wndproc)
    _old_wndproc = user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, _wndproc_ref)
    logger.debug("WindowGuard: WM_CLOSE hook установлен")


def _remove_wndproc_hook(hwnd):
    global _old_wndproc, _wndproc_ref
    if hwnd and _old_wndproc:
        try:
            user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, _old_wndproc)
        except Exception as e:
            logger.debug("remove wndproc: %s", e)
    _old_wndproc = None
    _wndproc_ref = None


def _loop():
    while not _stop.is_set():
        with _lock:
            hwnd = _protected_hwnd
            block = _block_close
        if hwnd and win32gui.IsWindow(hwnd) and block:
            _remove_close_button(hwnd)
        _stop.wait(1.5)


def protect(hwnd, block_close=True):
    global _protected_hwnd, _thread, _block_close
    if not hwnd:
        return
    with _lock:
        _protected_hwnd = hwnd
        _block_close = block_close

    _remove_close_button(hwnd)
    _install_wndproc_hook(hwnd)

    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="window-guard")
        _thread.start()
    logger.debug("WindowGuard: защита окна включена")


def release(hwnd=None):
    global _protected_hwnd, _thread, _block_close
    _block_close = False
    _stop.set()
    with _lock:
        target = hwnd or _protected_hwnd
        _protected_hwnd = None
        if _thread and _thread.is_alive():
            _thread.join(timeout=2.0)
        _thread = None
    _stop.clear()

    if target:
        _remove_wndproc_hook(target)
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
