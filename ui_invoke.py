"""Выполнение UI-операций в потоке WinForms (pywebview)."""

from logging_config import logger

_window_getter = None
_posted = []


def configure(window_getter):
    global _window_getter
    _window_getter = window_getter


def _view():
    window = _window_getter() if _window_getter else None
    if not window:
        return None
    from webview.platforms import winforms

    return winforms.BrowserView.instances.get(window.uid)


def run(action):
    """Синхронно выполнить action в UI-потоке.

    Нельзя вызывать из action window.evaluate_js / window.minimize:
    они сами ждут UI-поток и встают в deadlock.
    """
    try:
        from System import Func, Type

        view = _view()
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


def post(action):
    """Поставить action в очередь UI-потока и сразу вернуться."""
    try:
        from System import Action

        view = _view()
        if not view:
            action()
            return
        if not view.InvokeRequired:
            action()
            return

        def _wrapped():
            try:
                action()
            except Exception as e:
                logger.error("ui_invoke.post: %s", e)
            finally:
                try:
                    _posted.remove(delegate)
                except ValueError:
                    pass

        delegate = Action(_wrapped)
        _posted.append(delegate)
        view.BeginInvoke(delegate)
    except Exception as e:
        logger.debug("ui_invoke.post: %s", e)
        try:
            action()
        except Exception as e2:
            logger.error("ui_invoke.post: %s", e2)
