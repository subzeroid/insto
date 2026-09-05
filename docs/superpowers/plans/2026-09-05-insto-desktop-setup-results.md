# C1 desktop setup — verified local result

**Status:** C1 implemented and verified locally on 2026-09-05. No merge, push,
PR, signing, publication or release. Worktree: `desktop-setup`, branch
`feat/desktop-setup`, based on C0 `d4ea4f89aa57940e6fbba2d9c195b11ae682e777`.
The verified implementation/test source is `a08ea40`; the subsequent handoff
commit only updates documentation. The main checkout, C0 and sibling GUI P0
checkout remain unchanged.

## Delivered

- Eight capabilities: hello; setup/settings inspection; token-only configure;
  credential replacement; service start, stop and repair.
- Explicit isolated HikerAPI validation, strict token grammar and static errors.
  Candidate requests and close share a bounded validation wait; client creation
  does not inherit provider/backend/proxy/CA settings.
- Canonical private profile, stable nonblocking flock, exact ownership/state/
  journal schemas and atomic durable file publication. No adoption of unmarked
  populated profiles or external CLI homes.
- Setup retains validated credentials when local startup fails. Replacement
  stops and excludes the executor before publishing config; rollback preserves
  exact old config/state and separately observed running disposition.
- Durable crash reconciliation, terminal cleanup, orphan-backup handling,
  private staged database initialization and current-WAL schema preflight.
- Exact owned launchd runtime/argument verification; idempotent start, persistent
  stop, clean-exit kickstart, matching PID/flock readiness and cancellation drain.
- Read-only, SDK-free inspection and installed-process protocol checks.

Public reference: [Desktop setup protocol](../../desktop-protocol.md).
Execution contract: [C1 plan](2026-09-05-insto-desktop-setup.md).

## Verification

Host: macOS 26.6.2, build 25G83, arm64; developer and installed-test Python
3.12.11. All implementation and independent reviews used Astra.

| Gate | Result |
| --- | --- |
| Full pytest with coverage | **1625 passed, 1 opt-in native skipped**, 18.88s |
| Desktop/controller regressions with `python -B` | **358 passed**, 5.19s |
| Ruff check and formatting | Pass, 143 files formatted |
| mypy | Pass, 63 source files |
| `git diff --check` | Pass |
| Strict MkDocs build | Pass; output outside worktree |
| Wheel/sdist build | Pass, frozen local build toolchain, no isolation resolver |
| Fresh installed-wheel processes with `-I -B` | Hello, both missing inspections, invalid params, both pending inspections pass |
| Supervised native fake lifecycle | **1 passed, 34.92s**; cleanup confirmed |
| Independent component and final whole-change review | Approved after regression-backed fixes |

The installed smoke verifies site-packages origin, a single response line,
preserved request IDs, no stderr, no provider SDK imports, and no missing-profile
initialization. The ordinary suite leaves native testing opt-in; the native pass
is a separate supervised run, not a skipped test counted as success.

## Native evidence and safety

Successful proof root: `/private/tmp/insto-c1-proof.oApDqN`.

- `native-context.json`: phase `passed`, exit 0, `cleanup_confirmed: true`.
- `native-result.txt`: exact passing native test result.
- `native-test/service home/desktop-lifecycle.json`: verified transitions.
- `native-test/service home/desktop-native-states.jsonl`: private native
  state/PID/exit and command-timing evidence, without credentials or raw environment.
- `coverage.json` and `site/`: coverage artifact and strict documentation build.

The unchanged previously reviewed GUI P0 supervisor ran the extended existing
native test against a freshly wheel-installed interpreter. It starts an owned
process group, drains it before fallback cleanup, verifies the exact fake fixture
and registration identity, and confirms absence after normal owned uninstall.
This C1 venv is **not** a new portable-runtime proof or a distributable app.

The native C1 sequence proved:

1. Start with matching native/executor PID `60215`; identical start retained it.
2. Stop left installed artifacts intact, registration unloaded and executor idle.
3. Restart reached matching PID `60242`.
4. Clean exit left the exact registration loaded, exit 0 and executor idle.
5. One non-forcing kickstart reached matching PID `60360`.
6. Final stop/uninstall removed only test service artifacts; fake profile data
   remained. The existing legacy lifecycle smoke then also passed.

Crucially, the recorded kickstart client **did time out after 10.0038s**. Subsequent
bounded observation proved the owned process running; no second kickstart or
larger command timeout was used. This establishes the uncertain-outcome path on
the real host, without assuming that a timed-out client cancels launchd's work.

Exact successful label: `io.insto.watch.501.bfa9a7e4096d59be`. A separate final
`launchctl print` returned 113 and the expected exact-label absence message.
The test plist and manifest were removed by normal uninstall. All fake config,
database, logs and diagnostic roots were retained for inspection.

Earlier native runs intentionally remain recorded as **failed**, not passing:
`insto-c1-verification.VEBNdm`, `insto-c1-native-diagnostic.57T0SD`,
`insto-c1-verified.Oi4Vxn`, and `insto-c1-final.m43kCb` under `/private/tmp`.
Each supervisor confirmed cleanup. They exposed real `xpcproxy`, `SIGTERMed`,
`(never exited)` and kickstart-timeout cases; corresponding red/green tests and
independent reviews preceded the successful proof.

## Artifacts and reproducibility

Built from the frozen developer toolchain with `python -m build --no-isolation`.
The fresh test environment installed runtime dependencies from a hash-bearing
frozen export excluding the root project, then installed the wheel without
dependency resolution. No lock or package version bump was made.

| Artifact | SHA-256 |
| --- | --- |
| `dist/insto-0.7.20-py3-none-any.whl` | `fb2eae1b62c7860d050f91e691b30598c5817d44051647e39f51a4abef014d36` |
| `dist/insto-0.7.20.tar.gz` | `3c7d7fa9a422638b776bce406058a1696a8f6f75232bae076e2fd5c3d7ae536a` |
| Unchanged `uv.lock` | `efa064cc8906504ba0e60e054c42ed2cfaefdf8ab6c44d145b353904cbfdc86b` |
| Developer frozen requirements | `5d35d0dd25aae82ad0ce42c59b9ec8088fc15aa70717106e3f87ea0274c34b81` |

Installed interpreter: `different path/python/bin/python3` within the proof root.
The wheel's `insto.__file__` resolves inside that environment's `site-packages`.
The deliberate lock root metadata remains 0.7.17 while package/wheel are 0.7.20;
all frozen dependency exports exclude the root project.

## Limits and next gate

No real token, real provider request or live watch was used. Credential
transactions were exercised with real private files/SQLite and controlled service
boundaries; native proof exercised the installed controller with the existing
fake backend. A live-provider credential replacement inside an installed GUI is
not claimed. Cancellation-resistant SDK cleanup still needs the P1 process owner's
hard deadline, beyond C1's bounded wait and attempted close.

P1 is next: the Astra-built Tauri/Vue application shell, bundled-runtime/process
ownership, and token onboarding UI. C2/G1 add account/watch operations and the
working monitoring interface; G2 owns migration/update compatibility; R1 owns
signed/notarized distribution and installation acceptance. Intel, Gatekeeper,
fresh-user app installation and the complete “install → token → accounts →
monitoring” experience remain unproved. The existing C0/P0 artifacts are not
silently repinned to this C1 wheel.
