"""
Watchdog доступности сервисов (Discord / YouTube).

Пока обход включён, раз в ~минуту тихо проверяет, что подконтрольные сервисы реально
открываются (TLS + чтение тела через tester.check_site). Голос Discord живёт по UDP —
за ним следит VoiceMonitor; здесь мы стережём TCP/TLS-путь (сайт / приложение / CDN):
провайдер может задушить именно его (RST / таймаут / «заморозка» на 16-20 КБ), пока
голосовой UDP ещё жив. Если сервис устойчиво не проходит (fail_limit циклов подряд с
блок-подобным статусом), зовём on_degraded — тот же путь авто-восстановления, что и у
голоса (перезапуск текущей стратегии, затем переключение на следующую рабочую).

Стоит дёшево: один хост на сервис раз в интервал — доли процента CPU.
Идея подсмотрена у B4 (watchdog важных URL с авто-переподбором).
"""
from __future__ import annotations

import threading
from typing import Callable

from . import tester


class ServiceWatchdog:
    # Не давать pywebview рекурсивно обходить объект при построении js_api.
    _serializable = False

    def __init__(
        self,
        services_provider: Callable[[], list[str]],
        interval: float = 60.0,
        fail_limit: int = 2,
        timeout: float = 6.0,
        on_degraded: Callable[[str, str], None] | None = None,
    ) -> None:
        # services_provider — какие сервисы стеречь (список ключей tester.DEFAULT_TARGETS).
        self.services_provider = services_provider
        self.interval = interval
        self.fail_limit = fail_limit
        self.timeout = timeout
        self.on_degraded = on_degraded

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._fails: dict[str, int] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._fails.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._fails.clear()

    def _probe(self, service: str) -> tuple[bool, str]:
        """True + '' если сервис открывается; False + статус (RST/TIMEOUT/FREEZE/…) если нет.
        Берём один представительный хост сервиса — этого достаточно и дёшево."""
        targets = tester.DEFAULT_TARGETS.get(service) or []
        if not targets:
            return True, ""
        host, path = targets[0]
        r = tester.check_site(host, path, timeout=self.timeout)
        return r.ok, ("" if r.ok else (r.status or "FAIL"))

    def record(self, service: str, ok: bool) -> bool:
        """Учитывает результат одной проверки; True — сервис деградировал (набрал
        fail_limit неудач подряд). Вынесено отдельно для тестируемости без сети."""
        if ok:
            self._fails[service] = 0
            return False
        n = self._fails.get(service, 0) + 1
        self._fails[service] = n
        if n >= self.fail_limit:
            self._fails[service] = 0  # сброс, чтобы не спамить каждый цикл
            return True
        return False

    def _loop(self) -> None:
        # Первую проверку делаем не сразу — даём стратегии «прогреться» после включения.
        if self._stop.wait(self.interval):
            return
        while not self._stop.is_set():
            for svc in (self.services_provider() or []):
                if self._stop.is_set():
                    break
                ok, status = self._probe(svc)
                if self.record(svc, ok) and self.on_degraded:
                    try:
                        self.on_degraded(svc, status)
                    except Exception:
                        pass
                    break  # одно срабатывание восстановления за цикл — не душим все сразу
            self._stop.wait(self.interval)
