import os
import subprocess
import threading
import time

import browser_download_block
from logging_config import logger

MODE_OFF = "off"
MODE_WAITING = "waiting"
MODE_SESSION = "session"

_lock = threading.Lock()
_thread = None
_stop = threading.Event()
_mode = MODE_OFF

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


def _targets_for_mode(mode):
    targets = set()
    if mode in (MODE_WAITING, MODE_SESSION):
        targets |= _SHELL_TOOLS
    if mode == MODE_SESSION:
        for name in _list_process_names():
            if _is_installer(name):
                targets.add(name)
    elif mode == MODE_WAITING:
        targets |= _INSTALLERS
    return targets


def _enforce_processes(mode):
    if mode == MODE_OFF:
        return

    running = _list_process_names()
    targets = _targets_for_mode(mode)

    for name in targets:
        if name in _ALWAYS_ALLOW or name in _LAUNCHER_ALLOW:
            continue
        if name in running:
            _kill_process(name)


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
    if mode in (MODE_WAITING, MODE_SESSION):
        browser_download_block.enable()
    else:
        browser_download_block.disable()


def _loop():
    while not _stop.is_set():
        with _lock:
            mode = _mode
        if mode != MODE_OFF:
            try:
                _enforce_processes(mode)
                _purge_downloads(mode)
            except Exception as e:
                logger.error("PolicyGuard: %s", e)
        _stop.wait(1.5)


def set_mode(mode):
    global _mode, _thread
    if mode not in (MODE_OFF, MODE_WAITING, MODE_SESSION):
        mode = MODE_OFF

    with _lock:
        _mode = mode

    if mode == MODE_OFF:
        stop()
        return

    _apply_download_policies(mode)

    with _lock:
        if _thread and _thread.is_alive():
            logger.debug("PolicyGuard: режим %s", mode)
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="policy-guard")
        _thread.start()
        logger.info("PolicyGuard: режим %s (браузеры открыты, загрузки заблокированы)", mode)


def stop():
    global _thread, _mode
    _stop.set()
    browser_download_block.disable()
    with _lock:
        if _thread and _thread.is_alive():
            _thread.join(timeout=2.0)
        _thread = None
        _mode = MODE_OFF
    _stop.clear()
