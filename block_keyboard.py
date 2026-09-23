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
LLKHF_ALTDOWN = 0x20
THREAD_PRIORITY_HIGHEST = 2

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


def _is_win_key(vk, scan_code):
    return vk in (win32con.VK_LWIN, win32con.VK_RWIN) or (scan_code & 0xFF) in (0x5B, 0x5C)


def _should_block(vk, is_keydown, alt_held, scan_code):
    mode = _keyboard_mode
    if mode == MODE_OFF:
        return False

    if mode == MODE_STRICT:
        # До активации пакета и снова после его конца Win не доходит до меню Пуск.
        if _is_win_key(vk, scan_code):
            return True
        # Alt+Tab глушится целиком, как Win: обе клавиши не доходят до переключателя задач.
        if vk in (
            win32con.VK_MENU,
            win32con.VK_LMENU,
            win32con.VK_RMENU,
            win32con.VK_TAB,
        ) or (scan_code & 0xFF) in (0x0F, 0x38):
            return True
        if alt_held and vk in (
            win32con.VK_ESCAPE,
            win32con.VK_F4,
            win32con.VK_SPACE,
        ):
            return True
        if _ctrl_down and vk == win32con.VK_ESCAPE:
            return True
        if not is_keydown:
            return False
        if vk == win32con.VK_APPS:
            return True
        return False

    if mode == MODE_SESSION:
        # Ctrl+Shift+Esc открывает диспетчер задач и на отпускании.
        if _ctrl_down and vk == win32con.VK_ESCAPE:
            return True
        if not is_keydown:
            return False
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


# LRESULT на 64-bit — указатель. c_long здесь 32 бита, из-за этого Windows
# игнорирует «проглоченную» клавишу и Win / Alt+Tab проходят.
_LRESULT = ctypes.c_ssize_t
_HOOKPROC = ctypes.WINFUNCTYPE(
    _LRESULT,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
_hook_proc = None
_user32_ready = False
_on_win_key = None


def set_on_win_key(callback):
    """Мгновенный сигнал, что нажата Win. Колбэк не должен делать ничего тяжёлого."""
    global _on_win_key
    _on_win_key = callback


def _prepare_user32(user32):
    global _user32_ready
    if _user32_ready:
        return
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int,
        _HOOKPROC,
        wintypes.HINSTANCE,
        wintypes.DWORD,
    ]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [
        wintypes.HHOOK,
        ctypes.c_int,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.CallNextHookEx.restype = _LRESULT
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = _LRESULT
    _user32_ready = True


def listen():
    global _running, _hook_id, _listener_thread_id, _hook_proc

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    _prepare_user32(user32)
    _listener_thread_id = kernel32.GetCurrentThreadId()
    kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST)

    def low_level_handler(nCode, wParam, lParam):
        try:
            if nCode >= 0 and _running:
                is_keydown = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                is_keyup = wParam in (WM_KEYUP, WM_SYSKEYUP)
                if is_keydown or is_keyup:
                    kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    vk = kb.vkCode
                    scan_code = kb.scanCode
                    alt_held = bool(kb.flags & LLKHF_ALTDOWN) or _alt_down
                    if (
                        not alt_held
                        and _keyboard_mode == MODE_STRICT
                        and vk in (win32con.VK_TAB, win32con.VK_ESCAPE, win32con.VK_F4)
                    ):
                        alt_held = bool(user32.GetAsyncKeyState(win32con.VK_MENU) & 0x8000)
                    _update_modifiers(vk, is_keydown)
                    if _should_block(vk, is_keydown, alt_held, scan_code):
                        if _is_win_key(vk, scan_code) and _keyboard_mode == MODE_STRICT:
                            callback = _on_win_key
                            if callback is not None:
                                callback()
                        return 1
        except Exception:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    _hook_proc = _HOOKPROC(low_level_handler)
    # Для WH_KEYBOARD_LL модуль должен быть NULL, процедура живёт в этом процессе.
    _hook_id = user32.SetWindowsHookExW(
        win32con.WH_KEYBOARD_LL,
        _hook_proc,
        None,
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
    global _keyboard_mode, _hide_taskbar, _listener_thread, _alt_down, _ctrl_down, _shift_down

    if mode not in (MODE_OFF, MODE_STRICT, MODE_SESSION):
        mode = MODE_OFF

    with _lock:
        _keyboard_mode = mode
        _hide_taskbar = hide_taskbar and mode != MODE_OFF
        _apply_taskbar_visibility()

        if mode != MODE_STRICT:
            _alt_down = False
            _ctrl_down = False
            _shift_down = False

        if mode == MODE_OFF:
            stop_block_unlocked()
        elif not (_running or (_listener_thread and _listener_thread.is_alive())):
            _stop_event.clear()
            logger.debug("Блокировка клавиш: режим %s", mode)
            _listener_thread = threading.Thread(target=listen, daemon=True, name="keyboard-block")
            _listener_thread.start()
        else:
            logger.debug("Клавиатура: режим %s", mode)

    # Клавиша Win заблокирована только в ожидании. В сессии снимается, после пакета включается снова.
    set_win_key_block(mode == MODE_STRICT)


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
    set_win_key_block(False)


def start_block(mode=MODE_STRICT, hide_taskbar=True):
    set_mode(mode, hide_taskbar=hide_taskbar)


def taskbar(active=True):
    global _hide_taskbar
    _hide_taskbar = not active
    _apply_taskbar_visibility()


def set_foreground_lock(enabled):
    """В ожидании другие окна не должны забирать фокус (Alt+Tab)."""
    try:
        ctypes.windll.user32.LockSetForegroundWindow(1 if enabled else 2)
    except Exception as e:
        logger.debug("LockSetForegroundWindow: %s", e)


# Системная блокировка Win: пока регистрация жива, оболочка не открывает Пуск.
# Снимается в сессии и включается снова, когда пакет закончился.
_RIDEV_REMOVE = 0x00000001
_RIDEV_INPUTSINK = 0x00000100
_RIDEV_NOHOTKEYS = 0x00000200
_RID_INPUT = 0x10000003
_WM_INPUT = 0x00FF
_WM_WIN_ON = 0x8001
_WM_WIN_OFF = 0x8002
_WIN_BLOCK_CLASS = "GameSenseWinBlock"

_win_block_thread = None
_win_block_hwnd = None
_win_block_proc = None
_win_block_ready = threading.Event()
_win_block_enabled = False
_win_block_api_ready = False
_raw_win_blocked = False
_raw_lock = threading.Lock()


class _RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class _RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _prepare_win_block_api():
    global _win_block_api_ready
    if _win_block_api_ready:
        return
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.RegisterRawInputDevices.argtypes = [
        ctypes.POINTER(_RAWINPUTDEVICE),
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.RegisterRawInputDevices.restype = wintypes.BOOL
    user32.GetRawInputData.argtypes = [
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.UINT),
        wintypes.UINT,
    ]
    user32.GetRawInputData.restype = wintypes.UINT
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    _win_block_api_ready = True


def _drain_raw_input(lparam):
    user32 = ctypes.windll.user32
    size = wintypes.UINT(0)
    header = ctypes.sizeof(_RAWINPUTHEADER)
    user32.GetRawInputData(lparam, _RID_INPUT, None, ctypes.byref(size), header)
    if not size.value:
        return
    buf = ctypes.create_string_buffer(size.value)
    user32.GetRawInputData(lparam, _RID_INPUT, buf, ctypes.byref(size), header)


def _apply_raw_win_block(hwnd, enabled):
    global _raw_win_blocked
    with _raw_lock:
        user32 = ctypes.windll.user32
        rid = _RAWINPUTDEVICE()
        rid.usUsagePage = 0x01
        rid.usUsage = 0x06
        if enabled and hwnd:
            rid.dwFlags = _RIDEV_INPUTSINK | _RIDEV_NOHOTKEYS
            rid.hwndTarget = hwnd
        else:
            rid.dwFlags = _RIDEV_REMOVE
            rid.hwndTarget = None
            enabled = False
        ok = user32.RegisterRawInputDevices(
            ctypes.byref(rid), 1, ctypes.sizeof(_RAWINPUTDEVICE)
        )
        if not ok:
            logger.warning(
                "Не удалось %s клавишу Win: %s",
                "заблокировать" if enabled else "разблокировать",
                ctypes.windll.kernel32.GetLastError(),
            )
            return
        if enabled != _raw_win_blocked:
            _raw_win_blocked = enabled
            logger.info("Клавиша Win %s", "заблокирована" if enabled else "разблокирована")


def _win_block_wndproc(hwnd, msg, wparam, lparam):
    try:
        if msg == _WM_INPUT:
            _drain_raw_input(lparam)
            return 0
        if msg == _WM_WIN_ON:
            if _win_block_enabled:
                _apply_raw_win_block(hwnd, True)
            return 0
        if msg == _WM_WIN_OFF:
            _apply_raw_win_block(hwnd, False)
            return 0
    except Exception as e:
        logger.debug("win block wnd: %s", e)
        return 0
    return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _win_block_thread_main():
    global _win_block_hwnd, _win_block_proc

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    _prepare_win_block_api()
    hinst = kernel32.GetModuleHandleW(None)
    _win_block_proc = _WNDPROC(_win_block_wndproc)
    wc = _WNDCLASSW()
    wc.lpfnWndProc = _win_block_proc
    wc.hInstance = hinst
    wc.lpszClassName = _WIN_BLOCK_CLASS
    if not user32.RegisterClassW(ctypes.byref(wc)):
        err = kernel32.GetLastError()
        if err not in (0, 1410):
            logger.warning("Не удалось зарегистрировать блокировку Win: %s", err)
            _win_block_ready.set()
            return
    hwnd = user32.CreateWindowExW(
        0,
        _WIN_BLOCK_CLASS,
        _WIN_BLOCK_CLASS,
        0,
        0,
        0,
        0,
        0,
        wintypes.HWND(-3),
        None,
        hinst,
        None,
    )
    _win_block_hwnd = hwnd
    _win_block_ready.set()
    if not hwnd:
        logger.warning("Не удалось создать окно блокировки Win: %s", kernel32.GetLastError())
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def _ensure_win_block_thread():
    global _win_block_thread
    if _win_block_thread and _win_block_thread.is_alive():
        return
    _win_block_ready.clear()
    _win_block_thread = threading.Thread(
        target=_win_block_thread_main, daemon=True, name="win-key-block"
    )
    _win_block_thread.start()


def _send_win_block(hwnd, enabled):
    """Ждёт, пока поток окна реально снимет или поставит блок. PostMessage терялся в очереди."""
    _prepare_win_block_api()
    if not hwnd:
        if not enabled:
            _apply_raw_win_block(None, False)
        return
    result = ctypes.c_size_t(0)
    ok = ctypes.windll.user32.SendMessageTimeoutW(
        hwnd,
        _WM_WIN_ON if enabled else _WM_WIN_OFF,
        0,
        0,
        0x0002,
        1000,
        ctypes.byref(result),
    )
    if not ok or not enabled:
        _apply_raw_win_block(hwnd if enabled else None, enabled)


def set_win_key_block(enabled):
    """Блокирует клавишу Win, пока клиент в ожидании. В сессии и у админа снимается."""
    global _win_block_enabled

    enabled = bool(enabled)
    _win_block_enabled = enabled
    if not enabled and not (_win_block_thread and _win_block_thread.is_alive()):
        _prepare_win_block_api()
        _apply_raw_win_block(None, False)
        return

    if enabled:
        _ensure_win_block_thread()
        if not _win_block_ready.wait(2.0):
            logger.warning("Блокировка клавиши Win не подготовилась")
            return
    hwnd = _win_block_hwnd
    _send_win_block(hwnd, enabled)
