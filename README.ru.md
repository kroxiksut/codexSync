<p align="center">
  <img src="assets/brand/brand-mark.png" width="96" alt="CodexSync">
</p>

<h1 align="center">codexSync</h1>

<p align="center">
  Переносит локальное состояние Codex — проекты, привязки чатов и историю
  сессий — между личными машинами через облачную папку.
</p>

<p align="center">
  <a href="https://github.com/kroxiksut/codexSync/actions/workflows/ci.yml"><img src="https://github.com/kroxiksut/codexSync/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/kroxiksut/codexSync/releases"><img src="https://img.shields.io/github/v/release/kroxiksut/codexSync?include_prereleases&sort=semver" alt="Release"></a>
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white" alt="Python 3.11 | 3.12 | 3.13">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey" alt="Платформы: Windows | macOS">
  <img src="https://img.shields.io/badge/runtime%20dependencies-0-brightgreen" alt="Внешних зависимостей: 0">
  <img src="https://img.shields.io/badge/GUI-PySide6%2C%20optional-41CD52?logo=qt&logoColor=white" alt="Окно: PySide6, необязательно">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0--or--later-blue" alt="Лицензия: GPL-3.0-or-later"></a>
</p>

<p align="center">
  <a href="./README.md">English</a> · <b>Русский</b> · <a href="./README.zh.md">中文</a>
</p>

> [!IMPORTANT]
> На практике проверен только перенос Windows → Windows. macOS поддержан в коде
> и в CI, но полный перенос между настоящими macOS-машинами ещё не проверялся.

<p align="center">
  <a href="docs/ru/GUI.md"><img src="docs/screenshots/ru/01-overview.png" width="85%" alt="Окно CodexSync: обзор"></a>
</p>

## Что делает

- **Синхронизирует** каталог состояния Codex через любую облачную папку —
  сначала резервная копия, и только при закрытом Codex.
  → [Синхронизация](docs/ru/SYNC.md)
- **Защищает глобальное состояние** проверенными снимками, которые делаются
  *при работающем* Codex и пишутся только вне `.codex`.
  → [Хранитель снимков](docs/ru/GUARDIAN.md)
- **Переносит историю сессий между машинами**, классифицируя каждую ветку;
  расхождение никогда не сливается и не решается по времени.
  → [Сессии](docs/ru/SESSIONS.md)
- **Находит чат и кладёт его под проект**, чинит привязки после переезда на
  другую машину, переносит файлы проекта.
  → [Проекты и чаты](docs/ru/PROJECTS.md)
- **Восстанавливается после прерванной записи** — блокировка, журнал,
  проверенная резервная копия.
  → [Резервные копии и восстановление](docs/ru/RECOVERY.md)

Ни интеграции во внутренности Codex, ни синхронизации в реальном времени:
[чего не делает](docs/ru/README.md#чего-не-делает).

## Установка

```powershell
pip install ".[gui]"     # командная строка и окно; только CLI — `pip install .`
codexsync-gui            # или: codexsync -c config.toml doctor
```

Сборки для Windows без Python — в
[Releases](https://github.com/kroxiksut/codexSync/releases). Подробности и
первый запуск: [установка и начало работы](docs/ru/README.md#установка).

## Документация

| | |
|---|---|
| [Обзор](docs/ru/README.md) | Как это работает, установка, первый запуск, принципы, платформы |
| [Окно](docs/ru/GUI.md) | Все одиннадцать экранов со скриншотами |
| [Командная строка](docs/ru/CLI.md) | Все команды, общие параметры, коды выхода |
| [Конфигурация](docs/ru/CONFIGURATION.md) | `config.toml` по секциям, автоматизация |
| [Синхронизация](docs/ru/SYNC.md) | `plan` и `sync`: сравнение, конфликты, направление, удаления |
| [Хранитель снимков](docs/ru/GUARDIAN.md) | Снимки глобального состояния, карантин, восстановление, новая база |
| [Сессии](docs/ru/SESSIONS.md) | Ветки сессий между машинами, рабочий набор, зеркало, индекс |
| [Проекты и чаты](docs/ru/PROJECTS.md) | Привязки чатов, ремонт после переезда, перенос проекта |
| [Резервные копии и восстановление](docs/ru/RECOVERY.md) | Резервные копии, восстановление, прерванные операции |

Все страницы в одном месте: [docs/](docs/README.ru.md). Для разработчиков
(на английском): [документы разработчика](docs/dev/README.md). История изменений:
[CHANGELOG.md](./CHANGELOG.md).

## Статус

0.2 — первый выпуск с окном (`codexsync[gui]` или `CodexSync.exe`) рядом с
командной строкой. Некоторые возможности намеренно не используются, пока
контролируемый эксперимент не зафиксирует поведение Codex, — см.
[что пока не подтверждено](docs/ru/README.md#что-пока-не-подтверждено).

## Лицензия

Двойное лицензирование: открытая лицензия `GPL-3.0-or-later`
([LICENSE](./LICENSE)) и коммерческий путь, описанный в
[COMMERCIAL_LICENSE.md](./COMMERCIAL_LICENSE.md). Вклад принимается на условиях
[CONTRIBUTING.md](./CONTRIBUTING.md) и [CLA.md](./CLA.md).
