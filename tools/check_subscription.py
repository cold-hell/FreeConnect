#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностика подписки: что реально отдаёт панель и что из этого понял FreeConnect.

Зачем: панели с привязкой к устройству (Happ-шлюзы, 3x-ui/Remnawave с device-limit)
отдают клиенту БЕЗ ID устройства (HWID) заглушку — один нерабочий сервер с remarks
«Данное приложение не поддерживается». Снаружи это выглядит как «VPN не подключается»,
хотя на самом деле серверов просто не дали.

Запуск (из корня проекта):
    .venv\\Scripts\\python.exe tools\\check_subscription.py <ссылка-подписка>
    .venv\\Scripts\\python.exe tools\\check_subscription.py <ссылка> <HWID>

Без второго аргумента запрос уходит без ID устройства — панель не регистрирует новое
устройство, так что подписку этим не испортить. ID можно скопировать в Happ:
подписка → «Your HWID» → «Copy HWID» (или взять в поле «ID устройства» FreeConnect).
"""
from __future__ import annotations

import base64
import platform
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from freeconnect import __version__, vpn  # noqa: E402

UAS = ["Happ/1.11.0", "Streisand", "v2rayNG/1.9.5", "clash-verge/1.5", "sing-box",
       "FreeConnect"]


def headers_for(ua: str, hwid: str) -> dict:
    h = {"User-Agent": ua}
    if not hwid:
        return h
    ver = ua.split("/", 1)[1] if "/" in ua else __version__
    h.update({"X-HWID": hwid, "X-Device-OS": "Windows",
              "X-Ver-OS": f"{platform.release()} {platform.version()}".strip() or "Windows",
              "X-Device-model": platform.machine() or "PC",
              "X-App-Version": ver, "X-Device-Locale": "ru"})
    return h


def decode_header(value: str) -> str:
    v = (value or "").strip()
    if v.lower().startswith("base64:"):
        raw = v[7:].strip()
        try:
            return base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return raw
    return v


def main(argv: list[str]) -> int:
    # Консоль Windows бывает в cp1251 и падает на эмодзи из сообщений панели.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    if len(argv) < 2:
        print(__doc__)
        return 2
    url = argv[1].strip()
    hwid = (argv[2].strip() if len(argv) > 2 else "")
    # HWID можно взять и из самой ссылки (параметр HWID у HWID-подписок).
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    url_hwid = (qs.get("HWID") or qs.get("hwid") or [""])[0].strip()
    hwid = hwid or url_hwid
    print(f"Подписка: {url}")
    print(f"ID устройства: {hwid or '— (запрос без HWID, устройство не регистрируется)'}")
    print("=" * 78)

    best = None
    for ua in UAS:
        try:
            req = urllib.request.Request(url, headers=headers_for(ua, hwid))
            with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
                body = r.read().decode("utf-8", "replace")
                hd = getattr(r, "headers", {})
                status = getattr(r, "status", 200)
        except Exception as e:  # noqa: BLE001
            print(f"{ua:18s} ОШИБКА: {type(e).__name__}: {e}")
            continue
        try:
            servers = vpn.parse_servers(body)
        except Exception as e:  # noqa: BLE001
            servers = []
            print(f"{ua:18s} не разобрал: {e}")
        announce = decode_header(hd.get("Announce", ""))
        title = decode_header(hd.get("Profile-Title", ""))
        print(f"{ua:18s} HTTP {status} · {len(body):7d} Б · серверов: {len(servers)}"
              f"{' · панель: ' + title if title else ''}")
        if announce:
            print(f"{'':18s} сообщение панели: {announce}")
        if vpn.looks_like_stub(body) and len(servers) <= 1:
            print(f"{'':18s} ⚠ это ЗАГЛУШКА: нужен ID устройства (HWID)")
        if best is None or len(servers) > len(best[1]):
            best = (ua, servers)

    if not best:
        print("\nНи один запрос не удался — проверь ссылку и интернет.")
        return 1
    ua, servers = best
    print("=" * 78)
    print(f"Лучший ответ: {ua} · серверов: {len(servers)}")
    for s in servers[:60]:
        print(f"  {s.kind:14s} {s.address}:{s.port:<6d} {s.name}")
    if len(servers) > 60:
        print(f"  … ещё {len(servers) - 60}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
