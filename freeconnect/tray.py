"""
Иконка в системном трее.

Позволяет программе жить в фоне: окно закрывается «в трей», а не выходит;
при автозапуске программа стартует сразу в трее и фоном подключается.
"""
from __future__ import annotations

import threading
from typing import Callable

import pystray
from PIL import Image, ImageDraw


def _make_icon(active: bool = False) -> Image.Image:
    """Рисует иконку-молнию (бирюзовую в фоне, ярче — когда обход включён)."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Молния — та же форма, что и в приложении (Material bolt), масштаб ×2.67.
    pts = [(19, 5), (19, 35), (27, 35), (27, 59), (45, 27), (35, 27), (45, 5)]
    fill = (120, 240, 255, 255) if active else (55, 224, 196, 255)
    d.polygon(pts, fill=fill)
    return img


class Tray:
    # Не давать pywebview рекурсивно обходить трей при построении js_api.
    _serializable = False

    def __init__(
        self,
        on_show: Callable[[], None],
        on_toggle: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self.on_show = on_show
        self.on_toggle = on_toggle
        self.on_quit = on_quit
        self.icon: pystray.Icon | None = None
        self._thread: threading.Thread | None = None

    def _menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Открыть FreeConnect", lambda: self.on_show(), default=True),
            pystray.MenuItem("Включить / выключить обход", lambda: self.on_toggle()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda: self.on_quit()),
        )

    def start(self) -> None:
        self.icon = pystray.Icon("FreeConnect", _make_icon(False), "FreeConnect", self._menu())
        self._thread = threading.Thread(target=self.icon.run, daemon=True)
        self._thread.start()

    def set_active(self, active: bool) -> None:
        if self.icon:
            try:
                self.icon.icon = _make_icon(active)
                self.icon.title = "FreeConnect — обход включён" if active else "FreeConnect"
            except Exception:
                pass

    def stop(self) -> None:
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
