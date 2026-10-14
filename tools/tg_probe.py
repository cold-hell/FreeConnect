#!/usr/bin/env python3
"""
Диагностика обхода Telegram: какой способ достучаться до веб-входа Telegram
(kws{N}.web.telegram.org/apiws) переживает DPI провайдера.

ЗАПУСКАТЬ С ВЫКЛЮЧЕННЫМ VPN — иначе тест бессмысленный (через VPN проходит всё).

Перебирает матрицу: [дата-центр] x [IP-адрес] x [профиль TLS] и для каждой
комбинации проверяет три ступени: TCP-коннект -> TLS-рукопожатие -> WebSocket-
апгрейд (HTTP 101). Где дошло до 101 — тот способ рабочий.

Гипотеза, которую проверяем: DNS отдаёт заблокированный IP, а на «запасных»
адресах Telegram тот же вход открыт (так делает GhostWire).

Зависимостей нет — только стандартная библиотека.

Использование:
    python tools/tg_probe.py                 # полный прогон
    python tools/tg_probe.py --ip 1.2.3.4    # добавить свой IP в перебор
"""
from __future__ import annotations

import argparse
import base64
import os
import socket
import ssl
import sys
import time

# Веб-вход Telegram по дата-центрам (тот же, что использует web.telegram.org).
HOSTS = {
    1: "kws1.web.telegram.org",
    2: "kws2.web.telegram.org",
    3: "kws3.web.telegram.org",
    4: "kws4.web.telegram.org",
    5: "kws5.web.telegram.org",
}
WS_PATH = "/apiws"

# IP, опубликованный в примере конфига GhostWire для ДЦ2/ДЦ4 (заведомо отличается
# от того, что отдаёт DNS) — главный кандидат на «незаблокированный» адрес.
KNOWN_GOOD = ["149.154.167.220"]

# Порядок шифров как у Chrome (Python по умолчанию шлёт свой, DPI может отличать).
CHROME_CIPHERS = (
    "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
    "ECDHE-RSA-AES128-SHA:ECDHE-RSA-AES256-SHA:AES128-GCM-SHA256:"
    "AES256-GCM-SHA384:AES128-SHA:AES256-SHA"
)
UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

TIMEOUT = 7.0


def make_ctx(profile: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    try:
        ctx.load_default_certs()
    except Exception:
        pass
    if profile == "chrome":
        # Прикидываемся браузером — но только порядком шифров.
        try:
            ctx.set_ciphers(CHROME_CIPHERS)
        except ssl.SSLError:
            pass
    # ALPN всегда http/1.1: WebSocket живёт поверх HTTP/1.1, и Chrome для ws-соединений
    # просит именно его. Если предложить h2, сервер согласует HTTP/2 и ответит на наш
    # апгрейд бинарными фреймами — рукопожатие развалится.
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def ws_upgrade(sock: ssl.SSLSocket, host: str) -> tuple[bool, str]:
    """Ручной WebSocket-апгрейд поверх готового TLS-сокета. True, если пришёл 101."""
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {WS_PATH} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Protocol: binary\r\n"
        f"Origin: https://web.telegram.org\r\n"
        f"User-Agent: {UA_CHROME}\r\n"
        f"\r\n"
    )
    sock.sendall(req.encode())
    sock.settimeout(TIMEOUT)
    buf = b""
    try:
        while b"\r\n\r\n" not in buf and len(buf) < 8192:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    except Exception as e:
        return False, f"нет ответа ({type(e).__name__})"
    first = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
    return ("101" in first), (first or "пусто")


def probe(host: str, ip: str, profile: str) -> tuple[str, str, str]:
    """Возвращает (tcp, tls, ws) — по строке-вердикту на каждую ступень."""
    # 1) TCP
    t = time.perf_counter()
    try:
        raw = socket.create_connection((ip, 443), timeout=TIMEOUT)
        raw.settimeout(TIMEOUT)
        tcp = f"ok {int((time.perf_counter() - t) * 1000)}ms"
    except Exception as e:
        return f"СБОЙ {type(e).__name__}", "-", "-"
    # 2) TLS (SNI = имя хоста, хотя коннектимся на IP)
    t = time.perf_counter()
    try:
        ctx = make_ctx(profile)
        sock = ctx.wrap_socket(raw, server_hostname=host)
        tls = f"ok {int((time.perf_counter() - t) * 1000)}ms"
    except ssl.SSLCertVerificationError as e:
        raw.close()
        return tcp, f"СЕРТИФИКАТ! {e.verify_message or e}", "-"
    except Exception as e:
        raw.close()
        return tcp, f"СБОЙ {type(e).__name__}", "-"
    # 3) WebSocket
    try:
        ok, detail = ws_upgrade(sock, host)
        ws = "101 OK" if ok else f"СБОЙ ({detail})"
    except Exception as e:
        ws = f"СБОЙ {type(e).__name__}"
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return tcp, tls, ws


def resolve(host: str) -> list[str]:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443,
                                                             proto=socket.IPPROTO_TCP)
                       if ":" not in ai[4][0]})
    except Exception:
        return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", action="append", default=[],
                    help="добавить IP-кандидат в перебор (можно несколько раз)")
    args = ap.parse_args()

    print("=" * 78)
    print("  Диагностика обхода Telegram — ЗАПУСКАТЬ С ВЫКЛЮЧЕННЫМ VPN")
    print("=" * 78)

    print("\n[1] DNS: что провайдер отдаёт на веб-вход Telegram")
    dns_map: dict[int, list[str]] = {}
    for dc, host in HOSTS.items():
        ips = resolve(host)
        dns_map[dc] = ips
        print(f"    ДЦ{dc}  {host:28s} -> {', '.join(ips) if ips else 'НЕ РЕЗОЛВИТСЯ'}")

    # Кандидаты: то, что дал DNS + заведомо рабочий из GhostWire + переданные вручную.
    extra = list(dict.fromkeys(KNOWN_GOOD + args.ip))

    print("\n[2] Перебор: TCP -> TLS -> WebSocket(101)")
    print(f"    Кандидаты помимо DNS: {', '.join(extra)}")
    winners: list[str] = []
    for dc, host in HOSTS.items():
        print(f"\n  --- ДЦ{dc} ({host}) ---")
        candidates = [(ip, "DNS") for ip in dns_map[dc]] + [(ip, "доп") for ip in extra]
        if not candidates:
            print("      кандидатов нет (DNS пуст)")
            continue
        for ip, origin in candidates:
            for profile in ("default", "chrome"):
                tcp, tls, ws = probe(host, ip, profile)
                tag = f"{ip:16s} [{origin:3s}] TLS={profile:7s}"
                print(f"      {tag} TCP:{tcp:18s} TLS:{tls:22s} WS:{ws}")
                if ws == "101 OK":
                    winners.append(f"ДЦ{dc} {host} через {ip} ({origin}, TLS={profile})")

    print("\n" + "=" * 78)
    if winners:
        print("  РАБОЧИЕ КОМБИНАЦИИ (по ним и будем строить обход):")
        for w in winners:
            print(f"    + {w}")
    else:
        print("  Рабочих комбинаций НЕТ — ни один способ не пробил DPI.")
        print("  Пришли вывод целиком, будем смотреть, на какой ступени рвётся.")
    print("=" * 78)


if __name__ == "__main__":
    sys.exit(main())
