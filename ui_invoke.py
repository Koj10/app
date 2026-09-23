"""Выполнение UI-операций в потоке WinForms (pywebview)."""

from logging_config import logger

_window_getter = None


def configure(window_getter):
    global _window_getter
    _window_getter = window_getter


def run(action):
    window = _window_getter() if _window_getter else None
    if not window:
        return

    try:
        from System import Func, Type
        from webview.platforms import winforms

        view = winforms.BrowserView.instances.get(window.uid)
        if not view:
            action()
            return
        if view.InvokeRequired:
            view.Invoke(Func[Type](action))
        else:
            action()
    except Exception as e:
        logger.debug("ui_invoke: %s", e)
        try:
            action()
        except Exception as e2:
            logger.error("ui_invoke: %s", e2)
