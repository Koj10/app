import os
import subprocess
import threading
import winreg

import win32con
import win32gui

import browser_download_block
from logging_config import logger

MODE_OFF = "off"
MODE_WAITING = "waiting"
MODE_SESSION = "session"

_INTERVALS = {
    MODE_WAITING: 4.0,
    MODE_SESSION: 8.0,
}

_lock = threading.Lock()
_thread = None
_stop = threading.Event()
_wake = threading.Event()
_mode = MODE_OFF
_download_policy_mode = None
_shell_locked = False
_purge_counter = 0

# Окна файлового проводника и диспетчера задач. Рабочий стол (Progman) не трогаем.
_BLOCKED_WINDOW_CLASSES = frozenset(
    {
        "CabinetWClass",
        "ExploreWClass",
        "TaskManagerWindow",
    }
)

_TASKMGR_POLICY = r"Software\Microsoft\Windows\CurrentVersion\Policies\System"

_ALWAYS_ALLOW = frozenset(
    {
        "gamesense.exe",
        "msedgewebview2.exe",
        "python.exe",
        "pythonw.exe",
    }
)

_LAUNCHER_ALLOW = frozenset(
    {
        "steam.exe",
        "steamservice.exe",
        "steamwebhelper.exe",
        "epicgameslauncher.exe",
        "battle.net.exe",
        "agent.exe",
        "riotclientservices.exe",
        "riot client.exe",
        "origin.exe",
        "eadesktop.exe",
        "goggalaxy.exe",
        "ubisoftconnect.exe",
        "upc.exe",
    }
)

# Проводник, cmd, taskmgr — по-прежнему блокируем; браузеры — разрешены
_SHELL_TOOLS = frozenset(
    {
        "explorer.exe",
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "wscript.exe",
        "cscript.exe",
        "mshta.exe",
        "taskmgr.exe",
        "regedit.exe",
    }
)

_INSTALLERS = frozenset(
    {
        "msiexec.exe",
    }
)

_BLOCKED_DOWNLOAD_EXT = frozenset(
    {
        ".exe",
        ".msi",
        ".msix",
        ".bat",
        ".cmd",
        ".ps1",
        ".scr",
        ".zip",
        ".rar",
        ".7z",
        ".iso",
        ".apk",
        ".dmg",
        ".crdownload",
        ".part",
        ".download",
    }
)

_WATCH_DIRS = []


def _init_watch_dirs():
    global _WATCH_DIRS
    if _WATCH_DIRS:
        return
    home = os.path.expanduser("~")
    _WATCH_DIRS = [
        os.path.join(home, "Downloads"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
        os.path.join(os.environ.get("TEMP", home), ""),
        os.path.join(home, "AppData", "Local", "Temp"),
    ]


def _list_process_names():
    names = set()
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=8,
        )
        for line in (result.stdout or "").splitlines():
            if not line.strip():
                continue
            part = line.split(",", 1)[0].strip().strip('"')
            if part:
                names.add(part.lower())
    except Exception as e:
        logger.debug("tasklist: %s", e)
    return names


def _kill_process(name):
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", name, "/T"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5,
        )
        logger.info("PolicyGuard: завершён %s", name)
    except Exception as e:
        logger.debug("taskkill %s: %s", name, e)


def _is_installer(name):
    lower = name.lower()
    if lower in _INSTALLERS:
        return True
    if "setup" in lower or "installer" in lower:
        return lower not in _LAUNCHER_ALLOW and lower not in _ALWAYS_ALLOW
    return False


def _targets_for_mode(mode, running):
    targets = set()
    if mode == MODE_WAITING:
        targets |= _SHELL_TOOLS | _INSTALLERS
    elif mode == MODE_SESSION:
        targets |= _SHELL_TOOLS - {"explorer.exe"}
        for name in running:
            if _is_installer(name):
                targets.add(name)
    return targets


def _enforce_processes(mode):
    running = _list_process_names()
    with _lock:
        mode = _mode
    if mode == MODE_OFF:
        return

    targets = _targets_for_mode(mode, running)

    for name in targets:
        if name in _ALWAYS_ALLOW or name in _LAUNCHER_ALLOW:
            continue
        if name in running:
            _kill_process(name)


def _set_dword(root, path, name, value):
    try:
        key = winreg.CreateKeyEx(root, path, 0, winreg.KEY_SET_VALUE | winreg.KEY_CREATE_SUB_KEY)
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
        winreg.CloseKey(key)
        return True
    except OSError as e:
        logger.debug("policy %s: %s", name, e)
        return False


def _delete_value(root, path, name):
    try:
        key = winreg.OpenKeyEx(root, path, 0, winreg.KEY_SET_VALUE)
    except FileNotFoundError:
        return True
    except OSError as e:
        logger.debug("policy open %s: %s", name, e)
        return False
    try:
        winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass
    finally:
        winreg.CloseKey(key)
    return True


def _broadcast_policy():
    try:
        win32gui.SendMessageTimeout(
            win32con.HWND_BROADCAST,
            win32con.WM_SETTINGCHANGE,
            0,
            "Policy",
            win32con.SMTO_ABORTIFHUNG,
            500,
        )
    except Exception as e:
        logger.debug("policy broadcast: %s", e)


def _apply_shell_policies(lock_down):
    """DisableTaskMgr: диспетчер задач не запускается, в том числе с Ctrl+Alt+Del."""
    global _shell_locked
    if lock_down == _shell_locked:
        return

    if lock_down:
        _set_dword(winreg.HKEY_CURRENT_USER, _TASKMGR_POLICY, "DisableTaskMgr", 1)
        logger.info("PolicyGuard: диспетчер задач отключён")
    else:
        _delete_value(winreg.HKEY_CURRENT_USER, _TASKMGR_POLICY, "DisableTaskMgr")
        logger.info("PolicyGuard: диспетчер задач снова доступен")

    _broadcast_policy()
    _shell_locked = lock_down


def _close_blocked_windows():
    """Закрыть окна проводника. Процесс explorer.exe остаётся — это рабочий стол и панель задач."""

    def _enum(hwnd, _):
        try:
            class_name = win32gui.GetClassName(hwnd)
        except Exception:
            return True
        if class_name not in _BLOCKED_WINDOW_CLASSES:
            return True
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception as e:
            logger.debug("close window %s: %s", class_name, e)
        return True

    try:
        win32gui.EnumWindows(_enum, None)
    except Exception as e:
        logger.debug("EnumWindows: %s", e)


def _purge_downloads(mode):
    if mode not in (MODE_WAITING, MODE_SESSION):
        return

    _init_watch_dirs()
    for folder in _WATCH_DIRS:
        if not folder or not os.path.isdir(folder):
            continue
        try:
            for entry in os.scandir(folder):
                if not entry.is_file():
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext not in _BLOCKED_DOWNLOAD_EXT:
                    continue
                try:
                    os.remove(entry.path)
                    logger.info("PolicyGuard: удалён загрузка %s", entry.path)
                except OSError as e:
                    logger.debug("purge %s: %s", entry.path, e)
        except OSError as e:
            logger.debug("scan %s: %s", folder, e)


def _apply_download_policies(mode):
    global _download_policy_mode

    want = mode if mode in (MODE_WAITING, MODE_SESSION) else MODE_OFF
    if want == _download_policy_mode:
        return

    if want != MODE_OFF:
        browser_download_block.enable()
    else:
        browser_download_block.disable()
    _download_policy_mode = want


def _loop():
    global _purge_counter

    scan_ticks = 0
    while not _stop.is_set():
        with _lock:
            mode = _mode

        if mode == MODE_SESSION:
            try:
                _close_blocked_windows()
            except Exception as e:
                logger.debug("close windows: %s", e)
            interval = 0.15
            scan_ticks += 1
            do_scan = scan_ticks >= 27
        elif mode == MODE_WAITING:
            interval = _INTERVALS[MODE_WAITING]
            do_scan = True
        else:
            interval = 1.0
            do_scan = False

        if do_scan and mode != MODE_OFF:
            scan_ticks = 0
            try:
                _enforce_processes(mode)
                _purge_counter += 1
                if _purge_counter >= 3:
                    _purge_downloads(mode)
                    _purge_counter = 0
            except Exception as e:
                logger.error("PolicyGuard: %s", e)

        _wake.wait(interval)
        _wake.clear()


def set_mode(mode):
    global _mode, _thread, _purge_counter
    if mode not in (MODE_OFF, MODE_WAITING, MODE_SESSION):
        mode = MODE_OFF

    with _lock:
        _mode = mode

    if mode == MODE_OFF:
        stop()
        return

    _apply_download_policies(mode)
    _apply_shell_policies(True)
    _purge_counter = 0
    _wake.set()

    with _lock:
        if _thread and _thread.is_alive():
            logger.debug("PolicyGuard: режим %s", mode)
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="policy-guard")
        _thread.start()
        logger.info("PolicyGuard: режим %s (браузеры открыты, загрузки заблокированы)", mode)


def stop():
    global _thread, _mode, _download_policy_mode, _purge_counter
    _stop.set()
    _wake.set()
    _apply_shell_policies(False)
    browser_download_block.disable()
    with _lock:
        if _thread and _thread.is_alive():
            _thread.join(timeout=2.0)
        _thread = None
        _mode = MODE_OFF
        _download_policy_mode = None
        _purge_counter = 0
    _stop.clear()
    _wake.clear()
