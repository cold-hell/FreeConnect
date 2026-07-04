"""
Монитор голосового канала Discord.

Раз в несколько секунд измеряет UDP-задержку (STUN RTT) — это дёшево (один пакет,
доли процента CPU) и служит индикатором здоровья голосового пути. При устойчивом
скачке пинга или потере пакетов сообщает наверх (для индикатора и авто-восстановления).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

from . import tester


class VoiceMonitor:
    # Не давать pywebview рекурсивно обходить монитор при построении js_api.
    _serializable = False

    def __init__(
        self,
        interval: float = 3.0,
        spike_factor: float = 2.5,
        spike_abs_ms: float = 200.0,
        loss_limit: int = 3,
        on_update: Callable[[float | None], None] | None = None,
        on_spike: Callable[[], None] | None = None,
    ) -> None:
        self.interval = interval
        self.spike_factor = spike_factor
        self.spike_abs_ms = spike_abs_ms
        self.loss_limit = loss_limit
        self.on_update = on_update
        self.on_spike = on_spike

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._recent: deque[float] = deque(maxlen=20)
        self._consecutive_loss = 0
        self.last_rtt: float | None = None

    @property
    def baseline_ms(self) -> float | None:
        """Медиана недавних измерений как опорный уровень."""
        if len(self._recent) < 4:
            return None
        s = sorted(self._recent)
        return s[len(s) // 2]

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._recent.clear()
        self._consecutive_loss = 0

    def _loop(self) -> None:
        while not self._stop.is_set():
            rtt = tester.stun_rtt(timeout=1.5)
            self.last_rtt = rtt
            if self.on_update:
                try:
                    self.on_update(rtt)
                except Exception:
                    pass

            if rtt is None:
                self._consecutive_loss += 1
                if self._consecutive_loss >= self.loss_limit:
                    self._fire_spike()
            else:
                self._consecutive_loss = 0
                base = self.baseline_ms
                spiked = (
                    base is not None
                    and rtt >= self.spike_abs_ms
                    and rtt >= base * self.spike_factor
                )
                # Опорный уровень копим ДО добавления текущего, чтобы всплеск не влил себя в медиану.
                self._recent.append(rtt)
                if spiked:
                    self._fire_spike()

            self._stop.wait(self.interval)

    def _fire_spike(self) -> None:
        # После срабатывания сбрасываем историю, чтобы не спамить повторно.
        self._recent.clear()
        self._consecutive_loss = 0
        if self.on_spike:
            try:
                self.on_spike()
            except Exception:
                pass
