# Changelog

## v0.1.0 — Public Hardware Preview (2026-07-20)

First public release of the distribution channel.

- `veizik doctor` — hardware scan + per-model-family support tier table
  (stdlib-only; no heavy deps).
- `veizik login / status / logout` — live entitlement client against
  veizik.com (server-signed tokens; no secrets in this repo).
- `veizik t2v / t2i` — experimental universal render path (Linux + NVIDIA,
  bring your own torch/diffusers env).
- SUPPORTED.md / KNOWN_ISSUES.md — honest environment + defect ledger.
- Installer: `curl -fsSL https://veizik.com/install.sh | sh` (git-pinned tag,
  isolated venv, PATH shims `veizik` / `vz`).
