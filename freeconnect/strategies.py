"""
Загрузка и разрешение стратегий обхода.

Стратегия в strategies.json хранится с плейсхолдерами {BIN}/{LISTS}/{ROOT}.
resolve_args() подставляет реальные абсолютные пути рантайма — получается готовый
список аргументов для winws.exe.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

# Фолбэк-блобы. winws ПАДАЕТ на старте, если стратегия ссылается на несуществующий
# .bin («could not read ...»), и тогда ВСЕ стратегии становятся «нерабочими». Апстрим
# (Flowseal) перешёл на ЖИВОЙ захват пакетов — ACTIVE_DISCORD_UDP.bin/ACTIVE_GAME_UDP.bin
# создаёт его батник на лету, а ещё ссылается на stun2.bin/quic_initial_4pda.to.bin,
# которых в нашем наборе нет. Наш strategy_update тянет эти ссылки дословно → winws
# не находит файлы. Чтобы не зависеть от того, что напишет апстрим, для КАЖДОГО
# недостающего блоба кладём копию подходящего имеющегося (см. ensure_referenced_blobs).
_FALLBACK_TLS = "tls_clienthello_www_google_com.bin"   # для *tls*-фейков
_FALLBACK_DEFAULT = "quic_initial_dbankcloud_ru.bin"   # для discord/quic/stun/udp-фейков


def _referenced_blobs(*json_paths: Path) -> set[str]:
    """Имена всех .bin, на которые ссылаются стратегии ({BIN}/имя.bin)."""
    refs: set[str] = set()
    for p in json_paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        refs |= set(re.findall(r"\{BIN\}/([A-Za-z0-9_.\-]+\.bin)", text))
    return refs


def ensure_referenced_blobs(log=None) -> int:
    """Создаёт фолбэк-копии для блобов, на которые ссылаются стратегии, но которых нет
    в bin/. Без этого winws не может прочитать файл и падает на старте — и подбор бракует
    ВСЕ стратегии подряд. Возвращает число созданных файлов. Идемпотентно и безопасно:
    существующие блобы не трогаем."""
    log = log or (lambda *_: None)
    bin_dir = paths.BIN_DIR
    if not bin_dir.is_dir():
        return 0
    have = {p.name for p in bin_dir.glob("*.bin")}
    if not have:
        return 0
    custom_json = paths.APP_HOME / "custom_strategies.json"
    made = 0
    for name in sorted(_referenced_blobs(paths.STRATEGIES_JSON, custom_json)):
        if name in have:
            continue
        src = _FALLBACK_TLS if "tls" in name.lower() else _FALLBACK_DEFAULT
        if src not in have:
            src = sorted(have)[0]           # хоть что-то читаемое, лишь бы winws не падал
        try:
            shutil.copy2(bin_dir / src, bin_dir / name)
            have.add(name)
            made += 1
            log(f"blob fallback: {name} <- {src}")
        except OSError as e:
            log(f"blob fallback failed {name}: {e}")
    return made


@dataclass
class Strategy:
    id: str
    name: str
    source_bat: str
    args: list[str] = field(default_factory=list)

    def resolve_args(self, game_filter: bool | None = None) -> list[str]:
        """Возвращает аргументы winws с подставленными реальными путями и портами.

        game_filter: покрывать ли игровой трафик. None -> читаем из config.
        ВКЛ -> порты 1024-65535 (как zapret), ВЫКЛ -> 12 (заглушка, ничего не ловит).
        """
        bin_dir = str(paths.BIN_DIR).replace("\\", "/")
        lists_dir = str(paths.LISTS_DIR).replace("\\", "/")
        root_dir = str(paths.RUNTIME_DIR).replace("\\", "/")
        if game_filter is None:
            try:
                from . import config
                game_filter = config.load().get("game_filter", False)
            except Exception:
                game_filter = False
        game_ports = "1024-65535" if game_filter else "12"
        out: list[str] = []
        for a in self.args:
            a = (a.replace("{BIN}", bin_dir)
                  .replace("{LISTS}", lists_dir)
                  .replace("{ROOT}", root_dir)
                  .replace("{GAME_TCP}", game_ports)
                  .replace("{GAME_UDP}", game_ports))
            out.append(a)
        return out


def _sanitize_args(args: list[str]) -> list[str]:
    """Выкидывает битые токены (артефакты парсинга .bat: каретка ^, неразвёрнутый
    %VAR%). Такой токен winws читает как файл и падает («could not read ^!»).
    Токены самостоятельные (--key=value), поэтому удаление безопасно."""
    out = []
    for a in args:
        if "^" in a or "%" in a:
            continue  # артефакты batch-экранирования (^!) и неразвёрнутые %VAR%
        if a == "--dpi-desync-fake-tls=0x00000000":
            continue  # нулевой fake-tls ломает --dpi-desync-fake-tls-mod (could not mod tls)
        out.append(a)
    return out


def load_strategies(path: Path | None = None, include_custom: bool = True) -> list[Strategy]:
    p = path or paths.STRATEGIES_JSON
    data = json.loads(Path(p).read_text(encoding="utf-8"))
    strategies = []
    for s in data["strategies"]:
        st = Strategy(**s)
        st.args = _sanitize_args(st.args)
        strategies.append(st)
    if include_custom and path is None:
        try:
            from .custom import load_custom  # ленивый импорт: избегаем цикла
            strategies += load_custom()
        except Exception:
            pass
    return strategies


def get_strategy(strategy_id: str, path: Path | None = None) -> Strategy | None:
    for s in load_strategies(path):
        if s.id == strategy_id:
            return s
    return None
