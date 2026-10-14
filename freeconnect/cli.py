"""
Консольный интерфейс FreeConnect (для отладки ядра до появления GUI).

Команды:
  python -m freeconnect.cli list                 — список стратегий
  python -m freeconnect.cli test                 — проверить доступность БЕЗ обхода
  python -m freeconnect.cli stun                 — замер UDP-пинга (STUN)
  python -m freeconnect.cli start <id>           — включить стратегию и держать
  python -m freeconnect.cli search [--first]     — автоподбор (нужен админ)
"""
from __future__ import annotations

import sys
import time

from . import paths, tester
from .autosearch import search
from .engine import Engine, EngineError, is_admin
from .strategies import get_strategy, load_strategies


def _print_service(sr: tester.ServiceResult) -> None:
    mark = "✓" if sr.ok else "✗"
    print(f"  [{mark}] {sr.service}:")
    for s in sr.sites:
        m = "✓" if s.ok else "✗"
        lat = f"{s.latency_ms:.0f}ms" if s.latency_ms >= 0 else "  -  "
        print(f"      {m} {s.host:32} {s.status:8} {lat:>7}  {s.detail}")


def cmd_list() -> int:
    for s in load_strategies():
        print(f"  {s.id:32} {s.name}")
    return 0


def cmd_test() -> int:
    print(f"[i] Проверка доступности (без обхода). Рантайм: {paths.APP_HOME}")
    for sr in tester.test_all():
        _print_service(sr)
    rtt = tester.stun_rtt()
    print(f"  UDP STUN RTT: {rtt} ms" if rtt is not None else "  UDP STUN: недоступен")
    return 0


def cmd_stun() -> int:
    for _ in range(5):
        rtt = tester.stun_rtt()
        print(f"  RTT: {rtt} ms" if rtt is not None else "  RTT: timeout")
        time.sleep(1)
    return 0


def cmd_start(strategy_id: str) -> int:
    s = get_strategy(strategy_id)
    if not s:
        print(f"[!] Стратегия не найдена: {strategy_id}")
        return 1
    if not is_admin():
        print("[!] Нужны права администратора")
        return 1
    eng = Engine()
    try:
        eng.start(s)
    except EngineError as e:
        print(f"[!] {e}")
        return 1
    print(f"[✓] Стратегия '{s.name}' включена. Ctrl+C для остановки.")
    try:
        while eng.is_running():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop()
        print("\n[i] Обход выключен.")
    return 0


def cmd_verify(strategy_id: str) -> int:
    """Быстрая проверка одной стратегии: поднять -> протестировать -> погасить."""
    s = get_strategy(strategy_id)
    if not s:
        print(f"[!] Стратегия не найдена: {strategy_id}")
        return 1
    if not is_admin():
        print("[!] Нужны права администратора")
        return 1
    eng = Engine()
    print(f"[i] Поднимаю стратегию '{s.name}' и проверяю доступность...")
    try:
        eng.start(s)
    except EngineError as e:
        print(f"[!] Не удалось запустить: {e}")
        return 1
    try:
        results = tester.test_all()
        for sr in results:
            _print_service(sr)
        rtt = tester.stun_rtt()
        print(f"  UDP STUN RTT: {rtt} ms" if rtt is not None else "  UDP STUN: недоступен")
        all_ok = all(sr.ok for sr in results)
        print(f"\n[{'✓' if all_ok else '~'}] Стратегия '{s.name}': "
              f"{'все сервисы открылись' if all_ok else 'открылось частично'}")
    finally:
        eng.stop()
        print("[i] Обход выключен.")
    return 0


def cmd_search(stop_on_first: bool) -> int:
    if not is_admin():
        print("[!] Нужны права администратора (запусти терминал от админа)")
        return 1

    def on_progress(i, total, strat):
        print(f"\n[{i + 1}/{total}] Проверяю: {strat.name} ...", flush=True)

    def on_result(sc):
        if sc.error:
            print(f"    ⚠ не запустилась: {sc.error}")
        else:
            tag = "РАБОТАЕТ" if sc.working else "частично" if sc.sites_ok else "нет"
            print(f"    → {tag}: {sc.summary()}  (score {sc.score:.0f})")

    print("[i] Автоподбор стратегии. Это займёт несколько минут...")
    results = search(stop_on_first=stop_on_first, on_progress=on_progress, on_result=on_result)

    print("\n" + "=" * 60)
    print("РЕЗУЛЬТАТЫ (лучшие сверху):")
    working = [r for r in results if r.working]
    for r in results[:10]:
        tag = "✓" if r.working else " "
        print(f"  [{tag}] {r.strategy.name:28} score={r.score:7.0f}  {r.summary()}")
    if working:
        best = working[0]
        print(f"\n[✓] Лучшая рабочая стратегия: {best.strategy.name} (id: {best.strategy.id})")
    else:
        print("\n[!] Полностью рабочая стратегия не найдена. Смотри частичные выше.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    cmd, rest = args[0], args[1:]
    if cmd == "list":
        return cmd_list()
    if cmd == "test":
        return cmd_test()
    if cmd == "stun":
        return cmd_stun()
    if cmd == "start":
        if not rest:
            print("[!] Укажи id стратегии")
            return 1
        return cmd_start(rest[0])
    if cmd == "verify":
        if not rest:
            print("[!] Укажи id стратегии (напр. general)")
            return 1
        return cmd_verify(rest[0])
    if cmd == "search":
        return cmd_search(stop_on_first="--first" in rest)
    print(f"[!] Неизвестная команда: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
