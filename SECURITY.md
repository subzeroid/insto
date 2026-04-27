# Security Policy

## Supported versions

insto is pre-1.0. Only the latest published release receives security fixes.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a vulnerability

Please **do not** open a public issue for security-sensitive reports. Instead:

1. Use GitHub's [private vulnerability reporting](https://github.com/subzeroid/insto/security/advisories/new) on this repository, **or**
2. Email the maintainer directly (see the GitHub profile linked from `pyproject.toml` `authors`).

Include:

- a description of the issue and the impact you observed,
- a minimal reproduction (inputs, commands, expected vs. actual behavior),
- the `insto` version (`insto --version` or the installed dist) and Python version.

You can expect:

- an acknowledgement within **7 days**,
- a fix or public advisory coordinated within **30 days** for confirmed issues,
- credit in the release notes if you'd like it.

## Scope

In scope:

- Code under `insto/` and `tests/`.
- Default deployment via `pip install insto` / `uv tool install insto`.
- Anything that allows reading secrets (`hikerapi_token`, snapshots) from logs, error output, or `~/.insto/`.
- Any path-traversal, MIME-confusion, or remote-content-trust issue in the CDN streamer.
- Schema-drift or rate-limit handling that could exfiltrate the operator's IP / token.

Out of scope:

- Bugs in HikerAPI itself, aiograpi, or the underlying Instagram private API.
- Account-level bans on user accounts that result from real Instagram authentication via aiograpi.
- Information already public on instagram.com about the target — `insto` reads, it does not create new visibility.

## Operator notes

`insto` is offensive-tooling-adjacent: it queries third-party data, downloads remote media, and stores intel on disk. Even fully patched, the local store at `~/.insto/store.db` is not encrypted at rest in 0.1.x. A stolen laptop is still a data-loss event. Treat the output directory like any other source of sensitive material.
