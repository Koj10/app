VERSION = "1.1.5"

import atexit
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import ntplib
import requests
import webview
import win32con
import win32gui

import add_autostart
import block_keyboard
import policy_guard
import ui_invoke
import window_guard
from logging_config import logger
from token_utils import create_token
from update import check_for_updates, download_and_install_update

try:
    import config

    DEBUG = config.DEBUG
    API_BASE = config.API_BASE
    SITE_BASE = config.SITE_BASE
    ROBLOX_LAUNCHER = getattr(config, "ROBLOX_LAUNCHER", "") or ""
    POLL_INTERVAL = int(getattr(config, "POLL_INTERVAL_IDLE", 10))
except Exception:
    DEBUG = False
    API_BASE = "https://api.gamesense-club.ru"
    SITE_BASE = "https://pc.gamesense-club.ru"
    ROBLOX_LAUNCHER = ""
    POLL_INTERVAL = 10

POLL_INTERVAL_BUSY = 6
POLL_INTERVAL_SESSION = 12
POLL_INTERVAL_ERROR = 25
API_TIMEOUT = 4
NTP_RESYNC_SECONDS = 300

if DEBUG:
    DIR = "lib"
else:
    APPDATA_DIR = os.getenv("LOCALAPPDATA")
    DIR = os.path.join(APPDATA_DIR, "GameSense")

os.makedirs(DIR, exist_ok=True)

if not DEBUG:
    add_autostart.add_to_autostart()

try:
    token = create_token(DIR)
except Exception:
    logger.critical("Не удалось инициализировать токен ПК. Проверьте сеть и API.")
    sys.exit(1)

http = requests.Session()
http.headers.update({"Content-Type": "application/json", "Authorization": f"Bearer {token}"})

window = None
_mode = None  # waiting | session | admin
_stop_polling = threading.Event()
_screen_size = None
_hwnd = None
_app_ready = threading.Event()
_last_status = None

_ntp_offset = timedelta(0)
_ntp_synced_at = 0.0

logger.info("Версия: %s", VERSION)


class ShellApi:
    """API для кнопок на сайте внутри shell."""

    def minimize_to_desktop(self):
        minimize_to_desktop()
        return True

    def restore_app(self):
        restore_app_window()
        return True

    def is_shell(self):
        return True


def minimize_to_desktop():
    if not window:
        return
    try:
        window.minimize()
        logger.info("GameSense свёрнут на рабочий стол")
    except Exception as e:
        logger.error("minimize_to_desktop: %s", e)


def restore_app_window():
    if not window:
        return
    try:
        window.restore()
        window.show()
    except Exception as e:
        logger.error("restore_app_window: %s", e)


def _sync_ntp(force=False):
    global _ntp_offset, _ntp_synced_at
    now = time.monotonic()
    if not force and now - _ntp_synced_at < NTP_RESYNC_SECONDS:
        return
    client = ntplib.NTPClient()
    for server in ("pool.ntp.org", "time.google.com"):
        try:
            response = client.request(server, version=3, timeout=2)
            _ntp_offset = timedelta(seconds=response.tx_time - time.time())
            _ntp_synced_at = now
            return
        except Exception:
            continue
    _ntp_synced_at = now


def _now_local():
    _sync_ntp()
    return datetime.now(timezone.utc) + _ntp_offset


def _session_expired(response_data):
    time_str = response_data.get("time_active")
    if not time_str:
        return True

    time_zone = int(response_data.get("time_zone") or 0)
    time_active = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
    time_active += timedelta(hours=time_zone)
    now_time = _now_local().replace(tzinfo=None)
    now_time += timedelta(hours=time_zone)
    return now_time > time_active


def _on_closing():
    if DEBUG:
        return True
    if _mode in ("waiting", "session"):
        logger.debug("Закрытие заблокировано (режим %s)", _mode)
        return False
    return True


def edit_status():
    http.post(
        f"{API_BASE}/pc/status",
        json={"token": token, "status": "активен"},
        timeout=API_TIMEOUT,
    )


def _get_screen_size():
    global _screen_size
    if _screen_size:
        return _screen_size
    screens = webview.screens
    if not screens:
        return None
    _screen_size = (screens[0].width, screens[0].height)
    return _screen_size


def _find_hwnd():
    global _hwnd
    if _hwnd and win32gui.IsWindow(_hwnd):
        return _hwnd
    _hwnd = win32gui.FindWindow(None, "GameSense")
    return _hwnd


def _native_move_resize(hwnd, width, height, x=0, y=0, topmost=True):
    if not hwnd:
        return
    window_guard.set_topmost(hwnd, topmost)
    win32gui.SetWindowPos(
        hwnd,
        win32con.HWND_TOPMOST if topmost else win32con.HWND_NOTOPMOST,
        x,
        y,
        width,
        height,
        win32con.SWP_SHOWWINDOW,
    )


def _native_show(hwnd):
    if hwnd:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)


def _show_in_taskbar(hwnd):
    if not hwnd:
        return
    style = (
        win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        & ~win32con.WS_EX_TOOLWINDOW
        & ~win32con.WS_EX_NOACTIVATE
    )
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style)
    _native_show(hwnd)


def _show_waiting_window_impl():
    if not window:
        return
    try:
        size = _get_screen_size()
        if not size:
            return
        screen_width, screen_height = size
        hwnd = _find_hwnd()
        _show_in_taskbar(hwnd)
        _native_move_resize(hwnd, screen_width, screen_height, topmost=True)
        window_guard.protect(hwnd, block_close=True)
    except Exception as e:
        logger.error("_show_waiting_window: %s", e)


def _show_session_window_impl():
    if not window:
        return
    try:
        size = _get_screen_size()
        if not size:
            return
        screen_width, screen_height = size
        hwnd = _find_hwnd()
        _show_in_taskbar(hwnd)
        _native_move_resize(hwnd, screen_width, screen_height, topmost=False)
        window_guard.protect(hwnd, block_close=True)
        _native_show(hwnd)
    except Exception as e:
        logger.error("_show_session_window: %s", e)


def _show_admin_window_impl():
    if not window:
        return
    try:
        hwnd = _find_hwnd()
        window_guard.release(hwnd)
        window_guard.set_topmost(hwnd, False)
        _show_in_taskbar(hwnd)
        _native_show(hwnd)
    except Exception as e:
        logger.error("_show_admin_window: %s", e)


def _show_waiting_window():
    ui_invoke.run(_show_waiting_window_impl)


def _show_session_window():
    ui_invoke.run(_show_session_window_impl)


def _show_admin_window():
    ui_invoke.run(_show_admin_window_impl)


def _maybe_launch_repair_tool():
    if not ROBLOX_LAUNCHER or not os.path.isfile(ROBLOX_LAUNCHER):
        return
    try:
        working_dir = os.path.dirname(ROBLOX_LAUNCHER) or None
        subprocess.Popen([ROBLOX_LAUNCHER], cwd=working_dir)
    except Exception as e:
        logger.error("Не удалось запустить лаунчер: %s", e)


def _start_explorer():
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq explorer.exe", "/NH"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5,
        )
        if "explorer.exe" not in (result.stdout or "").lower():
            subprocess.Popen(["explorer.exe"])
    except Exception as e:
        logger.debug("start explorer: %s", e)


def _enter_waiting():
    global _mode
    if _mode == "waiting":
        return
    _mode = "waiting"
    if DEBUG:
        return

    logger.info("Режим: ожидание")
    _show_waiting_window()
    block_keyboard.set_mode(block_keyboard.MODE_STRICT, hide_taskbar=True)
    policy_guard.set_mode(policy_guard.MODE_WAITING)


def _enter_session():
    global _mode
    first_entry = _mode != "session"
    if _mode == "session":
        return
    _mode = "session"
    if DEBUG:
        return

    logger.info("Режим: игровая сессия")
    _start_explorer()
    _show_session_window()
    block_keyboard.set_mode(block_keyboard.MODE_SESSION, hide_taskbar=False)
    policy_guard.set_mode(policy_guard.MODE_SESSION)

    if first_entry and window:
        def _fire_session_event():
            try:
                window.evaluate_js(
                    "window.dispatchEvent(new CustomEvent('gs-session-started'));"
                )
            except Exception as e:
                logger.debug("session js event: %s", e)

        ui_invoke.run(_fire_session_event)


def _enter_admin():
    global _mode
    if _mode == "admin":
        return
    _mode = "admin"
    if DEBUG:
        return

    logger.info("Режим: администратор")
    block_keyboard.set_mode(block_keyboard.MODE_OFF, hide_taskbar=False)
    policy_guard.set_mode(policy_guard.MODE_OFF)
    _show_admin_window()
    _start_explorer()


def _process_status(status, response_data):
    global _last_status

    if status == "админ":
        _enter_admin()
        _last_status = status
        return POLL_INTERVAL

    if status == "ремонт":
        _enter_admin()
        _maybe_launch_repair_tool()
        _last_status = status
        return POLL_INTERVAL

    if status == "занят":
        session_active = not _session_expired(response_data)
        if session_active:
            _enter_session()
            interval = POLL_INTERVAL_SESSION if _last_status == status else POLL_INTERVAL_BUSY
            _last_status = status
            return interval

        edit_status()
        _enter_waiting()
        _last_status = "активен"
        return POLL_INTERVAL_BUSY

    _enter_waiting()
    _last_status = status
    return POLL_INTERVAL


def _on_window_loaded():
    global _hwnd
    _hwnd = _find_hwnd()
    _app_ready.set()
    if not DEBUG:
        _enter_waiting()


def api_loop(_window=None):
    if not _app_ready.wait(30):
        logger.warning("WebView не загрузился вовремя, продолжаем опрос API")

    interval = POLL_INTERVAL

    while not _stop_polling.is_set():
        try:
            response = http.get(f"{API_BASE}/pc", timeout=API_TIMEOUT)
            response.raise_for_status()
            response_data = response.json()
            status = response_data.get("status")
            interval = _process_status(status, response_data)

        except requests.exceptions.RequestException as e:
            logger.warning("Ошибка сети: %s", e)
            interval = POLL_INTERVAL_ERROR
        except (KeyError, ValueError, TypeError) as e:
            logger.error("Неверный ответ API: %s", e)
            interval = POLL_INTERVAL_ERROR
        except Exception as e:
            logger.error("Ошибка опроса: %s", e, exc_info=True)
            interval = POLL_INTERVAL_ERROR

        _stop_polling.wait(interval)


def _check_updates_background():
    try:
        new_version = check_for_updates(VERSION)
        if new_version:
            download_and_install_update(new_version)
    except Exception as e:
        logger.error("Ошибка проверки обновлений: %s", e)


def start_app():
    global window

    try:
        logger.info("Инициализация WebView")
        window = webview.create_window(
            "GameSense",
            f"{SITE_BASE}/login_pc/{token}",
            fullscreen=True,
            confirm_close=False,
            background_color="#110e1a",
            js_api=ShellApi(),
        )
        ui_invoke.configure(lambda: window)
        window.events.loaded += _on_window_loaded
        window.events.closing += _on_closing
        threading.Thread(target=_check_updates_background, daemon=True, name="updates").start()
        webview.start(api_loop, window, debug=DEBUG)
    except Exception as e:
        logger.error("Ошибка инициализации WebView: %s", e, exc_info=True)
        sys.exit(1)


def exit_handler():
    logger.info("Приложение завершает работу")
    _stop_polling.set()
    block_keyboard.stop_block()
    policy_guard.stop()
    window_guard.release(_find_hwnd())
    http.close()


atexit.register(exit_handler)

if __name__ == "__main__":
    start_app()
