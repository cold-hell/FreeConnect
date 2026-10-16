# -*- coding: utf-8 -*-
"""Замер реального времени автоподбора: по каждой пробе отдельно и целиком.

Запускать при РАБОТАЮЩЕМ winws (стратегия пользователя применена) — так видно
настоящую цену кандидата в автоподборе, без прав администратора и без остановки
текущего обхода.
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freeconnect import tester  # noqa: E402


def timed(fn, n=3):
    ts = []
    last = None
    for _ in range(n):
        t0 = time.perf_counter()
        last = fn()
        ts.append(time.perf_counter() - t0)
    return ts, last


def main() -> None:
    print("=== по каждой цели (timeout=5, как в автоподборе) ===")
    total_max = 0.0
    for svc, tgts in tester.DEFAULT_TARGETS.items():
        for host, path in tgts:
            adv = (host, path) in tester.ADVISORY_TARGETS
            to = tester.ADVISORY_TIMEOUT if adv else 5.0
            ts, r = timed(lambda h=host, p=path, t=to: tester.check_site(h, p, timeout=t), 3)
            total_max = max(total_max, max(ts))
            print(f"  {svc:8} {host:28} avg={statistics.mean(ts):.2f}с max={max(ts):.2f}с "
                  f"status={r.status:8} advisory={adv}")
    print(f"  -> самый долгий одиночный зонд: {total_max:.2f}с "
          f"(он и определяет время кандидата, остальное параллельно)")

    print()
    print("=== голос и полный прогон ===")
    ts, r = timed(lambda: tester.check_voice(), 3)
    print(f"  check_voice: avg={statistics.mean(ts):.2f}с ok={r.voice_ok} detail={r.voice_detail}")
    ts, r = timed(lambda: tester.check_voice(quick=True), 3)
    print(f"  check_voice(quick): avg={statistics.mean(ts):.2f}с ok={r.voice_ok}")
    ts, res = timed(lambda: tester.test_all(['discord', 'youtube'], timeout=5.0), 3)
    print(f"  test_all: avg={statistics.mean(ts):.2f}с max={max(ts):.2f}с")

    print()
    print("=== имитация кандидата с заблокированными сайтами ===")
    from unittest import mock
    blocked = tester.SiteResult(host="x", ok=False, status="TIMEOUT")
    with mock.patch.object(tester, "check_site", return_value=blocked):
        ts, _ = timed(lambda: tester.test_all(['discord', 'youtube'], timeout=5.0), 2)
    print(f"  test_all при полной блокировке: avg={statistics.mean(ts):.2f}с")
    print("  (если это ~0.2с — кандидаты-мертвяки почти бесплатны)")

    print()
    print("=== цена служебных операций движка ===")
    from freeconnect import engine
    t0 = time.perf_counter()
    engine.kill_winws()
    print(f"  kill_winws(): {time.perf_counter() - t0:.2f}с (вызывается на каждый старт и стоп)")


if __name__ == "__main__":
    main()
