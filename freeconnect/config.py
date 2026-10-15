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
    "vpn_hwid": "",         # ID устройства для панелей с device-limit (X-HWID). Пусто =
                            # сгенерируем свой при первом импорте; можно вставить ID из Happ
    "vpn_panel_announce": "",  # сообщение панели (заголовок Announce) — показываем в UI
    "vpn_panel_title": "",     # название подписки из панели (Profile-Title)
    "vpn_panel_provider": "",  # Providerid панели (для поддержки/диагностики)
    # Соединение Telegram: локальный MTProto-прокси -> WebSocket к веб-входу Telegram.
    # Ядро вендорено из Flowseal/tg-ws-proxy (см. freeconnect/tgws/UPSTREAM.md).
    "tg_port": 1443,        # порт локального прокси (по умолчанию как у ядра); если занят —
                            # берём свободный и запоминаем фактический
    "tg_enabled": False,    # держать ли прокси Telegram включённым при старте
    "tg_secret": "",        # MTProto-секрет (32 hex). Пусто = сгенерируем при первом
                            # включении и сохраним, чтобы ссылка для Telegram не менялась
    "tg_dc_ips": {          # адреса веб-входа по дата-центрам: DNS отдаёт заблокированные,
        "2": "149.154.167.220",   # поэтому ходим на конкретный IP с правильным SNI.
        "4": "149.154.167.220",   # Меняется без пересборки приложения.
    },
    "tg_cfproxy": True,     # резерв через Cloudflare-домены, когда прямые адреса придушат
    "tg_h2": True,          # мультиплексирование медиа по HTTP/2 (фото/видео через резерв)
    "tg_cf_domains": [],    # свой Cloudflare-домен (kws1..kws203 -> IP Telegram). Пусто =
                            # автоматический пул ядра, который оно само тянет с GitHub
    "tg_faketls": "",       # домен для маскировки локального участка под TLS (редко нужен)
    "tg_pool_size": 4,      # размер пула WebSocket-соединений на дата-центр (0 = без пула)
    "tg_core_version": "",  # версия вендоренного ядра, которая реально работает
    "tg_core_latest": "",   # последний тег ядра, увиденный на GitHub (для подсказки в UI)
    "tg_core_checked_at": 0,  # unixtime последней проверки обновления ядра
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
