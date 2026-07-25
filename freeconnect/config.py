"""Сохранение состояния и настроек FreeConnect в config.json."""
from __future__ import annotations

import json
from typing import Any

from . import paths

DEFAULTS: dict[str, Any] = {
    "strategy": None,       # id выбранной стратегии
    "working": [],          # последний список рабочих стратегий (для быстрого выбора)
    "autostart": False,     # автозапуск при старте Windows
    "monitor": True,        # мониторинг голоса (UDP) + доступности Discord/YouTube (TCP), авто-восстановление
    "auto_enable": True,    # включать обход сразу при запуске приложения
    "onboarded": False,     # обучение уже пройдено (иначе localStorage стирается WebView2)
    "game_filter": False,   # покрывать игровой трафик (порты 1024-65535) — для игр
    "doh": False,           # DNS-over-HTTPS: шифровать DNS, обходить DNS-подмену (опция)
    "voice_confirm": False, # точная проверка голоса: при генерации своих стратегий гайдить
                            # юзера подтвердить в живом Discord (ground truth, а не STUN-прокси)
    "voice_watch": False,   # эксперим.: пассивный детектор голоса через WinDivert SNIFF —
                            # ловит односторонний/мёртвый медиапоток Discord и авто-чинит
    "strategies_updated_at": 0,  # unixtime последнего автообновления стратегий из upstream
    "sites_seed_version": 0,     # версия засеянных дефолтных доменов «свои сайты» (см. sites.SEED_VERSION)
    # VPN-для-Discord (свой VPN пользователя, весь Discord через туннель):
    "vpn_sub_url": "",      # ссылка-подписка (основной способ импорта)
    "vpn_config": "",       # сырой текст подписки/JSON (кэш серверов, если ссылка недоступна)
    "vpn_country": "",      # выбранная страна-выход ("" = авто; "__other__" = прочие);
                            # внутри страны сервер выбираем по живости (не фиксированный)
    "vpn_enabled": False,   # держать ли туннель включённым (по A1 — пока сам не отключит)
    # Обход Telegram (локальный SOCKS5→WebSocket к web.telegram.org, без VPN):
    "tg_port": 1080,        # порт локального SOCKS5-прокси
    "tg_enabled": False,    # держать ли прокси Telegram включённым при старте
    "tg_endpoints_updated_at": 0,  # unixtime последней сверки адресов с нашим каналом
    "tg_endpoints": {},     # {"2": ["1.2.3.4"]} — переопределить живые адреса веб-входа
                            # Telegram (DNS отдаёт заблокированные). Пусто = встроенная
                            # таблица. Даёт починить умерший IP без пересборки.
    "tg_cf_domains": [],    # резервные Cloudflare-домены (за ними релей на веб-вход
                            # Telegram): страховка, когда прямые IP придушат. Идём на
                            # kws{dc}.{домен}. Пусто = резерва нет. Заряжается с канала.
    "tg_cf_updated_at": 0,  # unixtime последней сверки cf_domains с каналом
}


def load() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        if paths.CONFIG_PATH.is_file():
            data = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update({k: data.get(k, v) for k, v in DEFAULTS.items()})
    except Exception:
        pass
    return cfg


def save(cfg: dict[str, Any]) -> None:
    try:
        paths.ensure_dirs()
        paths.CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
