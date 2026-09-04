# codexSync

Open-source утилита для синхронизации локального состояния Codex между персональными машинами через облачно-синхронизируемую папку.

> [!IMPORTANT]
> Практическая проверка сценария на реальных машинах пока проведена только для Windows-to-Windows.
> Поддержка macOS в коде и CI есть, но end-to-end handoff на реальных macOS-машинах ещё не валидирован.
## Зачем

Разработчику может понадобиться продолжать работу с Codex на другой машине, не теряя локальное состояние сессии.

## Что делает

* Синхронизирует локальную директорию состояния Codex
* Работает только после закрытия процесса Codex
* Использует любую облачно-синхронизируемую папку (Dropbox, OneDrive, Syncthing и т. д.)

## Что НЕ делает

* Нет интеграции с внутренними механизмами Codex
* Нет использования API
* Нет извлечения токенов
* Нет перехвата сетевого трафика
* Нет синхронизации в реальном времени
* Нет проверок процесса/состояния облачного клиента
* Нет проверок свободного места в облачном/сетевом хранилище

## Принципы дизайна

* Просто и предсказуемо
* Безопасно (без повреждения данных)
* Удобно для офлайна
* Сначала резервная копия
* Windows-first

## Как это работает (MVP)

1. Определяем, запущен ли Codex
2. Если не запущен:

   * Сравниваем локальное и облачное состояние
   * Синхронизируем более новые файлы
   * Создаём резервную копию перед перезаписью

## Политика конфликтов

`conflict.policy` поддерживает:

- `manual_abort`: сообщить о конфликте и остановиться (по умолчанию)
- `prefer_cloud`: автоматически взять облачную версию
- `prefer_local`: автоматически взять локальную версию
- `prefer_newer_mtime`: автоматически взять сторону с более новым mtime

## Параметры синхронизации

`sync.compare` определяет стратегию сравнения файлов:

- `mtime` (по умолчанию): сравнение по `size + mtime`
- `mtime_hash_fallback`: быстрый путь по `size + mtime`, но при равенстве/близких значениях (в пределах tolerance) дополнительно сравнивается хэш содержимого (SHA-256)

`sync.equal_mtime_action` определяет поведение, когда `mtime` файлов совпадает (с учетом `sync.time_tolerance_seconds`), но содержимое отличается:

- `skip`: не копировать (по умолчанию)
- `prefer_local`: копировать локальную версию в облако
- `prefer_cloud`: копировать облачную версию в локальное состояние
- `manual_abort`: пометить как конфликт и остановить синхронизацию в режиме `manual_abort`

`sync.session_mode` определяет область синхронизации для `sessions`:

- `all`: синхронизировать все содержимое `sessions` (поведение по умолчанию)
- `last_date_only`: синхронизировать только папку с самой новой датой в `sessions`
  - поддерживаемые структуры: `sessions/YYYY-MM-DD/...` и `sessions/YYYY/MM/DD/...`
  - если структура папок с датами не обнаружена, codexSync пишет предупреждение в лог и синхронизирует все файлы `sessions`

## Резервные копии

- `backup.compression` поддерживает:
  - `none` (по умолчанию): backup snapshot хранится как дерево директорий
  - `zip`: backup snapshot хранится как один `.zip` файл
- `restore` поддерживает оба формата snapshot и при отсутствии `--from` выбирает самый новый по `mtime`.

## Логирование

- Уровни: `DEBUG|INFO|WARNING|ERROR`
- Форматы (через конфиг): `text|json|logfmt`
- Правила ротации/лимита размера/retention/архивации одинаково применяются ко всем форматам (`text`, `json`, `logfmt`)
- UTF-8 для всех лог-файлов
- Ежедневные файлы логов с машиной (`<stem>-<machine>-YYYY-MM-DD[.N].log`)
- Ротация по дате и по размеру (`logging.max_file_size_mb`, по умолчанию `10`)
- Очистка по сроку хранения (`logging.retention_days`, по умолчанию `7`)
- Режим хранения старых логов (`logging.archive_mode`):
  - `zip` (по умолчанию): архивировать старые/ротированные логи в `.zip`
  - `text`: хранить старые логи обычными текстовыми файлами

## Платформы и CI

- На текущем этапе поддержка runtime ориентирована на Windows.
- Поддержка macOS допускается в рамках текущего scope проекта (цель: Apple Silicon).
- Поддержка Linux runtime намеренно вне MVP.
- CI сейчас запускается на:
  - `windows-latest`
  - `macos-latest`

## Команды CLI

Запуск из корня проекта:

```powershell
python -m codexsync -c config.toml <command>
```

Генерация `config.toml` из шаблона, встроенного в пакет:

```powershell
python -m codexsync init-config
```

Свой путь для файла конфигурации:

```powershell
python -m codexsync init-config --output D:\codexSync\config.toml
```

Перезапись существующего файла:

```powershell
python -m codexsync init-config --output D:\codexSync\config.toml --force
```

Проверка конфига:

```powershell
python -m codexsync -c config.toml validate
```

Построение плана (без изменений):

```powershell
python -m codexsync -c config.toml plan
```

Построение плана с выводом процессов (`--verbose`):

```powershell
python -m codexsync -c config.toml -v plan
```

Пробный запуск синка (без записи):

```powershell
python -m codexsync -c config.toml sync --dry-run
```

Пробный запуск синка с выводом процессов (`--verbose`):

```powershell
python -m codexsync -c config.toml -v sync --dry-run
```

Реальный синк (с записью):

```powershell
python -m codexsync -c config.toml sync --apply
```

Типовой запуск на другой машине после handoff:

```powershell
python -m codexsync -c config.toml sync --apply
```

Восстановление из последнего backup snapshot в локальное состояние:

```powershell
python -m codexsync -c config.toml restore --apply
```

Восстановление из конкретного backup snapshot:

```powershell
python -m codexsync -c config.toml restore --from <snapshot_dir_name> --apply
```

Восстановление из конкретного zip snapshot:

```powershell
python -m codexsync -c config.toml restore --from <snapshot_name.zip> --apply
```

Восстановление в cloud-цель вместо локальной:

```powershell
python -m codexsync -c config.toml restore --target cloud --apply
```

Проверка восстановления без записи:

```powershell
python -m codexsync -c config.toml restore --dry-run
```

Справка по командам:

```powershell
python -m codexsync -h
```

Поведение завершения процесса:

- На Windows при `sync`/`restore`, если Codex ещё запущен, codexSync может завершать процессы Codex перед продолжением.
- По умолчанию включено ручное подтверждение через GUI (`process_detection.manual_terminate_confirmation = true`).
- Список фоновых процессов задаётся по ОС в `process_detection.background_process_names`:
  - `windows = ["codex-windows-sandbox"]`
  - `macos = []`
  - `linux = []`
- Канал подтверждения задаётся параметром `process_detection.terminate_confirmation_mode = "gui" | "console"` (по умолчанию `gui`).
- Все вопросы в GUI и консоли выводятся на английском языке.
- Если обнаружен `codex-windows-sandbox`, codexSync сообщает, что Codex ещё запущен, и завершает работу с кодом `3` (без auto-terminate).
- Если `codex.exe` запущен, но `codex-windows-sandbox` не обнаружен, codexSync спрашивает, завершить ли Codex и продолжить.
- Принудительно включить ручное подтверждение можно флагом:

```powershell
python -m codexsync -c config.toml --manual-terminate-confirmation sync --apply
```

- Можно принудительно отключить ручное подтверждение только для текущего запуска:

```powershell
python -m codexsync -c config.toml --auto-terminate-without-confirmation sync --apply
```

- На macOS/Linux сохраняется прежнее поведение: если Codex запущен, срабатывает safety precondition.
- `--verbose` работает для `plan`, `sync --dry-run`, `sync --apply`, `restore --dry-run` и `restore --apply`; в лог выводятся отслеживаемые процессы с PID и именем.
- В verbose-режиме на Windows codexSync выводит только:
  - запущен ли `codex.exe`,
  - обнаружен ли `codex-windows-sandbox`,
  - подпроцессы внутри `codex.exe` (PID/имя/cmd).

Коды завершения для automation:

- `3` Codex запущен / sandbox обнаружен / пользователь отклонил завершение
- `5` завершение подтверждено, но не удалось завершить процесс в таймаут (fail-safe)

## Коды завершения CLI

- `0` успех
- `1` внутренняя ошибка выполнения
- `2` обнаружен конфликт (требуется ручное разрешение)
- `3` Codex запущен (нарушен precondition cold sync)
- `4` ошибка аргументов CLI или конфигурации
- `5` безопасная остановка (`fail-safe`)

## Обязательный протокол работы

Инструмент предполагает строгий порядок передачи работы между машинами:

1. Закрыть Codex на машине A.
2. Дождаться, пока облачная синхронизация полностью доставит изменения с машины A.
3. Запустить codexSync на машине B.
4. Запускать Codex на машине B только после завершения синхронизации.
5. После синхронизации файлов на машине B нужно заново войти в Codex.

Важно: по лицензионным ограничениям OpenAI токены авторизации не переносятся через codexSync.

Проект намеренно не проверяет статус синхронизации облачного провайдера, состояние процесса облачного клиента и свободное место в облачном/сетевом хранилище. Это зона ответственности пользователя.

## Скрипты настройки планировщика

В репозитории есть редактируемые шаблоны для настройки планировщика:

- Windows Task Scheduler:
  - `scripts/scheduler/windows/task.config.ps1`
  - `scripts/scheduler/windows/install-task.ps1`
  - `scripts/scheduler/windows/remove-task.ps1`
  - подробная инструкция: [scripts/scheduler/windows/README.md](./scripts/scheduler/windows/README.md)
- macOS launchd (LaunchAgent):
  - `scripts/scheduler/macos/launchd.config.sh`
  - `scripts/scheduler/macos/install-launchd.sh`
  - `scripts/scheduler/macos/uninstall-launchd.sh`
  - подробная инструкция: [scripts/scheduler/macos/README.md](./scripts/scheduler/macos/README.md)

Установка на Windows:

```powershell
cd scripts/scheduler/windows
# 1) Отредактируйте task.config.ps1
.\install-task.ps1
```

Удаление на Windows:

```powershell
cd scripts/scheduler/windows
.\remove-task.ps1
```

Установка на macOS:

```bash
cd scripts/scheduler/macos
# 1) Отредактируйте launchd.config.sh
chmod +x install-launchd.sh uninstall-launchd.sh run-codexsync.sh
./install-launchd.sh
```

Удаление на macOS:

```bash
cd scripts/scheduler/macos
./uninstall-launchd.sh
```

Важно:

- Эти скрипты только регистрируют задачи планировщика, а не системные службы.
- Во время проверки оставляйте `MODE="dry-run"`, переключайтесь на `apply` только когда готовы.
- Протокол cold sync остаётся обязательным: codexSync должен запускаться только когда Codex не запущен.

## Статус

MVP (готово к публичному репозиторию и тестированию сообществом)

## Публикация

См. чеклист релиза: [docs/PUBLISHING.md](./docs/PUBLISHING.md)
Release notes: [CHANGELOG.md](./CHANGELOG.md)

## Лицензирование

В проекте используется двойное лицензирование:

- Open-source лицензия: `GPL-3.0-or-later` (см. [LICENSE](./LICENSE))
- Коммерческое лицензирование: см. [COMMERCIAL_LICENSE.md](./COMMERCIAL_LICENSE.md)

Условия участия и CLA:

- [CONTRIBUTING.md](./CONTRIBUTING.md)
- [CLA.md](./CLA.md)

## Режимы doctor/preflight

Команды `doctor` и `preflight` эквивалентны и запускают диагностику перед синхронизацией.

```powershell
python -m codexsync -c config.toml doctor
python -m codexsync -c config.toml preflight
```

Что проверяется:
- загрузка конфига и инициализация runtime-путей;
- читаемость local/cloud/backup/temp и что это каталоги;
- состояние процесса Codex (precondition cold sync);
- совместимость версии manifest (`state.data_version`);
- аудит каталога сессий (невалидные/неоднозначные сессии, коды графа);
- read-only аудит SQLite;
- orphan temp-файлы в `paths.temp_dir`.

`doctor`/`preflight` не пишут на диск: проверка дрейфа mtime из 0.1 удалена,
потому что создавала probe-файл внутри state-каталога Codex.

Коды возврата:
- `0` — нет FAIL-проверок (PASS/WARN);
- `5` — есть хотя бы одна FAIL-проверка (`fail-safe`).


