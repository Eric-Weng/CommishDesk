# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue or PR.

Use GitHub's private vulnerability reporting for this repository:
**Security → Advisories → Report a vulnerability**
(<https://github.com/Eric-Weng/CommishDesk/security/advisories/new>).

We aim to acknowledge a report within 7 days and to agree a disclosure timeline with
you. Please give us a reasonable window to ship a fix before any public disclosure.

## Scope

This repository is the **engine** only — a self-hostable Python package that runs with
zero credentials. In scope:

- Input handling of untrusted, league-supplied strings (team / league / display names)
  in `ingest/sanitize.py` and anything downstream that trusts a Facts JSON string.
- Prompt-injection paths into the LLM narrator.
- The content-safety checks in `narrate/safety.py`.
- Anything that could cause the "deterministic, zero-credential" guarantee to break
  (an unexpected network call, a credential requirement, non-determinism).
- Dependency and supply-chain issues in the packaged code.

The hosted service (accounts-free onboarding, email delivery, storage, serving) lives
in a separate private repository and is **out of scope here** — but if you find
something in this engine that the hosted service would inherit, report it.

## Design assumptions (not bugs)

- All league-supplied text is treated as untrusted and is sanitized at one ingest
  boundary; downstream code then trusts Facts JSON strings by design.
- The engine ships no secrets and no real data. If you find a secret, an API key, a
  real email address, or a real league config committed here, that **is** a security
  issue — please report it.
- Untrusted issue / PR / comment text is never fed to a coding agent or CI LLM. A
  workflow that does so is a vulnerability.

## Supported versions

The project is pre-1.0. Only the latest `main` and the most recent tagged release
receive security fixes.
