# TODOS

Отложенные идеи и работа, которая не попала в текущий релиз.

## Deferred from /plan-ceo-review (2026-04-27)

### `/replay <N>` — повторить запись из cli_history (E7)

**Что:** REPL-команда `/replay 3` перезапускает третью с конца команду из
`cli_history`. С опциональным `--target @newuser` для переадресации на
другого пользователя.

**Зачем:** Iterative OSINT: «прогони то же что я делал 3 шага назад, но на
новом таргете». Сейчас оператор копирует и вставляет вручную.

**Pros:**
- 30 минут CC.
- Естественное продолжение `/history`, который уже есть в плане.
- Заметно ускоряет recon-pass через список целей.

**Cons:**
- Двусмысленность с side-effect командами (повторный `/purge`?). Решение —
  whitelist «replayable» команд (info, posts, followers, mutuals, etc).
- Нужно решить как replay'ить таргет: использовать тот же что был, или
  текущий активный, или явно через `--target`. Дополнительный contract.

**Context:** v0.1 хранит cli_history в sqlite (`record_command(cmd,
target, ts)`). Нужно расширить запись: сохранять полный argv (не только
cmd+target) — иначе replay не сможет восстановить флаги. Это маленькая
schema-миграция: добавить колонку `argv_json TEXT` в `cli_history`.

**Effort:** S (human ~4h / CC ~30 мин)
**Priority:** P3
**Depends on:** v0.1 history-store должен сохранять полный argv.

### Plugin API через entry-points

**Что:** Pyproject `[project.entry-points."insto.commands"]` —
сторонние пакеты могут регистрировать свои команды. Аналогично для
`[project.entry-points."insto.backends"]`.

**Зачем:** Позволяет community / приватным командам расширять `insto` без
форка. Платформа-effect.

**Pros:**
- Долгосрочный leverage: чужие OSINT-эксперименты живут как pip-пакеты.
- Не блокирует core-разработку.

**Cons:**
- Без живого второго плагина contract — гадание. Wait для реального запроса.
- Plugin trust: chytрый плагин может exfiltrate токены — нужна модель
  доверия (только pip-installed, без auto-discovery из cwd).

**Context:** Откладывается до момента, когда появится первый реальный
сторонний user-case (например, кто-то напишет `insto-tiktok-backend`).
Тогда же делаем plugin contract.

**Effort:** M (human ~2 дня / CC ~3-4h)
**Priority:** P3
**Depends on:** живой второй backend / команда от стороннего автора.

### Daemon-режим для `/watch` (persistent, multi-process)

**Что:** `insto daemon start` — фоновый процесс с persistent watches,
auto-resume на старт, IPC через unix socket для управления из REPL/CLI.

**Зачем:** Long-running OSINT мониторинг переживает закрытие ноутбука и
ребуты. Спека уже отметила как v0.2.

**Effort:** L (human ~1 неделя / CC ~6-8h)
**Priority:** P2
**Depends on:** v0.1 (in-session watches) уже отлажены.

### At-rest шифрование `~/.insto/store.db`

**Что:** SQLCipher вместо stock sqlite + GPG/age-encrypted резервные
копии snapshots.

**Зачем:** Multi-operator scenarios, sensitive targets, compliance.

**Effort:** M (human ~2-3 дня / CC ~3h)
**Priority:** P2
**Depends on:** реальный multi-operator use-case.

### Multi-platform OSINT (TikTok, Bluesky, Threads)

**Что:** ABC уже переименована в `OSINTBackend`. Конкретные backends
(`TikTokBackend`, `BlueskyBackend`) — отдельные пакеты или плагины.

**Зачем:** Tool становится универсальным OSINT CLI, не Instagram-only.

**Effort:** Per-backend XL (human ~2-4 недели / CC ~10-15h на бэкенд)
**Priority:** P3
**Depends on:** живой запрос на конкретную платформу + способ получить
данные (paid API или OSINT-friendly эндпоинт).
