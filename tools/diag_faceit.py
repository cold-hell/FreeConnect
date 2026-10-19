# -*- coding: utf-8 -*-
"""Диагностика EU-серверов FACEIT: где именно ломается путь.

Проверяет без прав администратора и не трогает текущий обход:
  * DNS и ping до известных европейских узлов/диапазонов;
  * TCP/UDP на игровые порты (27015, 27020, 27036);
  * маршрут (tracert) — видно, где обрывается;
  * состояние списков и winws (game filter, ipset).

Запуск:  .venv\\Scripts\\python.exe tools\\diag_faceit.py [ip ...]
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freeconnect import paths  # noqa: E402

# Известные EU-узлы, которыми пользуются FACEIT/CS2 и Steam.
#  * 155.133.* / 162.254.* — Steam CM/DataCenter (EU);
#  * 146.66.*, 185.25.182.* — игровые хостинги, где часто бывают FACEIT-серверы;
#  * Cloudflare — часть EU-серверов FACEIT отдаётся через CF.
HOSTS = [
    "faceit.com",
    "api.faceit.com",
    "ac-client-ws.faceit.com",
    "client.faceit.com",
    "155.133.248.1",
    "162.254.196.1",
    "146.66.155.1",
    "185.25.182.1",
]
GAME_PORTS = [27015, 27020, 27036]


def log(msg: str = "") -> None:
    print(msg, flush=True)


def dns(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except OSError as e:
        return f"DNS-ошибка: {e}"


def ping(host: str, timeout_ms: int = 1500) -> str:
    try:
        out = subprocess.run(["ping", "-n", "3", "-w", str(timeout_ms), host],
                             capture_output=True, text=True, timeout=20,
                             encoding="cp866", errors="replace")
        text = out.stdout or ""
        loss = next((ln.strip() for ln in text.splitlines() if "Потер" in ln or "lost" in ln), "")
        avg = next((ln.strip() for ln in text.splitlines() if "Среднее" in ln or "Average" in ln), "")
        return f"{loss} {avg}".strip() or text.strip().splitlines()[-1][:80]
    except Exception as e:  # noqa: BLE001
        return f"ping не выполнился: {e}"


def tcp(host: str, port: int, timeout: float = 3.0) -> str:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return f"открыт за {(time.perf_counter()-t0)*1000:.0f}мс"
    except socket.timeout:
        return f"таймаут {timeout}с"
    except OSError as e:
        return f"{type(e).__name__}: {e}"


def udp(host: str, port: int, timeout: float = 2.0) -> str:
    """A2S-запрос к игровому серверу (Source Query). Ответ = сервер жив и достижим.

    Шлём стандартный A2S_INFO: FF FF FF FF 54 "Source Engine Query\\0".
    """
    pkt = b"\xff\xff\xff\xffTSource Engine Query\x00"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (host, port))
        data, _ = s.recvfrom(4096)
        return f"ответ {len(data)} б (сервер отвечает)"
    except socket.timeout:
        return "нет ответа (UDP дропается или порт другой)"
    except OSError as e:
        return f"{type(e).__name__}: {e}"
    finally:
        s.close()


def tracert(host: str, hops: int = 12) -> list[str]:
    try:
        out = subprocess.run(["tracert", "-d", "-w", "800", "-h", str(hops), host],
                             capture_output=True, text=True, timeout=180,
                             encoding="cp866", errors="replace")
        lines = [ln.rstrip() for ln in (out.stdout or "").splitlines() if ln.strip()]
        return lines[-hops:]
    except Exception as e:  # noqa: BLE001
        return [f"tracert не выполнился: {e}"]


def probe_state() -> None:
    log("=== состояние FreeConnect ===")
    rt = paths.RUNTIME_DIR
    log(f"  runtime: {rt}")
    for name in ("ipset-all.txt", "list-general.txt", "list-general-user.txt"):
        p = paths.LISTS_DIR / name
        if p.is_file():
            n = sum(1 for _ in p.open("r", encoding="utf-8", errors="replace"))
            log(f"  {name}: {p.stat().st_size} б, строк {n}")
    try:
        from freeconnect import config
        cfg = config.load()
        log(f"  game_filter={cfg.get('game_filter')} doh={cfg.get('doh')} "
            f"strategy={cfg.get('strategy')}")
    except Exception as e:  # noqa: BLE001
        log(f"  конфиг не прочитан: {e}")
    try:
        data = json.loads(paths.STRATEGIES_JSON.read_text(encoding="utf-8"))
        log(f"  стратегий в наборе: {len(data.get('strategies', []))}")
    except Exception as e:  # noqa: BLE001
        log(f"  набор стратегий не прочитан: {e}")


def main() -> None:
    extra = sys.argv[1:]
    probe_state()
    log()
    log("=== DNS и ping ===")
    for h in HOSTS + extra:
        ip = dns(h)
        log(f"  {h:28} -> {ip:18} ping: {ping(ip if ':' not in ip else h)}")
    log()
    log("=== TCP 443 (Cloudflare/API FACEIT) ===")
    for h in ("faceit.com", "api.faceit.com", "ac-client-ws.faceit.com") + tuple(extra):
        ip = dns(h)
        if ":" in ip or " " in ip:
            continue
        log(f"  {h:28} {ip:18} {tcp(ip, 443)}")
    log()
    log("=== UDP игровые порты (A2S-запрос) ===")
    for h in ("155.133.248.1", "162.254.196.1", "146.66.155.1") + tuple(extra):
        ip = dns(h)
        if ":" in ip or " " in ip:
            continue
        for port in GAME_PORTS:
            log(f"  {ip:18}:{port} {udp(ip, port)}")
    log()
    log("=== маршрут до EU-узла (последние хопы) ===")
    target = extra[0] if extra else "155.133.248.1"
    for ln in tracert(target):
        log("  " + ln)
    log()
    log("Подсказка: если ping идёт, а UDP на 27015 молчит — режется игровой UDP;")
    log("если маршрут обрывается до последнего хопа — проблема маршрутизации, и")
    log("zapret (L7-десинк) её не решает, нужен туннель для этого трафика.")


if __name__ == "__main__":
    main()
