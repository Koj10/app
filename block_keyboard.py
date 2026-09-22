import ctypes
import threading
from ctypes import wintypes

import win32api
import win32con
import win32gui

from logging_config import logger

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

MOD_ALT = 0x0001
VK_X = 0x58
HOTKEY_ID = 1

MODE_OFF = "off"
MODE_STRICT = "strict"
MODE_SESSION = "session"

_lock = threading.Lock()
_listener_thread = None
_hotkey_thread = None
_listener_thread_id = None
_hotkey_thread_id = None
_running = False
_hook_id = None
_stop_event = threading.Event()
_hotkey_stop = threading.Event()
_hotkey_callback = None
_taskbar_hwnd = None
_keyboard_mode = MODE_OFF
_alt_down = False
_ctrl_down = False
_shift_down = False
_hide_taskbar = False


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


def _get_taskbar_hwnd():
    global _taskbar_hwnd
    if _taskbar_hwnd and win32gui.IsWindow(_taskbar_hwnd):
        return _taskbar_hwnd
    _taskbar_hwnd = win32gui.FindWindow("Shell_TrayWnd", None)
    return _taskbar_hwnd


def _unhook():
    global _hook_id
    if _hook_id is not None:
        try:
            ctypes.windll.user32.UnhookWindowsHookEx(_hook_id)
        except Exception as e:
            logger.debug("UnhookWindowsHookEx: %s", e)
        _hook_id = None


def _should_block(vk, is_keydown):
    global _alt_down, _ctrl_down, _shift_down

    if not is_keydown:
        return False

    mode = _keyboard_mode
    if mode == MODE_OFF:
        return False

    if vk in (win32con.VK_LWIN, win32con.VK_RWIN):
        return True

    if mode == MODE_STRICT:
        if _alt_down and vk in (win32con.VK_TAB, win32con.VK_F4, win32con.VK_ESCAPE):
            return True
        if _ctrl_down and vk == win32con.VK_ESCAPE:
            return True
        if _ctrl_down and _shift_down and vk == win32con.VK_ESCAPE:
            return True
        return False

    if mode == MODE_SESSION:
        if _ctrl_down and _shift_down and vk == win32con.VK_ESCAPE:
            return True
        if _ctrl_down and vk == win32con.VK_ESCAPE:
            return True
        return False

    return False


def _update_modifiers(vk, is_keydown):
    global _alt_down, _ctrl_down, _shift_down

    if vk in (win32con.VK_LMENU, win32con.VK_RMENU, win32con.VK_MENU):
        _alt_down = is_keydown
    elif vk in (win32con.VK_LCONTROL, win32con.VK_RCONTROL, win32con.VK_CONTROL):
        _ctrl_down = is_keydown
    elif vk in (win32con.VK_LSHIFT, win32con.VK_RSHIFT, win32con.VK_SHIFT):
        _shift_down = is_keydown


def listen():
    global _running, _hook_id, _listener_thread_id

    user32 = ctypes.windll.user32
    _listener_thread_id = win32api.GetCurrentThreadId()

    def low_level_handler(nCode, wParam, lParam):
        if nCode < 0 or not _running:
            return user32.CallNextHookEx(_hook_id, nCode, wParam, lParam)

        is_keydown = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
        is_keyup = wParam in (WM_KEYUP, WM_SYSKEYUP)
        if not (is_keydown or is_keyup):
            return user32.CallNextHookEx(_hook_id, nCode, wParam, lParam)

        kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = kb.vkCode
        _update_modifiers(vk, is_keydown)

        if is_keydown and _should_block(vk, True):
            return 1

        return user32.CallNextHookEx(_hook_id, nCode, wParam, lParam)

    hook_proc = ctypes.CFUNCTYPE(
        ctypes.c_long,
        ctypes.c_int,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )(low_level_handler)

    _hook_id = user32.SetWindowsHookExW(
        win32con.WH_KEYBOARD_LL,
        hook_proc,
        win32api.GetModuleHandle(None),
        0,
    )

    if not _hook_id:
        logger.error("Не удалось установить keyboard hook")
        _running = False
        return

    _running = True
    msg = wintypes.MSG()
    try:
        while _running and not _stop_event.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        _unhook()
        _running = False
        _alt_down = False
        _ctrl_down = False
        _shift_down = False


def _hotkey_loop():
    global _hotkey_thread_id

    user32 = ctypes.windll.user32
    _hotkey_thread_id = win32api.GetCurrentThreadId()

    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_ALT, VK_X):
        logger.error("RegisterHotKey Alt+X не удался")
        return

    msg = wintypes.MSG()
    try:
        while not _hotkey_stop.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                cb = _hotkey_callback
                if cb:
                    try:
                        cb()
                    except Exception as e:
                        logger.error("Hotkey callback: %s", e)
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)
        _hotkey_thread_id = None


def start_hotkey(callback):
    global _hotkey_callback, _hotkey_thread
    _hotkey_callback = callback
    with _lock:
        if _hotkey_thread and _hotkey_thread.is_alive():
            return
        _hotkey_stop.clear()
        _hotkey_thread = threading.Thread(target=_hotkey_loop, daemon=True, name="hotkey")
        _hotkey_thread.start()


def stop_hotkey():
    global _hotkey_thread, _hotkey_thread_id
    _hotkey_stop.set()
    with _lock:
        if _hotkey_thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(_hotkey_thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if _hotkey_thread and _hotkey_thread.is_alive():
            _hotkey_thread.join(timeout=1.0)
        _hotkey_thread = None
    _hotkey_stop.clear()


def _apply_taskbar_visibility():
    hwnd = _get_taskbar_hwnd()
    if hwnd:
        show = 5 if not _hide_taskbar else 0
        ctypes.windll.user32.ShowWindow(hwnd, show)


def set_mode(mode, hide_taskbar=False):
    """strict — ожидание; session — игровая сессия; off — админ."""
    global _keyboard_mode, _hide_taskbar, _listener_thread

    if mode not in (MODE_OFF, MODE_STRICT, MODE_SESSION):
        mode = MODE_OFF

    with _lock:
        _keyboard_mode = mode
        _hide_taskbar = hide_taskbar and mode != MODE_OFF
        _apply_taskbar_visibility()

        if mode == MODE_OFF:
            stop_block_unlocked()
            return

        if _running or (_listener_thread and _listener_thread.is_alive()):
            logger.debug("Клавиатура: режим %s", mode)
            return

        _stop_event.clear()
        logger.debug("Блокировка клавиш: режим %s", mode)
        _listener_thread = threading.Thread(target=listen, daemon=True, name="keyboard-block")
        _listener_thread.start()


def stop_block_unlocked():
    global _running, _listener_thread, _listener_thread_id, _keyboard_mode

    _keyboard_mode = MODE_OFF
    _apply_taskbar_visibility()

    if not _running and not (_listener_thread and _listener_thread.is_alive()):
        return

    logger.debug("Блокировка клавиш остановлена")
    _running = False
    _stop_event.set()
    _unhook()

    if _listener_thread_id:
        try:
            ctypes.windll.user32.PostThreadMessageW(_listener_thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass

    if _listener_thread and _listener_thread.is_alive():
        _listener_thread.join(timeout=1.0)
    _listener_thread = None
    _listener_thread_id = None
    _stop_event.clear()


def stop_block():
    with _lock:
        stop_block_unlocked()


def start_block(mode=MODE_STRICT, hide_taskbar=True):
    set_mode(mode, hide_taskbar=hide_taskbar)


def taskbar(active=True):
    global _hide_taskbar
    _hide_taskbar = not active
    _apply_taskbar_visibility()
