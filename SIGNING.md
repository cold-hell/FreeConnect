# Подпись кода (code signing)

Цель — чтобы Windows SmartScreen не пугал друзей «Неизвестный издатель», а
антивирусы реже трогали `winws.exe`. Ниже — бесплатный путь через **SignPath
Foundation** (программа бесплатной подписи для open-source).

Всё, что можно было подготовить заранее, уже сделано:
- ✅ Репозиторий публичный, лицензия **MIT** (`LICENSE`) — обязательное условие.
- ✅ CI на GitHub Actions (`.github/workflows/tests.yml`) — сборка проверяема.

## Что осталось (только это может сделать человек)

Эти шаги требуют твоего GitHub-аккаунта и ручного одобрения SignPath — автоматизировать нельзя.

1. **Подать заявку** в SignPath Foundation: https://signpath.org/apply
   Готовый текст для формы (можно вставить как есть):

   > **Project name:** FreeConnect
   > **Repository:** https://github.com/cold-hell/FreeConnect
   > **License:** MIT
   > **Description:** FreeConnect is a free, open-source Windows GUI wrapper
   > around the zapret DPI-bypass engine that restores access to Discord and
   > YouTube for users in Russia. It auto-selects a working bypass strategy and
   > is distributed to non-technical users as a signed installer. Code signing
   > is needed so SmartScreen does not block first-time users.
   > **Programming language:** Python (packaged with PyInstaller) + Inno Setup installer.

2. Дождаться ответа SignPath (ревью, обычно несколько дней). Они заведут
   организацию/проект и дадут `SIGNPATH_ORGANIZATION_ID` и токен API.

3. **Подписать установщик** — простой путь (рекомендую на старте):
   в веб-портале SignPath загрузить собранный `installer/Output/FreeConnect-Setup.exe`,
   получить подписанный файл и заменить им ассет релиза
   (`gh release upload vX.Y.Z FreeConnect-Setup.exe --clobber`). Никаких изменений
   в CI не требуется.

> **Автоматизация (позже, опционально):** чтобы подписывать прямо в CI через
> `signpath/github-action-submit-signing-request`, сначала нужно сделать сборку
> установщика воспроизводимой в GitHub Actions. Сейчас она собирается локально,
> т.к. бинарники рантайма winws не лежат в репозитории (`.gitignore`). Это
> отдельная задача — не блокирует ручную подпись выше.

## Платная альтернатива (если ждать одобрение не хочется)

**Azure Trusted Signing** — ~$10/мес, работает и для физлиц, интегрируется с
SmartScreen-репутацией. Быстрее и надёжнее по срокам, но платно и тоже требует
настройки твоего аккаунта Azure.
