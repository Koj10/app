import ctypes
import os
import subprocess
import threading
import winreg
from ctypes import wintypes

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
_shell_policy_mode = None
_purge_counter = 0
_taskmgr_kill = None

# Все буквы дисков A–Z.
_ALL_DRIVES = 0x03FFFFFF

_EXPLORER_POLICY = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"

# Окна файлового проводника и диспетчера задач. Рабочий стол (Progman) не трогаем.
_FILE_WINDOW_CLASSES = frozenset(
    {
        "CabinetWClass",
        "ExploreWClass",
    }
)
_TASKMGR_WINDOW_CLASSES = frozenset({"TaskManagerWindow"})
# Переключатель задач. Только в ожидании: в сессии Alt+Tab нужен для игр.
_SWITCHER_WINDOW_CLASSES = frozenset(
    {
        "MultitaskingViewFrame",
        "XamlExplorerHostIslandWindow",
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
    folders = [
        os.path.join(home, "Downloads"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
        os.path.join(os.environ.get("TEMP", home), ""),
        os.path.join(home, "AppData", "Local", "Temp"),
    ]
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            downloads, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
            if downloads:
                folders.append(downloads)
    except OSError:
        pass
    _WATCH_DIRS = folders


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


def _kill_taskmgr():
    """Диспетчер задач гасится сразу, не раз в несколько секунд."""
    global _taskmgr_kill
    if _taskmgr_kill is not None and _taskmgr_kill.poll() is None:
        return
    try:
        _taskmgr_kill = subprocess.Popen(
            ["taskkill", "/F", "/IM", "taskmgr.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        logger.debug("taskkill taskmgr: %s", e)


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


def _apply_shell_policies(mode):
    """Win, диспетчер задач и диски. Диски только в сессии: в ожидании проводника нет."""
    global _shell_policy_mode
    if mode == _shell_policy_mode:
        return

    lock = mode in (MODE_WAITING, MODE_SESSION)
    if lock:
        if not _set_dword(winreg.HKEY_CURRENT_USER, _TASKMGR_POLICY, "DisableTaskMgr", 1):
            logger.warning("Не удалось отключить диспетчер задач через реестр")
        _set_dword(winreg.HKEY_LOCAL_MACHINE, _TASKMGR_POLICY, "DisableTaskMgr", 1)
        if not _set_dword(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoWinKeys", 1):
            logger.warning("Не удалось отключить клавишу Win через реестр")
    else:
        _delete_value(winreg.HKEY_CURRENT_USER, _TASKMGR_POLICY, "DisableTaskMgr")
        _delete_value(winreg.HKEY_LOCAL_MACHINE, _TASKMGR_POLICY, "DisableTaskMgr")
        _delete_value(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoWinKeys")

    if mode == MODE_SESSION:
        drives_hidden = _set_dword(
            winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoDrives", _ALL_DRIVES
        ) and _set_dword(
            winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoViewOnDrive", _ALL_DRIVES
        )
        if not drives_hidden:
            logger.warning("Не удалось закрыть диски в проводнике через реестр")
        logger.info("PolicyGuard: диски в проводнике закрыты, диспетчер задач отключён")
    else:
        _delete_value(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoDrives")
        _delete_value(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoViewOnDrive")
        if lock:
            logger.info("PolicyGuard: Win и диспетчер задач отключены")
        elif _shell_policy_mode is not None:
            logger.info("PolicyGuard: ограничения оболочки сняты")

    _broadcast_policy()
    _shell_policy_mode = mode


def _close_window_classes(classes):
    """Только PostMessage: ShowWindow из этого потока может зависнуть и остановить всю защиту."""

    def _enum(hwnd, _):
        try:
            class_name = win32gui.GetClassName(hwnd)
        except Exception:
            return True
        if class_name not in classes or not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception as e:
            logger.debug("close %s: %s", class_name, e)
        return True

    try:
        win32gui.EnumWindows(_enum, None)
    except Exception as e:
        logger.debug("EnumWindows: %s", e)


def _refocus_shell():
    user32 = ctypes.windll.user32
    shell = win32gui.FindWindow(None, "GameSense")
    if not shell:
        return
    foreground = win32gui.GetForegroundWindow()
    if not foreground or foreground == shell:
        return

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
    if pid.value == os.getpid():
        return

    try:
        fg_thread = user32.GetWindowThreadProcessId(foreground, None)
        shell_thread = user32.GetWindowThreadProcessId(shell, None)
        current = ctypes.windll.kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(current, fg_thread, True)
        user32.AttachThreadInput(current, shell_thread, True)
        win32gui.SetForegroundWindow(shell)
        win32gui.SetWindowPos(
            shell,
            win32con.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
        )
        user32.AttachThreadInput(current, fg_thread, False)
        user32.AttachThreadInput(current, shell_thread, False)
    except Exception as e:
        logger.debug("refocus: %s", e)


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

    ticks = 0
    while not _stop.is_set():
        with _lock:
            mode = _mode

        if mode == MODE_WAITING:
            _close_window_classes(
                _FILE_WINDOW_CLASSES | _TASKMGR_WINDOW_CLASSES | _SWITCHER_WINDOW_CLASSES
            )
            _refocus_shell()
        elif mode == MODE_SESSION:
            _close_window_classes(_FILE_WINDOW_CLASSES | _TASKMGR_WINDOW_CLASSES)

        if mode in (MODE_WAITING, MODE_SESSION) and ticks % 3 == 0:
            _kill_taskmgr()

        do_scan = mode == MODE_WAITING and ticks % 5 == 0
        do_scan = do_scan or (mode == MODE_SESSION and ticks % 10 == 0)
        do_purge = mode in (MODE_WAITING, MODE_SESSION) and ticks % 5 == 0

        if do_scan:
            try:
                _enforce_processes(mode)
            except Exception as e:
                logger.error("PolicyGuard: %s", e)
        if do_purge:
            try:
                _purge_downloads(mode)
            except Exception as e:
                logger.error("PolicyGuard purge: %s", e)

        ticks += 1
        interval = 0.2 if mode in (MODE_WAITING, MODE_SESSION) else 1.0
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
    _apply_shell_policies(mode)
    if mode == MODE_SESSION:
        browser_download_block.close_browsers()
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
    _apply_shell_policies(MODE_OFF)
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
