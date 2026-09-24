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
_win_pressed = threading.Event()
_mode = MODE_OFF
_download_policy_mode = None
_shell_policy_mode = None
_purge_counter = 0
_taskmgr_kill = None
_suspend_lock = threading.Lock()
_suspended_start = set()
_start_held_logged = False

# Меню Пуск Windows 11. В ожидании процесс замораживается, в сессии снова работает.
_START_HOSTS = frozenset(
    {
        "startmenuexperiencehost.exe",
        "searchhost.exe",
    }
)
_PROCESS_TERMINATE = 0x0001
_PROCESS_SUSPEND_RESUME = 0x0800

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

# Параметры и старая панель управления. Только в игровой сессии.
_SETTINGS_PROCESSES = frozenset(
    {
        "systemsettings.exe",
        "systemsettingsbroker.exe",
        "control.exe",
    }
)
_SETTINGS_TITLES = frozenset(
    {
        "параметры",
        "settings",
        "панель управления",
        "control panel",
    }
)
_RECYCLE_BIN_CLSID = "{645FF040-5081-101B-9F08-00AA002F954E}"
_HIDE_DESKTOP_ICONS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons"

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
        targets |= (_SHELL_TOOLS - {"explorer.exe"}) | _SETTINGS_PROCESSES
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


def _kill_taskmgr(force_image=False):
    """Закрыть диспетчер задач по окну и по имени процесса. WM_CLOSE он часто игнорирует."""
    _prepare_native()
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    found = False

    def _terminate(pid):
        if not pid:
            return
        handle = kernel32.OpenProcess(0x0001, False, pid)
        if not handle:
            return
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)

    def _enum(hwnd, _):
        nonlocal found
        try:
            title = win32gui.GetWindowText(hwnd) or ""
            class_name = win32gui.GetClassName(hwnd)
        except Exception:
            return True
        if class_name != "TaskManagerWindow" and title not in ("Диспетчер задач", "Task Manager"):
            return True
        found = True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        _terminate(pid.value)
        return True

    try:
        win32gui.EnumWindows(_enum, None)
    except Exception as e:
        logger.debug("taskmgr enum: %s", e)

    if not found and not force_image:
        return

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


def _set_sz(root, path, name, value):
    access = winreg.KEY_SET_VALUE | winreg.KEY_CREATE_SUB_KEY | winreg.KEY_WOW64_64KEY
    try:
        key = winreg.CreateKeyEx(root, path, 0, access)
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.CloseKey(key)
        return True
    except OSError as e:
        logger.debug("policy %s: %s", name, e)
        return False


def _set_dword(root, path, name, value):
    access = winreg.KEY_SET_VALUE | winreg.KEY_CREATE_SUB_KEY | winreg.KEY_WOW64_64KEY
    try:
        key = winreg.CreateKeyEx(root, path, 0, access)
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
        winreg.CloseKey(key)
        return True
    except OSError as e:
        logger.debug("policy %s: %s", name, e)
        return False


def _delete_value(root, path, name):
    try:
        key = winreg.OpenKeyEx(
            root, path, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
        )
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


def _refresh_desktop():
    try:
        shell32 = ctypes.windll.shell32
        shell32.SHChangeNotify.argtypes = [
            ctypes.c_long,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        shell32.SHChangeNotify.restype = None
        shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception as e:
        logger.debug("desktop notify: %s", e)
    try:
        progman = win32gui.FindWindow("Progman", None)
        if progman:
            win32gui.PostMessage(progman, win32con.WM_COMMAND, 0x7402, 0)
    except Exception as e:
        logger.debug("desktop refresh: %s", e)


def _set_recycle_bin_hidden(hidden):
    for folder in ("NewStartPanel", "ClassicStartMenu"):
        path = _HIDE_DESKTOP_ICONS + "\\" + folder
        if hidden:
            _set_dword(winreg.HKEY_CURRENT_USER, path, _RECYCLE_BIN_CLSID, 1)
        else:
            _delete_value(winreg.HKEY_CURRENT_USER, path, _RECYCLE_BIN_CLSID)


def _desktop_dirs():
    home = os.path.expanduser("~")
    public = os.environ.get("PUBLIC") or r"C:\Users\Public"
    candidates = [
        os.path.join(home, "Desktop"),
        os.path.join(public, "Desktop"),
    ]
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            desktop, _ = winreg.QueryValueEx(key, "Desktop")
            if desktop:
                candidates.append(desktop)
    except OSError:
        pass
    found = []
    for path in candidates:
        if path and os.path.isdir(path) and path not in found:
            found.append(path)
    return found


def _icacls(args):
    try:
        subprocess.run(
            ["icacls", *args],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=20,
        )
    except Exception as e:
        logger.debug("icacls: %s", e)


def _lock_desktop_delete(lock):
    """Запрет удаления файлов на рабочем столе. Меню Windows 11 политику обходит, права — нет."""
    user = os.environ.get("USERNAME")
    if not user:
        logger.warning("Не удалось закрыть удаление ярлыков: нет имени пользователя")
        return
    for folder in _desktop_dirs():
        if lock:
            _icacls([folder, "/deny", f"{user}:(OI)(CI)(DE,DC)", "/C", "/Q"])
            _icacls([os.path.join(folder, "*"), "/deny", f"{user}:(DE,DC)", "/C", "/Q"])
        else:
            _icacls([folder, "/remove:d", user, "/T", "/C", "/Q"])
    logger.info(
        "PolicyGuard: удаление с рабочего стола %s",
        "запрещено" if lock else "разрешено",
    )


def _apply_session_user_lock(enabled):
    """В сессии нельзя удалять ярлыки с рабочего стола и открывать Параметры."""
    if enabled:
        _set_dword(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoControlPanel", 1)
        _set_dword(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoViewContextMenu", 1)
        if not _set_sz(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "SettingsPageVisibility", "hide:*"):
            logger.warning("Не удалось закрыть параметры через реестр")
        _set_recycle_bin_hidden(True)
        _lock_desktop_delete(True)
    else:
        _delete_value(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoControlPanel")
        _delete_value(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoViewContextMenu")
        _delete_value(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "SettingsPageVisibility")
        _set_recycle_bin_hidden(False)
        _lock_desktop_delete(False)
    _refresh_desktop()


def _pids_named(names):
    _prepare_native()
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid:
        return set()
    found = set()
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return set()
        while True:
            if entry.szExeFile.lower() in names and entry.th32ProcessID:
                found.add(entry.th32ProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def _close_settings():
    """Параметры на Windows 11 открываются отдельным процессом, заголовок окна часто пустой."""
    _prepare_native()
    for pid in _pids_named(_SETTINGS_PROCESSES):
        _terminate_pid(pid)

    user32 = ctypes.windll.user32

    def _enum(hwnd, _):
        try:
            title = (win32gui.GetWindowText(hwnd) or "").strip().casefold()
        except Exception:
            return True
        if title not in _SETTINGS_TITLES:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        _terminate_pid(pid.value)
        return True

    try:
        win32gui.EnumWindows(_enum, None)
    except Exception as e:
        logger.debug("settings enum: %s", e)


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
        _set_dword(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoTrayContextMenu", 1)
    else:
        _delete_value(winreg.HKEY_CURRENT_USER, _TASKMGR_POLICY, "DisableTaskMgr")
        _delete_value(winreg.HKEY_LOCAL_MACHINE, _TASKMGR_POLICY, "DisableTaskMgr")
        _delete_value(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoWinKeys")
        _delete_value(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoTrayContextMenu")

    if mode == MODE_SESSION:
        drives_hidden = _set_dword(
            winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoDrives", _ALL_DRIVES
        ) and _set_dword(
            winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoViewOnDrive", _ALL_DRIVES
        )
        if not drives_hidden:
            logger.warning("Не удалось закрыть диски в проводнике через реестр")
        _apply_session_user_lock(True)
        logger.info("PolicyGuard: диски закрыты, удаление и параметры отключены")
    else:
        _delete_value(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoDrives")
        _delete_value(winreg.HKEY_CURRENT_USER, _EXPLORER_POLICY, "NoViewOnDrive")
        _apply_session_user_lock(False)
        if lock:
            logger.info("PolicyGuard: Win и диспетчер задач отключены")
        elif _shell_policy_mode is not None:
            logger.info("PolicyGuard: ограничения оболочки сняты")

    _broadcast_policy()
    _shell_policy_mode = mode


_native_ready = False


def _prepare_native():
    global _native_ready
    if _native_ready:
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    user32.keybd_event.restype = None
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    ntdll = ctypes.windll.ntdll
    ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtSuspendProcess.restype = ctypes.c_long
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    _native_ready = True


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _start_host_pids():
    _prepare_native()
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid:
        return set()
    found = set()
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return set()
        while True:
            if entry.szExeFile.lower() in _START_HOSTS and entry.th32ProcessID:
                found.add(entry.th32ProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def _visible_pids(pids):
    if not pids:
        return set()
    _prepare_native()
    user32 = ctypes.windll.user32
    found = set()

    def _enum(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
        except Exception:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        if pid.value in pids:
            found.add(pid.value)
            try:
                user32.ShowWindowAsync(int(hwnd), win32con.SW_HIDE)
            except Exception as e:
                logger.debug("hide start: %s", e)
        return True

    try:
        win32gui.EnumWindows(_enum, None)
    except Exception as e:
        logger.debug("start enum: %s", e)
    return found


def _open_process(pid, access):
    handle = ctypes.windll.kernel32.OpenProcess(access, False, pid)
    return handle or None


def _terminate_pid(pid):
    handle = _open_process(pid, _PROCESS_TERMINATE)
    if not handle:
        return False
    kernel32 = ctypes.windll.kernel32
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _suspend_pid(pid):
    handle = _open_process(pid, _PROCESS_SUSPEND_RESUME)
    if not handle:
        return False
    kernel32 = ctypes.windll.kernel32
    try:
        return ctypes.windll.ntdll.NtSuspendProcess(handle) == 0
    finally:
        kernel32.CloseHandle(handle)


def _resume_pid(pid):
    handle = _open_process(pid, _PROCESS_SUSPEND_RESUME)
    if not handle:
        return
    kernel32 = ctypes.windll.kernel32
    ntdll = ctypes.windll.ntdll
    try:
        for _ in range(8):
            ntdll.NtResumeProcess(handle)
    finally:
        kernel32.CloseHandle(handle)


def _cancel_start_gesture():
    """VK_E8 — системная клавиша, которой оболочка отменяет открытие Пуска."""
    _prepare_native()
    user32 = ctypes.windll.user32
    user32.keybd_event(0xE8, 0, 0, 0)
    user32.keybd_event(0xE8, 0, 0x0002, 0)


def _hold_start_menu():
    """В ожидании Пуск не рисуется: хост заморожен, уже открытое окно закрывается."""
    global _start_held_logged

    pids = _start_host_pids()
    with _suspend_lock:
        with _lock:
            if _mode != MODE_WAITING:
                return
        visible = _visible_pids(pids)
        changed = False
        for pid in pids:
            if pid in visible:
                if _terminate_pid(pid):
                    changed = True
                _suspended_start.discard(pid)
            elif pid not in _suspended_start and _suspend_pid(pid):
                _suspended_start.add(pid)
                changed = True
        if changed and not _start_held_logged:
            logger.info("PolicyGuard: меню Пуск заблокировано")
            _start_held_logged = True


def _release_start_menu():
    """Снимает заморозку Пуска, когда пакет активирован или включён режим админа."""
    global _start_held_logged

    pids = _start_host_pids()
    with _suspend_lock:
        had_block = bool(_suspended_start)
        for pid in set(_suspended_start) | set(pids):
            _resume_pid(pid)
        _suspended_start.clear()
        _start_held_logged = False
    if had_block:
        logger.info("PolicyGuard: меню Пуск снова доступно")


def release_start_menu():
    _release_start_menu()


def _on_win_key():
    _win_pressed.set()
    _wake.set()


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
    _prepare_native()
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
        fg_thread = user32.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
        shell_pid = wintypes.DWORD()
        shell_thread = user32.GetWindowThreadProcessId(shell, ctypes.byref(shell_pid))
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
            if _win_pressed.is_set():
                _win_pressed.clear()
                _cancel_start_gesture()
            _hold_start_menu()
            _close_window_classes(
                _FILE_WINDOW_CLASSES | _TASKMGR_WINDOW_CLASSES | _SWITCHER_WINDOW_CLASSES
            )
            _refocus_shell()
        elif mode == MODE_SESSION:
            _close_window_classes(_FILE_WINDOW_CLASSES | _TASKMGR_WINDOW_CLASSES)
            _close_settings()

        if mode in (MODE_WAITING, MODE_SESSION):
            _kill_taskmgr(force_image=(ticks % 10 == 0))

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
        if mode == MODE_WAITING:
            interval = 0.05
        elif mode == MODE_SESSION:
            interval = 0.2
        else:
            interval = 1.0
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

    if mode == MODE_WAITING:
        _hold_start_menu()
    else:
        _release_start_menu()

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
    with _lock:
        _mode = MODE_OFF
    _stop.set()
    _wake.set()
    _apply_shell_policies(MODE_OFF)
    browser_download_block.disable()
    thread = None
    with _lock:
        thread = _thread
        _thread = None
        _download_policy_mode = None
        _purge_counter = 0
    if thread and thread.is_alive():
        thread.join(timeout=2.0)
    _release_start_menu()
    _stop.clear()
    _wake.clear()


def _install_win_callback():
    import block_keyboard

    block_keyboard.set_on_win_key(_on_win_key)


_install_win_callback()
