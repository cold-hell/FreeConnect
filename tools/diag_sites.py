#!/usr/bin/env python3
"""
Диагностика «Своих сайтов»: почему конкретный сайт не открывается, даже когда он
добавлен в обход. winws умеет обходить ТОЛЬКО DPI по имени сайта (SNI). Другие типы
блокировки (по IP, подмена DNS) он не лечит — их надо видеть отдельно, иначе мы
чиним не то.

Скрипт по каждому домену проходит четыре ступени и ставит вердикт:
  1) системный DNS         -> в какой IP резолвит провайдер;
  2) честный DNS (DoH)     -> в какой IP резолвит Cloudflare (эталон);
  3) TCP-коннект :443      -> доходит ли вообще SYN;
  4) TLS-рукопожатие (SNI) -> режут ли соединение по имени сайта.
Ступени 3-4 гоняются И на системном, И на честном IP — это и различает причины:

  ОТКРЫВАЕТСЯ   — TLS прошёл. Обход не нужен либо уже работает.
  DPI-ПО-SNI    — TLS рвётся/молчит и на честном IP  -> лечится обходом winws.
  IP-БЛОК       — SYN не доходит даже на честный IP  -> лечится ТОЛЬКО VPN.
  ПОДМЕНА-DNS   — честный IP открывается, системный нет -> лечится «Шифровать DNS».
  НЕЯСНО        — не уложилось в шаблон, см. сырые ошибки.

Зависимостей нет — только стандартная библиотека. Запускать МОЖНО при включённом
обходе (winws перехватывает трафик этого скрипта так же, как трафик браузера).
ВАЖНО: если на машине поднят полный VPN — через него проходит всё, и тест покажет
«всё открывается». Тогда результат недостоверен, о чём скрипт предупредит.

Использование:
    python tools/diag_sites.py                    # все домены из обхода
    python tools/diag_sites.py x.com rutor.info   # только указанные
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import socket
import ssl
import sys
import urllib.request

RUNTIME_HOSTLIST = r"C:\FreeConnect\runtime\lists\list-general-user.txt"
LOG_DIR = r"C:\FreeConnect\logs"
CONTROL_HOST = "www.google.com"   # заведомо живой TLS для преполётной проверки
TIMEOUT = 4.0
WORKERS = 12


def load_domains() -> list[str]:
    """Домены из аргументов или из рантайм-hostlist обхода."""
    args = [a.strip().lower() for a in sys.argv[1:] if a.strip()]
    if args:
        return args
    out: list[str] = []
    try:
        with open(RUNTIME_HOSTLIST, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#") and "." in s:
                    out.append(s)
    except OSError as e:
        print(f"Не прочитал {RUNTIME_HOSTLIST}: {e}")
    return out


def resolve_system(host: str) -> list[str]:
    """IP-адреса от системного (провайдерского) резолвера."""
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        return sorted({i[4][0] for i in infos})
    except OSError:
        return []


def resolve_doh(host: str) -> list[str]:
    """Честные IP от Cloudflare DoH (JSON). Пусто — если DoH сам не достучался."""
    for base, sni in (("https://1.1.1.1/dns-query", "cloudflare-dns.com"),
                      ("https://8.8.8.8/resolve", "dns.google")):
        try:
            url = f"{base}?name={host}&type=A"
            req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
            ctx = ssl.create_default_context()
            # SNI выставляем явно на доменное имя DoH-сервера (идём по IP).
            ctx.check_hostname = True
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
            if ips:
                return sorted(set(ips))
        except Exception:  # noqa: BLE001  (любой сбой DoH -> пробуем следующий/пусто)
            continue
    return []


def tls_probe(ip: str, host: str) -> str:
    """Одна попытка TCP+TLS на конкретный IP с настоящим SNI=host.
    Возвращает короткий код исхода."""
    try:
        raw = socket.create_connection((ip, 443), timeout=TIMEOUT)
    except (socket.timeout, TimeoutError):
        return "tcp_timeout"
    except ConnectionRefusedError:
        return "tcp_refused"
    except OSError as e:
        return f"tcp_err:{getattr(e, 'winerror', '') or e}"
    try:
        raw.settimeout(TIMEOUT)
        ctx = ssl._create_unverified_context()  # нам важен транспорт, не валидность серта
        with ctx.wrap_socket(raw, server_hostname=host):
            return "ok"
    except ConnectionResetError:
        return "tls_reset"      # RST сразу после ClientHello — классический DPI по SNI
    except (socket.timeout, TimeoutError):
        return "tls_timeout"    # молчаливый дроп — тоже DPI
    except ssl.SSLError as e:
        return f"tls_err:{e.reason or e}"
    except OSError as e:
        return f"tls_oserr:{getattr(e, 'winerror', '') or e}"
    finally:
        try:
            raw.close()
        except OSError:
            pass


def _fail(code: str) -> bool:
    return code != "ok"


def _is_dpi(code: str) -> bool:
    # Обрыв сразу после ClientHello в любом виде — почерк DPI-инъекции по SNI:
    # RST (tls_reset), молчаливый дроп (tls_timeout) или закрытие соединения
    # (UNEXPECTED_EOF/ECONNRESET внутри tls_err/tls_oserr).
    if code in ("tls_reset", "tls_timeout"):
        return True
    return code.startswith(("tls_err", "tls_oserr")) and (
        "UNEXPECTED_EOF" in code or "reset" in code.lower() or "10054" in code)


def _is_unreachable(code: str) -> bool:
    return code in ("tcp_timeout", "tcp_refused") or code.startswith("tcp_err")


def classify(sys_ips, doh_ips, sys_res, doh_res) -> tuple[str, str]:
    """(вердикт, пояснение) по результатам ступеней."""
    if sys_res == "ok":
        return "ОТКРЫВАЕТСЯ", "TLS прошёл на системном IP"
    if not sys_ips and doh_ips:
        # провайдер не отдал адрес вовсе, а честный DNS отдал
        if doh_res == "ok":
            return "ПОДМЕНА-DNS", "системный DNS не резолвит, честный IP открывается"
        return "ПОДМЕНА-DNS", "системный DNS не резолвит (NXDOMAIN/воронка)"
    if doh_ips:
        if doh_res == "ok" and _fail(sys_res):
            return "ПОДМЕНА-DNS", f"честный IP открывается, системный — нет ({sys_res})"
        if _is_dpi(doh_res):
            return "DPI-ПО-SNI", f"TLS рвётся и на честном IP ({doh_res})"
        if _is_unreachable(doh_res):
            return "IP-БЛОК", f"SYN не доходит даже на честный IP ({doh_res})"
        return "НЕЯСНО", f"honest={doh_res} sys={sys_res}"
    # DoH недоступен — судим по системному IP, но причину назвать труднее
    if _is_dpi(sys_res):
        return "DPI-ПО-SNI", f"TLS рвётся ({sys_res}); DoH недоступен, IP не сверить"
    if _is_unreachable(sys_res):
        return "IP-БЛОК/DNS", f"SYN не доходит ({sys_res}); DoH недоступен, не различить"
    return "НЕЯСНО", f"sys={sys_res}, DoH недоступен"


def probe_domain(host: str) -> dict:
    sys_ips = resolve_system(host)
    doh_ips = resolve_doh(host)
    sys_res = tls_probe(sys_ips[0], host) if sys_ips else "no_dns"
    doh_res = tls_probe(doh_ips[0], host) if doh_ips else "no_doh"
    verdict, why = classify(sys_ips, doh_ips, sys_res, doh_res)
    return {"host": host, "sys_ips": sys_ips, "doh_ips": doh_ips,
            "sys_res": sys_res, "doh_res": doh_res, "verdict": verdict, "why": why}


ADVICE = {
    "ОТКРЫВАЕТСЯ": "обход работает или не нужен",
    "DPI-ПО-SNI":  "лечится обходом winws — если сайт в списке и всё равно так, стратегия слаба для него",
    "IP-БЛОК":     "winws бессилен, нужен VPN",
    "ПОДМЕНА-DNS": "включи «Шифровать DNS» (DoH) в настройках",
    "IP-БЛОК/DNS": "нужен VPN или «Шифровать DNS» (без DoH-сверки точнее не сказать)",
    "НЕЯСНО":      "смотри сырые коды ниже",
}
ORDER = ["ОТКРЫВАЕТСЯ", "DPI-ПО-SNI", "ПОДМЕНА-DNS", "IP-БЛОК", "IP-БЛОК/DNS", "НЕЯСНО"]


def main() -> int:
    domains = load_domains()
    if not domains:
        print("Список доменов пуст.")
        return 1

    print("Преполётная проверка сети…", flush=True)
    ctrl = tls_probe((resolve_system(CONTROL_HOST) or ["142.250.0.0"])[0], CONTROL_HOST)
    ctrl_line = f"Контроль ({CONTROL_HOST}): {ctrl}"
    warn = ""
    if ctrl != "ok":
        warn = ("ВНИМАНИЕ: даже контрольный www.google.com не открылся — сеть/VPN в "
                "нестандартном состоянии, результатам верить нельзя.")
    print(ctrl_line)
    if warn:
        print(warn)

    print(f"Проверяю доменов: {len(domains)} (по ~{TIMEOUT:.0f}с на ступень)…", flush=True)
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for r in pool.map(probe_domain, domains):
            results.append(r)
            print(f"  {r['verdict']:<12} {r['host']}", flush=True)

    groups: dict[str, list[dict]] = {}
    for r in results:
        groups.setdefault(r["verdict"], []).append(r)

    lines: list[str] = []
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"FreeConnect — диагностика сайтов, {stamp}")
    lines.append(ctrl_line)
    if warn:
        lines.append(warn)
    lines.append("")
    lines.append("ИТОГ по категориям:")
    for v in ORDER:
        if v in groups:
            lines.append(f"  {v}: {len(groups[v])} — {ADVICE.get(v, '')}")
    lines.append("")
    lines.append("=" * 70)
    for v in ORDER:
        if v not in groups:
            continue
        lines.append(f"\n### {v} ({len(groups[v])}) — {ADVICE.get(v, '')}")
        for r in sorted(groups[v], key=lambda x: x["host"]):
            lines.append(
                f"  {r['host']}\n"
                f"      системный DNS: {', '.join(r['sys_ips']) or '—'}  -> TLS: {r['sys_res']}\n"
                f"      честный  DNS:  {', '.join(r['doh_ips']) or '—'}  -> TLS: {r['doh_res']}\n"
                f"      причина: {r['why']}"
            )

    report = "\n".join(lines)
    print("\n" + report)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fname = os.path.join(LOG_DIR, "site_diag_" +
                             dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".txt")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\nОтчёт сохранён: {fname}")
    except OSError as e:
        print(f"\nНе смог сохранить отчёт: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
