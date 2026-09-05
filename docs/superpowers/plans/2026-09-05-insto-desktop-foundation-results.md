# C0 desktop foundation: execution results

Дата: 2026-09-05. Ветка: `feat/desktop-foundation`, base `b40241b` (`docs/insto-gui-design`). Рабочая директория: `<worktrees>/desktop-foundation`. Исполнение и все ревью — Astra.

Статус: **C0 завершён локально**. Это не релиз, не portable runtime и не установленный GUI. [План](2026-09-05-insto-desktop-foundation.md) и [проверенная матрица](2026-09-05-insto-gui-engineering-review.md#test-review) задают границы результата.

## Результат по задачам

- [x] Task 1: строгий ограниченный JSON request/response; duplicate keys, nonfinite constants и overflow floats отвергаются.
- [x] Task 2: одноразовый `python -I -B -m insto.desktop`, только side-effect-free `hello`; корректные статические ошибки.
- [x] Task 3: `HikerBackend.validate_access()` через существующий transport/retry, строгая квота и HTTP taxonomy, без изменения мягкого REPL refresh.
- [x] Task 4: совместимые legacy/`-B` формы службы, точный install и ownership-safe uninstall; test-only предсказуемый private home для P0 cleanup.
- [x] Task 5: [публичное описание протокола](../../desktop-protocol.md), nav, полный offline gate, wheel/sdist и installed-wheel smoke.

Коммиты реализации:

- `4bd16eb`: service argv compatibility и native test support.
- `87d2d6e`: protocol + hello (две тесно связанные задачи в одном коммите).
- `e2e0454`: strict Hiker access, включая найденный на финальном ревью redirect regression.

Версия пакета не повышалась. Manifest/schema SQLite, `uv.lock`, CLI semantics и другие backend не менялись. Уровень `get_last_error()` остаётся исторической диагностикой, а не очищаемым флагом последнего запроса. GUI не должен использовать его вместо результата отдельной validation operation.

## Test-first evidence

| Компонент | RED | GREEN после итогового исправления |
| --- | --- | --- |
| Protocol + hello | 64 failures: отсутствовал package | 65 passed, включая добавленные запреты config/store/SQLite initialization |
| Service/native helpers | 23 failures: отсутствовал `-B` и test helpers; прежние 45 прошли | 100 passed в service/runner/CLI/helper subset, обычный Python и `python -B` |
| Hiker strict access | 26 failures: отсутствовал метод; 1 existing-soft check прошёл | 77 passed в access + existing Hiker subset |
| HTTP redirects | 4 failures: 301/302/307/308 с balance-shaped JSON считались success | Все 4 отвергаются с записанным BackendError |

Финальное ревью обнаружило, что `_on_response` поднимает только 4xx/5xx. В strict balance дополнительно вызывается `response.raise_for_status()` внутри существующей error boundary; shared hook и `_call` не менялись. Исправление воспроизведено тестами и подтверждено отдельной повторной Astra-проверкой.

## Финальные проверки

| Проверка | Результат |
| --- | --- |
| `.venv/bin/ruff check` | pass |
| `.venv/bin/ruff format --check` | 127 files formatted |
| `.venv/bin/mypy insto` | 56 source files, pass |
| `.venv/bin/pytest --cov=insto --cov-fail-under=75 -q` | **1 379 passed, 1 skipped**, 12.44 s, **84.21%** |
| Service subset, обычный Python и `python -B` | 100 passed в каждом режиме |
| `.venv/bin/mkdocs build --strict` | pass; output вне worktree |
| `.venv/bin/python -m build --no-isolation --outdir ...` | wheel + sdist built |
| Installed wheel, чистое venv + `-I -B` | hello и 3 malformed cases pass, импорт только из installed site-packages, профиль не создан |
| Installed strict-access regression | 302 с balance-shaped JSON отвергнут через MockTransport |
| `git diff --check` | pass |

Baseline до реализации: 1 243 passed, 1 native skip. Итого добавлено 136 тестовых случаев. Единственный skip — opt-in `tests/e2e/test_watch_service.py`; настоящий launchd smoke намеренно отложен до P0 portable runtime, как требует план. Его отсутствие не скрыто общим coverage.

`insto.desktop.protocol` и `dispatch` имеют 100% измеренное statement coverage. Entry point `__main__` проверяется реальными изолированными subprocess; текущий coverage setup не собирает дочерние процессы и показывает для него 0%. Это ограничение измерения, а не заявление об отсутствии теста.

Сборка C0 использовала установленный из frozen lock локальный build toolchain (`--no-isolation`), без отдельного незакреплённого build environment. Runtime-зависимости тестового venv установлены из hash-bearing frozen export с `--no-emit-project`. Это developer verification C0, не реализация production P0 builder.

## Ревью и безопасность

- Protocol и service: независимые spec review и code-quality review — clear.
- Hiker + документация: spec review — clear.
- Итоговое integration/quality review: один P2 про redirects исправлен; повторная проверка — approved.
- Все ревью и реализация на Astra; другие модели не использовались.
- Настоящие HikerAPI-токены, API-сеть и пользовательские launchd регистрации не использовались.
- Непреднамеренное обновление только metadata версии в `uv.lock` одним reviewer через `uv run` установлено по точной команде и отменено адресным patch; финальный lock byte-identical исходному. Остальные изменения не сбрасывались.
- Основной checkout и предыдущие worktrees не менялись. Нет push, PR, merge, подписания или публикации.

## Артефакт и handoff

Проверенный wheel: `/tmp/insto-c0-verification.9g2ZT2/verified-dist/insto-0.7.20-py3-none-any.whl`.

SHA-256: `ed5907606ed00218a81bb1b90153f14ef4bfd20219d80a52e6e92fd2aef7e13d`.

Проверенный origin: `/private/tmp/insto-c0-verification.9g2ZT2/installed/lib/python3.12/site-packages/insto/__init__.py`. Рядом сохранены dependency export, verifier и docs output. Это временные локальные evidence, не дистрибутив для пользователя; повторную сборку делать из clean checkout.

Далее — [P0 portable-runtime proof](2026-09-05-insto-gui-runtime-proof.md) в отдельном `insto-gui`. Использовать clean `feat/desktop-foundation` как core source, build wheel заново с зафиксированными build constraints, проверить relocated runtime и изолированную fake-службу с supervisor cleanup. Не выдавать это venv за portable CPython и не запускать прежний native test без предусмотренного supervisor.

После P0 последовательность остаётся C1 → P1 → C2 → G1 → G2 → R1. Token-only onboarding, сохранение credential, GUI, signed/notarized DMG и публичный релиз не входят в завершённый C0.
