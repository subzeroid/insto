# insto-gui: engineering review

Дата: 2026-09-05. Режим: FULL_REVIEW, вариант B — вся согласованная спека и оба начальных плана. Модель: Astra; дополнительный read-only reviewer — Astra с отдельным контекстом, не cross-model review.

Проверены [спека](../specs/2026-09-05-insto-gui-design.md), [C0](2026-09-05-insto-desktop-foundation.md), [P0](2026-09-05-insto-gui-runtime-proof.md) и [delivery map](2026-09-05-insto-gui-delivery-map.md). Основа кода: `6d2dfd0`, документационная ветка `docs/insto-gui-design` на `e08b74d` до этого review. Уже присутствовавшие незакоммиченные уточнения C0 и порядка фаз сохранены и проверены.

Результат: восемь замечаний внесены в документы. Неопределённых технических решений не осталось; это разрешает исполнение C0/P0, а не подтверждает готовность GUI, portable runtime или релиза. Согласованные пользователем рекомендации применены без повторного выбора каждой детали.

## Scope challenge

Цель удержана: установить приложение, ввести токен, добавить аккаунты, получать наблюдения без внешнего Python/CLI. Упрощение до оболочки над системным CLI противоречило бы согласованной установке. Переписывание scheduler/provider в Rust увеличило бы число владельцев данных и не требуется.

Вся v1 слишком велика для одного PR, но уже разделена на самостоятельные этапы. C0/P0 остаются небольшими исполняемыми планами; C1/P1/C2/G1/G2/R1 имеют критерии выхода, а подробные шаги появятся после реального portability evidence. Дизайн полноценного UI не задерживает C0/P0.

NOT in scope: дополнительные backend в onboarding, Windows/Linux, новый event store, media downloader, расширение лимитов watcher, миграция SQLite v2, изменения отдельного CLI без совместимости, публикация, signing keys и реальные credentials. Соседний GUI-проект не изменяется.

## Что уже существует

- `insto/service/watch_service.py`: controller, management lock, canonical paths, manifest/plist ownership, точная идемпотентность install и uninstall.
- `insto/service/watch_service_runner.py`: загрузка pinned config и один daemon; runner не перечитывает token на каждом tick.
- `insto/backends/hiker.py`: `_call`, retry/error taxonomy, существующий HTTP transport и мягкий quota refresh.
- `insto/service/history.py` и watch service: SQLite v2, snapshots/retention, executor lock, durable registrations и read-only watch list.
- `tests/e2e/test_watch_service.py`: изолированный fake backend, установленный wheel, собственный label, проверка PID/lock/argv, restart и сохранение данных после uninstall.

Новый код должен расширить эти границы. Rust отвечает за trusted paths, runtime publisher и bounded subprocess I/O; Python — за токен, доменные операции и launchd controller. Frontend не получает shell, SQL, токен из сохранённого конфига или произвольный filesystem.

## Architecture review

### 1. P1 — замена токена должна применяться к daemon, confidence 10/10

Доказательство: `watch_service_runner.py:324–327` читает config перед `_run_daemon`; запись `config.toml` не обновляет уже созданный backend. Исходная спека обещала замену credential, но не описывала активацию в работающей службе.

Решение 1A: validate → serialize → stop owned running service → atomic save → start/verify, с защищённой предыдущей конфигурацией и recovery record. Stopped остаётся stopped; внешнее управление требует явного принятия ownership. Ошибка остановки не меняет токен, ошибка запуска восстанавливает прежнее состояние. Crash recovery и неудачный rollback — явные состояния. Изменены раздел 4 спеки и C1 delivery gate. Альтернатива file-only save отклонена: UI сообщил бы об успехе, daemon продолжил бы использовать старый токен.

### 4. P1 — final manifest должен описывать подписанные байты, confidence 9/10

Доказательство: spec связывает SHA с неизменяемым runtime, но не задавала порядок signing. Подпись вложенного кода должна предшествовать внешней подписи; Apple описывает inside-out signing. Вывод для нашего publisher: hash manifest нельзя вычислить до изменения runtime подписью. [Apple TN2206](https://developer.apple.com/library/archive/technotes/tn2206/_index.html).

Решение 4A: nested signing → final manifest/build-id → outer signing → notarization → downloaded/copy verification. Это требование P1/R1, не дефект намеренно unsigned P0. Проверка layout, entitlements и обеих архитектур остаётся реальным app gate; неподтверждённые signing keys не используются.

### 5. P1 — C1 должен предшествовать app service proof, confidence 10/10

Доказательство: C0 dispatch имеет только `hello`; P1 обязан запустить/проверить службу через desktop protocol. Без C1 это невозможно без временного второго контроллера.

Решение 5A: C0 → P0 → C1 → P1 → C2 → G1 → G2 → R1. Уже начатое исправление delivery map сохранено, порядок в самой спеке и gate P0 также согласован. Никакой production fake-операции ради теста.

## Code quality review

### 3. P2 — runtime lock не фиксирует wheel builder, confidence 9/10

Доказательство: `pyproject.toml:54–56` требует `hatchling` без версии; исходный P0 вызывал обычный `uv build` в isolated environment. Runtime export не закрепляет эту отдельную цепочку.

Решение 3A: frozen export dev group как hash-bearing build constraints, `uv build --build-constraints ... --require-hashes`, digest constraints и версия uv в metadata. Constraints не устанавливают dev tools в поставляемый runtime. `uv.lock` не переписывается в review; wheel берётся из точного clean core commit. Flags поддержаны [uv build reference](https://docs.astral.sh/uv/reference/cli/#uv-build). Позитивный build и негативные hash tests — задачи P0, не результат этого review.

### 6. P1 — balance 403/404 не являются результатом lookup профиля, confidence 9/10

Доказательство: `_call` использует общую HTTP taxonomy; `hiker.py:148–164` трактует 403/404 как Instagram lookup semantics. Для `/sys/balance` нельзя сообщать пользователю о banned/not-found аккаунте или принимать неопределённость за подтверждение токена.

Решение 6A: C0 `validate_access` преобразует эти два статуса в безопасный общий `BackendError` до shared translation, сохраняя `_call` bookkeeping. 401 остаётся auth-invalid; strict schema требует настоящий неотрицательный int, не bool. Мягкий `refresh_quota()` не меняется. Существующие уточнения плана сохранены, fixture отправляет SDK `x-access-key`, а не выдуманный Bearer header.

## Test review

### 2. P1 — timeout pytest не должен оставлять независимую службу, confidence 10/10

Доказательство: P0 использовал `subprocess.run(..., timeout=180)`, а cleanup native test находится в Python `finally`. Жёсткое завершение subprocess при timeout не выполняет его finally; launchd живёт независимо. Дополнительный Astra reviewer воспроизвёл отсутствие finally на безопасном дочернем процессе, без launchd. Логи записывались только после return, поэтому timeout также терял основной evidence.

Решение 2A: test-only supervisor в P0 Task 5: context/logs до запуска, 600-секундный бюджет, SIGINT с 60-секундным grace, подтверждённое завершение своей process group и ограниченный fallback через существующий ownership-safe uninstall. C0 fixture принимает только новый home внутри exact basetemp. Неуспешная очистка означает failed proof и сохранение runtime/home/label; никакого массового удаления LaunchAgents. Unit failure-injection предшествует native запуску.

### 7. P2 — примеры тестов недостаточны для заявленных границ, confidence 9/10

Решение 7A: нижняя матрица обязательна в соответствующем этапе. Четыре группы пробелов — protocol/auth boundaries, service mode compatibility, packaging/supervisor failures, будущие lifecycle/history/UI гонки. Не ограничиваться smoke assertions и процентом coverage. Образцы кода в планах — старт, не разрешение пропустить негативные случаи.

```text
Установка
  ├─ P0 archive → locked wheels → relocation [UNIT + INSTALLED]
  │    └─ timeout / tamper / native cleanup [UNIT failure injection]
  └─ P1 trusted publisher → hello [RUST UNIT + APP INTEGRATION]
       ├─ bad hash / owner / arch / disk / crash → безопасный отказ
       └─ C1 configure → G1 onboarding [UNIT + APP E2E]
            ├─ invalid / quota / network / unknown → различимые состояния
            ├─ save ok, start fail → recovery без повторного ввода
            └─ daemon → первый snapshot → exit GUI → новый tick [NATIVE E2E]
Управление
  ├─ replace token → owned restart / stopped / rollback [UNIT + NATIVE]
  ├─ watches → CAS / slot race / generation [DB INTEGRATION]
  ├─ history → PK / retention / cursor / budget [READONLY DB + UI]
  └─ update / external CLI / crash → restore exact owned state [APP E2E]
Релиз
  └─ signed runtime → final manifest → app → quarantined DMG [REAL MAC MATRIX]
```

Все новые ветви здесь — PLANNED, не TESTED. Имеющийся native lifecycle baseline не подтверждает ещё не написанные C0/P0 или GUI.

| Этап / место тестов | Обязательные случаи и assertions |
| --- | --- |
| C0 `tests/test_desktop_protocol.py` | Exact 64 KiB и +1; malformed UTF-8/JSON; duplicate keys, включая nested; NaN/Infinity; bool version; ID длины 0/1/64/65; extra keys; params другого типа; две строки; missing EOF ограничивается caller timeout; output exact 2 MiB/+1; ошибки не отражают sentinel из ввода. |
| C0 dispatch / subprocess tests | Только hello; unsupported op/params; unexpected exception → static internal error; корректный request ID, invalid ID → null; no SDK import, no credentials/DB/home creation; isolated installed-wheel origin; exception/timeout не считаются JSON success. |
| C0 Hiker tests | Valid remaining, zero, bool, negative, missing/unknown/list/invalid JSON; 401, 402, 403, 404, 429, 5xx, timeout/network; exact `/sys/balance` и SDK auth header; 403/404 — plain BackendError, error bookkeeping сохранён; soft quota regression. C1 дополнительно проверяет redactor-before-network и close backend на всех исходах. |
| C0 service tests | `stored mode {legacy,-B} × requested mode {legacy,-B} × loaded {yes,no}`: совпадение сохраняет существующие semantics; mismatch отказывает без native mutations/write; uninstall принимает только две exact owned формы. Параметры тестов не зависят от родительского `sys.dont_write_bytecode`; suite проходит обычным Python и `python -B`. Fake test home — default/valid/relative/existing/symlink/wrong parent. |
| P0 manifest/archive tests | Changed/deleted/extra file, chmod/owner/type; internal file symlink normalization, external/absolute/traversal/cycle/hardlink/device; count/total/entry budgets на границе; corrupt archive/hash; не включать partial manifest в success. |
| P0 builder tests | Clean vs dirty core; wrong architecture; existing/output-outside-root; frozen runtime + build exports; все build зависимости hash-pinned; tampered/missing build hash → failure до success manifest; uv version/constraints digest; runtime без dev group; retained incomplete output. |
| P0 probe/supervisor tests | Wrong hash/version/arch/origin; malformed/multiple JSON, stderr/nonzero/timeout; context существует прежде Popen; output stream на диск; SIGINT/finally/escalation; завершение своей группы перед fallback; ownership mismatch; skip и cleanup failure не pass; artifact root известен даже при exception. Native smoke — только после unit tests, с copied wheel runtime и fake home. |
| C1 lifecycle integration | Double-submit; invalid replacement preserves old config; running replacement restarts with new credential, paused watches сохранены; stopped stays stopped; failed stop не пишет; start fail → rollback; rollback fail; crash на каждой стадии; external CLI ownership refusal; exhausted composite budget → reconciliation; старый/new sentinel нигде не опубликован. |
| C2 read/write DB integration | No API/SQLite writes у reads; 3 active и concurrent last-slot adds; stale revision и `last_ok` update; pause/resume vs in-flight generation; retained history on remove; PK reuse/rename ambiguity; `(captured_at,id)` ties; pruned selection; baseline не diff; empty page + continuation; incomplete search не объявляет unique PK. |
| C2 performance fixture | 2 000 candidates × near-64-KiB и oversized row; bounded batches, bounded response, SQLite busy/progress timeout; 200 feed candidates/50 items; process RSS/elapsed и `EXPLAIN QUERY PLAN` evidence; overview не запускает history scan; два чтения не блокируют mutation безгранично. |
| P1/G1/G2 app E2E | Offline runtime preparation без внешнего Python; tamper/owner/race/disk-full; token-only setup, safe UI messages, four sections; hidden window no polling; close/reopen during mutation; новый tick после exit и unmount DMG; two-runtime update/rollback, desired stopped, crash transitions, external home refusal/adoption. |
| R1 real Mac matrix | Обе заявленные архитектуры и minimum OS; downloaded quarantine, Developer ID/notarization, nested signature и final hashes после copy; чистая учётная запись без developer PATH; background item refusal; sleep/login; uninstall сохраняет данные. |

Failure handling: auth/schema/ownership errors не retry вслепую; read failure сохраняет stale UI; transport loss during mutation перечитывает состояние; native test timeout сохраняет evidence; update/credential failure восстанавливает только свою службу. Ни неизвестный launchd format, ни нулевая квота не означают «мониторинг работает».

## Performance review

### 8. P2 — row limit не ограничивает память JSON scan, confidence 9/10

Доказательство: спецификация допускала 2 000 × 64 KiB, примерно 125 MiB сырого JSON на запрос, до Python объектов; два параллельных чтения удваивают объём. Ограничение ответа 2 MiB не ограничивает внутренний `fetchall`.

Решение 8A: потоковые пакеты ≤16, SQL byte-length guard до материализации oversized JSON, progress deadline и pagination. Overview каждые 5 секунд не выполняет historical scan. Существующая SQLite schema сохраняется; query plans и fixture измеряются в C2, а не заменяются заранее придуманным cache/index store. Это план ограничения ресурсов, не измеренный performance результат.

## Параллелизация и задачи исполнения

Три роли: core, packaging/app runtime, frontend. До стабильного C0 контракта последовательный путь; после него P0 unit helpers и C1 unit tests допустимы параллельно без публикации app, но финальный P0 build требует clean C0 wheel. После P1 C2 и mock-IPC G1 могут разрабатываться параллельно; интеграция G1 зависит от C2. Контракты/файлы принадлежат одному агенту, независимый Astra review не пишет в те же файлы.

Flat tasks, без нового scope:

| ID | Priority | Фаза / результат | Оценка человек / Astra |
| --- | --- | --- | --- |
| T1 | P1 | C0 strict protocol/auth и service mode matrix, wheel gate | 1–2 дня / 3–6 часов |
| T2 | P1 | P0 supervisor, explicit isolated home, timeout/cleanup tests | 1 день / 2–4 часа |
| T3 | P2 | P0 build constraints + hash failure tests/evidence | 2–4 часа / 30–90 минут |
| T4 | P1 | C1 credential replacement/recovery и ownership tests | 1–2 дня / 3–6 часов |
| T5 | P1 | P1 final signing manifest order и copied-app verification | 1–2 дня / 3–6 часов, зависит от Mac/ключей |
| T6 | P2 | C2 bounded history reads и большой fixture | 1 день / 2–4 часа |

Это ориентиры, не обещание времени релиза. TODO-файл не создавался: все задачи входят в существующие фазы, отложенного нового scope нет. Design review полного G1 уместен после P1; CEO review не нужен для повторного согласования уже выбранного продукта.

## Outside voice и итог

Независимый Astra reviewer подтвердил native timeout/finally проблему и незакреплённый wheel builder; оба замечания приняты. Отсутствие signing в P0 не объявлено дефектом: P0 намеренно developer-only. Claude/Sol и nested Codex CLI не запускались.

Scope accepted as-is. Architecture: 3 замечания; code quality: 2; tests: 2 замечания, включая четыре группы coverage gaps; performance: 1. Критических пробелов в исправленном плане: 0. Lake score: 8/8 рекомендаций закрывают причину и тестирование, не только симптом. План не заменяет будущие executable gates.

Свежая проверка review-документов: 5 файлов, AST parsing 24 Python-блоков, parsing 1 JSON-блока, 14 локальных ссылок и 4 terminal review reports — pass. `git diff --check` и `mkdocs build --strict` — pass. MkDocs исключает `docs/superpowers`, поэтому отдельная проверка ссылок и примеров обязательна и выполнена; успешная сборка сайта сама по себе их не проверяет. Найденное неверное положение footer C0 исправлено, validator повторно прошёл.

QA checklist и 6 сериализованных task records сохранены локально в gstack project artifacts. Продуктовые тесты C0/P0, приложение и релиз в этом review не выполнялись; настоящее API и пользовательские LaunchAgents не затронуты.

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
