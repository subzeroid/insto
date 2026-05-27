# TODOS

## Parallelize startup quota-refresh and target-resolve

- **What:** At REPL startup, `_safe_refresh_quota(facade)` and the new
  `_safe_set_startup_target(...)` run sequentially, each bounded to ~2s. Run
  them concurrently with `asyncio.gather` so a slow network costs ~2s total
  instead of ~4s.
- **Why:** Cuts worst-case startup latency on a slow/dead network roughly in
  half. No effect on a fast network.
- **Pros:** Faster cold-start when the network is degraded.
- **Cons:** Adds concurrency to the startup path; the banner already waits on
  quota, so the resolve must complete before `repl.run()` draws the banner —
  gather'ing them is fine but the ordering/error handling needs care.
- **Context:** Introduced by the 2026-05-27 startup-target feature
  (`docs/superpowers/specs/2026-05-27-startup-target-in-header-design.md`).
  See `run_repl._main()` in `insto/repl.py`. Deferred from eng review as P3 —
  marginal benefit, conflicts with the right-sized-diff goal for that PR.
- **Depends on / blocked by:** Lands after the startup-target feature merges.
