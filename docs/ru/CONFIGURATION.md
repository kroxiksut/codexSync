# Конфигурация

[English](../en/CONFIGURATION.md) · **Русский** · [中文](../zh/CONFIGURATION.md)

[← Документация](README.md)

Всё, что делает codexSync, определяется одним `config.toml`. Создайте его
командой `codexsync init-config` или на экране «Первый запуск» и правьте вручную
или на экране «Настройки». Полный шаблон с комментарием к каждому ключу —
[config.example.toml](../../config.example.toml) (комментарии на английском).

Каждая пишущая команда дополнительно проверяет, что конфигурация не просит
ничего, что запрещают правила безопасности; такая конфигурация отклоняется с
кодом `4`.

## Пути и рабочее пространство

```toml
[identity]
machine_id = "desktop"

[paths]
workspace_root_dir = "D:/Cloud/codexSync"
local_state_dir = "C:/Users/me/.codex"
cloud_root_dir = "${workspace_root}/sync"
backup_dir = "${workspace_root}/backups"
temp_dir = "${workspace_root}/.tmp"
```

- **`machine_id`** — уникальное и постоянное имя. На него ссылаются резервные
  копии, снимки, планы и правила соответствия путей на другой машине; две машины
  под одним именем смешают свои данные.
- **`workspace_root_dir`** — папка, которую облачный клиент синхронизирует между
  машинами. `${workspace_root}` в любом другом пути означает её.
- **`local_state_dir`** — собственный каталог состояния Codex. Он читается,
  пишется только во время холодной операции и никогда не создаётся.
- **`cloud_root_dir`** — зеркало состояния в облачной папке.
- **`backup_dir`** — резервные копии, сделанные перед заменой.
- **`temp_dir`** — здесь лежат замок операции и журнал мутации, сюда же
  `restore` распаковывает снимок. Сама копия готовится и проверяется в папке
  своего назначения: атомарная замена работает только в пределах одного тома, а
  `.codex` часто лежит не на том диске, где облачная папка.

Относительный путь отсчитывается от `workspace_root_dir`, а если рабочее
пространство не задано — от папки, где лежит `config.toml`.

## Секции

| Секция | Что задаёт | Подробно |
|---|---|---|
| `[sync]` | Сравнение, направление, удаления, проверочный запуск по умолчанию | [Синхронизация](SYNC.md) |
| `[targets]` | `include_roots`: что внутри `.codex` участвует в `sync` | [Синхронизация](SYNC.md#что-синхронизируется) |
| `[filters]` | `exclude_globs`: что не копируется никогда | [Синхронизация](SYNC.md#что-синхронизируется) |
| `[conflict]` | `policy` для файла, изменённого с обеих сторон | [Синхронизация](SYNC.md#конфликты) |
| `[backup]` | Срок хранения и формат резервных копий | [Восстановление](RECOVERY.md#резервные-копии) |
| `[guardian]` | Хранилище снимков, опрос, пороги сокращения, срок хранения | [Хранитель](GUARDIAN.md#настройки) |
| `[semantic]` | Пакеты конфликтов, сжатие зеркала | [Сессии](SESSIONS.md#облачное-зеркало) |
| `[[path_mappings]]` | Как путь на одной машине соответствует пути на другой | [ниже](#path_mappings) |
| `[process_detection]` | Какие процессы означают «Codex запущен» | [ниже](#process_detection) |
| `[scheduler]` | Безопасное действие по расписанию | [ниже](#автоматизация) |
| `[logging]` | Уровень, формат, ротация, срок хранения журналов | [ниже](#журналы) |
| `[safety]` | Неизменно: Codex должен быть остановлен, сомнение — отказ | не редактируется |
| `[state]` | Где лежит манифест синхронизации | — |

## `[[path_mappings]]`

При переезде на другую машину обычно меняются пути: на настольном компьютере
проект лежит в `D:/Projects/atlas`, а на ноутбуке — в `C:/Work/atlas`. Правило
говорит об этом:

```toml
[[path_mappings]]
rule_id = "desktop-projects-to-laptop"
source_machine = "desktop"
target_machine = "laptop"
from = "D:/Projects"
to = "C:/Work"
# case_sensitive = false   # необязательно
```

`rule_id` должен быть уникальным. Правила используют `chats`, `repair-projects`
и рабочий набор `sessions`; в файлы Codex они не записываются, и сам Codex их не
читает. См. [Проекты и чаты](PROJECTS.md).

## `[process_detection]`

```toml
[process_detection]
process_names = ["codex.exe", "codex", "codex-app-server"]
grace_period_seconds = 2

[process_detection.background_process_names]
windows = ["codex-windows-sandbox", "codex-windows-sandbox-setup", "codex-windows-sandbox-service", "codex-command-runner"]
macos = ["ChatGPT.app/Contents/MacOS/", "codex-app-server", "codex-execve-wrapper", "codex-code-mode-host"]
linux = ["/usr/lib/chatgpt/", "codex-app-server", "codex-linux-sandbox", "codex-execve-wrapper", "codex-code-mode-host"]
```

Имена сравниваются целиком, никогда как подстроки. Запись, содержащая `/`, —
маркер пути, который сравнивается с путём процесса: настольная сборка на macOS
называется `ChatGPT`, и просто `ChatGPT` совпало бы и с обычным приложением
ChatGPT.

Ключ `allow_terminate_if_running` и остальные `terminate_*` остались от 0.1.
codexSync никогда не завершает Codex, и `allow_terminate_if_running = true`
отклоняется.

## Автоматизация

Задача по расписанию — применённая секция `[scheduler]`. Настраивайте её здесь
или в окне на вкладке «Настройки → Автоматизация», но не в планировщике ОС
напрямую.

```toml
[scheduler]
enabled = true
mode = "guardian_snapshot"   # guardian_snapshot | preflight | sync_dry_run
interval_seconds = 300       # не меньше 60
run_at_login = true
startup_delay_seconds = 0
jitter_seconds = 0
```

```powershell
codexsync -c config.toml automation status   # конфигурация, точная команда, состояние задачи ОС; ничего не меняет
codexsync -c config.toml automation apply    # установить или обновить задачу; при enabled = false — удалить
codexsync -c config.toml automation remove   # удалить задачу; config.toml не меняется
codexsync -c config.toml automation run      # выполнить настроенное действие один раз, сейчас
```

- Задача выполняет только безопасное действие: `guardian_snapshot`, `preflight`
  или `sync_dry_run`. Запись, ремонт, перенос, восстановление или откат по
  расписанию запустить нельзя, а проверочный запуск синхронизации по расписанию
  всё равно отклоняется при открытом Codex.
- Это задача уровня пользователя — Планировщик заданий в Windows, LaunchAgent в
  macOS, `systemd --user` в Linux, — а не служба.
- `automation run` завершается так же, как действие: `0` при успехе или если уже
  работает другой хранитель, `2` при карантине, `3`, если во время
  `sync_dry_run` запущен Codex, `5` при сбое.

**Устарело:** `guardian scheduler` и `scripts/scheduler/{windows,macos}` хранят
настройки расписания вне `config.toml` и будут удалены. Если вы ставили задачу
этими скриптами, сначала удалите её ими же, чтобы не работали две задачи.

## Журналы

```toml
[logging]
level = "INFO"            # DEBUG | INFO | WARNING | ERROR
file = "${workspace_root}/logs/codexsync.log"
format = "text"           # text | json | logfmt
retention_days = 7
archive_mode = "zip"      # zip | text
max_file_size_mb = 10
```

- Файлы журнала — по дням и с именем машины:
  `<stem>-<machine>-YYYY-MM-DD[.N].log`, в UTF-8.
- Файл ротируется по дню и по размеру; старые файлы архивируются в `.zip`
  (`archive_mode = "zip"`) или остаются текстом и удаляются через
  `retention_days`.
- Каждое опасное действие записывается отдельно: создана резервная копия,
  перезапись, пропуск.
