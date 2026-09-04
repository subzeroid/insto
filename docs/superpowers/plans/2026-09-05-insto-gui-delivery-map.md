# insto-gui: delivery map

Согласованная пользователем [спека](../specs/2026-09-05-insto-gui-design.md) — источник продуктовых требований. Подтверждение письменного документа: «да», 2026-09-05.

Результат остаётся прежним: установить app, ввести HikerAPI-токен, добавить username и получать наблюдения без терминала или внешнего Python. Все этапы GUI выполняются на Astra.

## Почему несколько планов

Здесь есть три различных риска: переносимость Python, корректность управления существующим сервисом и пользовательский интерфейс. Сначала проверяем переносимость отдельным законченным developer-инструментом. Этот результат не называется установщиком или готовым GUI. Затем проверяем настоящий app bundle до строительства полного интерфейса.

В этой итерации детально расписаны **два начальных исполняемых плана**, а не вся реализация v1:

1. [C0 — desktop foundation в insto](2026-09-05-insto-desktop-foundation.md): строгий handshake, строгая проверка credential на backend-уровне и совместимый запуск службы без записи bytecode.
2. [P0 — переносимость runtime для insto-gui](2026-09-05-insto-gui-runtime-proof.md): фиксированные upstream-артефакты, wheel-only сборка дерева, manifest и проверка после перемещения, включая изолированный native smoke.

Следующие фазы ниже имеют критерии выхода, но **не являются готовыми implementation plans**. Их кодовые шаги составляются после результатов C0/P0. Это явное разбиение работы, а не исключение функций из согласованной спеки.

```text
C0: совместимый core foundation
  ↓
P0: переносимый runtime и native service proof
  ↓
C1: setup / credential replacement / service recovery
  ↓
P1: минимальный Tauri app → подготовка runtime → служба после выхода
  ↓
C2: readonly history / revision-safe watches
  ↓
G1: token-only onboarding и компоновка A
  ↓
G2: runtime update / existing home / восстановление
  ↓
R1: signed + notarized DMG, чистые Mac, релиз
```

## Карта покрытия спеки

| Требования | Этап и проверяемый результат |
| --- | --- |
| Единственный Python backend, короткоживущий JSON bridge | C0: strict envelope и `hello`; C1/C2: операции. Нет SDK или SQLite в Rust. |
| Независимая поставка CPython и insto | P0: собранное перемещаемое дерево; P1: настоящее приложение публикует его автоматически. |
| Runtime hash, trusted bundle, symlink, lock, atomic publish, 120 секунд | P0: developer manifest и отказ от небезопасного payload; P1: отдельный hardened Rust publisher с межпроцессной блокировкой и тестами crash/race/disk-full. |
| Отсутствие сетевой установки у пользователя | P1: сеть заблокирована, runtime готовится из ресурсов приложения. Сетевые шаги P0 существуют только на build machine. |
| Один экран токена | C0: строгий backend primitive; C1: безопасное сохранение и setup; G1: единственная обязательная форма. |
| Invalid token / quota / network / unknown response | C0: typed errors; C1: безопасные protocol codes и сохранность прежнего токена; G1: различимые UI-состояния. |
| Применение замены токена в работающем daemon | C1: проверка → остановка своей службы → atomic config → запуск, защищённый recovery record и rollback; stopped остаётся stopped, внешний CLI требует принятия управления. |
| Никаких секретов в argv/logs/plist/frontend storage | C1 + P1 + G1: stdin transport, redaction до запроса, atomic 0600 config, deny-by-default IPC и sentinel tests. |
| Отдельная служба, stop/start/repair, desired state | C1 + P1: настоящий launchd, остановка уважается, loaded-but-exited восстанавливается без guessed PID. |
| 3 активных, 300 секунд, CAS revision и pause гонки | C2: транзакционные доменные операции и stale revision tests; G1: понятный conflict. |
| Readonly snapshots, неоднозначный username, pagination, retention | C2: readonly транзакции, stable ID order, bounded cursors, fixture с rename/reused username; G1: выбор истории и compare. |
| Компоновка A, четыре раздела, empty/loading/error/stale | G1: Vue mock-IPC tests + реальные bridge integrations + визуальная проверка. |
| Close GUI, sleep/login, отказ background item | P1/G1/R1: новый tick после выхода, честные ограничения состояния и OS-specific evidence. |
| Обновление без overwrite и rollback | G2: две runtime версии, failure injection на каждом переходе, stopped остаётся stopped. |
| Existing home, ownership transfer, aiograpi refusal | G2: дополнительные настройки, явное подтверждение, неизвестный владелец readonly. Onboarding не читает `~/.insto`. |
| Удаление app не оставляет ложного обещания uninstall | G2: явное отключение службы, сохранение данных, понятная инструкция перед удалением app. |
| arm64 / x86_64 / minimum OS / Developer ID | P0 определяет переносимость; P1 проверяет nested signing; R1 фиксирует фактически проверенную матрицу и скачанный quarantined DMG. |
| Подписи и окончательный hash manifest | P1/R1: вложенная подпись → final manifest/build-id → внешняя подпись; проверяется уже подписанное скопированное дерево. Unsigned P0 manifest не переиспользуется после signing. |

## Инженерные решения, которые уже можно зафиксировать

- C0 не рекламирует ещё не реализованные operations в capabilities. Один `protocol_version=1` не означает, что весь интерфейс v1 уже существует.
- Существующий CLI-сервис использует точное сравнение plist. Нельзя просто глобально вставить `-B`: это сломает повторную установку и удаление старой регистрации. C0 сохраняет legacy default и разрешает при удалении только две точные принадлежащие сервису формы — с `-B` и без него.
- Проверка HikerAPI-токена не заменяет мягкий REPL balance refresh. Новый строгий метод не скрывает 401, сетевые ошибки и неизвестную структуру ответа.
- Developer proof не внедряет `fake`-операцию в production desktop protocol. Изолированные native тесты используют уже существующий fake backend через конфигурацию тестового home.
- P0 не проверяет Gatekeeper, пользовательский onboarding или Rust publisher. Они остаются обязательными P1/R1 gates; до P1 нельзя называть установку подтверждённой.
- `uv.lock` текущей основы содержит metadata версии проекта 0.7.17 при wheel 0.7.20. P0 экспортирует только зависимости с `--frozen --no-emit-project`; wheel берёт из текущего commit. Это не повод менять lock в документационной ветке.

## Переход к исполнению

Исполнение рекомендуем inline на Astra, небольшими проверяемыми коммитами; Sol не используется. Для C0 создаётся отдельная implementation worktree от этой документационной ветки. P0 выполняется в новом отдельном `insto-gui`; существующий `insta-dl-gui` остаётся нетронутым.

Инженерное ревью всей спеки, C0/P0 и границ последующих фаз выполнено: [решения, доказательства и обязательная матрица тестов](2026-09-05-insto-gui-engineering-review.md). C1 предшествует P1: `hello` из C0 недостаточно для запуска службы через desktop protocol. P0 по-прежнему проверяет переносимость до расширения контрактов и создания app. Это исправление зависимости, не перенос полного UI перед packaging proof.

Локальные коммиты разрешены обычным implementation workflow. Публикация, merge, signing credentials, реальный HikerAPI-токен и установка службы на пользовательские живые наблюдения не следуют автоматически из согласования спеки. Native proof работает только со своим временным fake home и проверяемой очисткой одной регистрации.

## Источники упаковки

Механизм вложенных ресурсов описан в [Tauri resources](https://v2.tauri.app/develop/resources/). Формат переносимой Python installation — в [python-build-standalone distributions](https://gregoryszorc.com/docs/python-build-standalone/main/distributions.html). Выбранные P0 assets и digest взяты из [release 20260901](https://github.com/astral-sh/python-build-standalone/releases/tag/20260901); это входы proof, не заявление о проверенной совместимости.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
| --- | --- | --- | --- | --- | --- |
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | Not run | Scope already approved |
| Codex Review | `/codex review` | Cross-model opinion | 0 | Not run | Astra-only requirement |
| Eng Review | `/plan-eng-review` | Architecture & tests | 1 | CLEAR (PLAN) | 8 findings folded; 0 critical gaps |
| Outside voice | Astra read-only agent | Fresh-context check | 1 + recheck | Complete | 2 findings folded; no remaining blocker |
| Design Review | `/plan-design-review` | UI/UX | 0 | Not run | Before full G1 implementation |
| DX Review | `/plan-devex-review` | Developer experience | 0 | Not run | Not required for C0/P0 |

**VERDICT:** ENG CLEARED — можно исполнять C0/P0. Полная спека проверена на архитектурную согласованность; последующие фазы требуют своих подробных планов и executable gates. GUI и релиз ещё не готовы.

NO UNRESOLVED DECISIONS
