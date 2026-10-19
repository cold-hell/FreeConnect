# -*- coding: utf-8 -*-
"""Проверка UDP-пути до игровых серверов (без прав администратора).

Как проверить «работает ли game filter» по факту:
  * если UDP до чужих игровых серверов отвечает БЕЗ обхода, значит порт не режут;
  * если и с обходом молчит — смотреть нечего, ломает либо фильтр, либо маршрут.

Тест шлёт настоящий A2S_INFO запрос (как игровой клиент) и ждёт ответ.
Серверы берутся из публичного списка Steam-мастер-сервера (если доступен) и из
небольшого набора известных адресов.
"""
from __future__ import annotations

import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

A2S = b"\xff\xff\xff\xffTSource Engine Query\x00"

# Steam master server (IPv4). Отдаёт список публичных игровых серверов.
MASTER = [("hl2master.steampowered.com", 27011), ("hl2master.steampowered.com", 27010)]
REGION_EU = "0xFF"   # the world


def master_query(addr: tuple[str, int], region: bytes = b"\xff", last: str = "0.0.0.0:0",
                 timeout: float = 4.0) -> list[tuple[str, int]]:
    """Запрос к мастер-серверу Steam: возвращает список серверов (ip, port)."""
    out: list[tuple[str, int]] = []
    ip, port = last.split(":")
    pkt = b"\x31" + bytes([region[0] if isinstance(region, bytes) else region]) + ip.encode() + b"\x00" + struct.pack("!H", int(port))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, addr)
        data, _ = s.recvfrom(8192)
        if data[:4] != b"\xff\xff\xff\xff" or data[4] != 0x66:
            return out
        body = data[6:]
        for i in range(0, len(body) - 5, 6):
            a = body[i:i + 4]
            p = struct.unpack("!H", body[i + 4:i + 6])[0]
            if p:
                out.append((socket.inet_ntoa(a), p))
    except OSError:
        pass
    finally:
        s.close()
    return out


def a2s_ping(ip: str, port: int = 27015, attempts: int = 2, timeout: float = 1.5) -> tuple[bool, float | None, int]:
    """Шлёт A2S_INFO, возвращает (ответил, rtt_ms, размер ответа)."""
    for _ in range(attempts):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            t0 = time.perf_counter()
            s.sendto(A2S, (ip, port))
            data, _ = s.recvfrom(4096)
            if data[:4] == b"\xff\xff\xff\xff":
                return True, round((time.perf_counter() - t0) * 1000, 1), len(data)
        except socket.timeout:
            continue
        except OSError:
            break
        finally:
            s.close()
    return False, None, 0


def main() -> None:
    print("=== мастер-сервер Steam: получаем реальные игровые серверы ===", flush=True)
    servers: list[tuple[str, int]] = []
    for addr in MASTER:
        got = master_query(addr)
        print(f"  {addr[0]}:{addr[1]} -> серверов {len(got)}", flush=True)
        servers += got
        if servers:
            break

    print()
    if servers:
        print(f"=== A2S-опрос первых {min(12, len(servers))} серверов (порт из списка) ===", flush=True)
        ok = 0
        for ip, port in servers[:12]:
            alive, rtt, n = a2s_ping(ip, port)
            if alive:
                ok += 1
            print(f"  {ip:16}:{port:<6} {'ответ' if alive else 'НЕТ ОТВЕТА':12} rtt={rtt}мс {n}б", flush=True)
        print(f"  итог: ответили {ok} из {min(12, len(servers))}", flush=True)
    else:
        print("  список серверов не получен — проверяем известные адреса", flush=True)

    print()
    print("=== контрольный A2S на порт 27015 по нескольким точкам ===", flush=True)
    for ip in ("155.133.248.1", "162.254.196.1", "146.66.155.1", "185.25.182.1"):
        alive, rtt, n = a2s_ping(ip)
        print(f"  {ip:16}:27015 {'ответ' if alive else 'НЕТ ОТВЕТА'} rtt={rtt} {n}б", flush=True)

    print()
    print("Если мастер-сервер отдал IP, а A2S молчит на всех — UDP до игровых портов")
    print("режется/дропается. Если отвечает хотя бы часть — UDP ходит, и проблема")
    print("в конкретных адресах FACEIT EU (маршрут/адресная фильтрация).")


if __name__ == "__main__":
    main()
