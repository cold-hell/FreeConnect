# Сторонние компоненты

## Flowseal/tg-ws-proxy — ядро обхода Telegram

FreeConnect использует код проекта [tg-ws-proxy](https://github.com/Flowseal/tg-ws-proxy)
(локальный MTProto-прокси, заворачивающий трафик Telegram в WebSocket-канал веб-входа
Telegram, с резервом через Cloudflare). Файлы лежат в `freeconnect/tgws/` без изменений;
подробности и порядок обновления — в `freeconnect/tgws/UPSTREAM.md`.

Взятая версия: **1.11.2** (тег `v1.11.2`).

```
MIT License

Copyright (c) 2026 Flowseal

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Копия лицензии также лежит рядом с кодом: `freeconnect/tgws/LICENSE-tg-ws-proxy.txt`.

## Прочие компоненты

FreeConnect использует также [zapret](https://github.com/bol-van/zapret) (`winws.exe` +
WinDivert), наборы параметров [Flowseal/zapret-discord-youtube](https://github.com/Flowseal/zapret-discord-youtube)
и [sing-box](https://github.com/SagerNet/sing-box). Они не входят в этот репозиторий:
бинарники рантайма разворачиваются на машине пользователя, а списки и стратегии
подтягиваются по сети.
