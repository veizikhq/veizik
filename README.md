# Veizik — Local AI Video Runtime

Run large AI video models on the GPU you already own. **Low-VRAM, memory-resident execution for RTX 3090 / 4090 (24 GB)-class GPUs** — Wan · HunyuanVideo · LTX · Step-Video · FLUX · CogVideoX. Local. No cloud rendering.

*Veizik is a proprietary commercial runtime — **not open source**. This repo is the public distribution channel: CLI, hardware check, and license client.*

> **Public Preview (v0.1.0)** — current verified and experimental environments are listed in [SUPPORTED.md](SUPPORTED.md). Numbers below are **internal benchmarks** on our own hardware; external reproduction is what this preview is for.

---

## Install (60 seconds)

```sh
curl -fsSL https://veizik.com/install.sh | sh
```

Requires `git` + Python 3.10+. Then:

```sh
veizik doctor                 # hardware scan + per-model-family support tier
veizik login <YOUR_API_KEY>   # free key: https://veizik.com  (shown once at signup)
veizik status
```

- **Detects your hardware** and shows what tier each model family runs at
- **Free API key** activates the runtime (non-commercial, watermarked)
- **Keeps input and output local** — the runtime only talks to veizik.com for license entitlement (key id, device id, tier — never your media or prompts)

## What works in v0.1.0 (Public Preview)

| Command | Status |
|---|---|
| `veizik doctor` | ✅ works everywhere (hardware + support-tier table) |
| `veizik login / status / logout` | ✅ live against veizik.com |
| `veizik t2v / t2i` (universal engine) | 🧪 experimental — Linux + NVIDIA, needs your own `torch`/`diffusers` env |
| `veizik run / serve` (ComfyUI drop-in) | 🔜 next release |
| Native CUDA DiT engines, TimeMachine | 🔜 ship as platform release assets (not in-repo) |

Known problems are tracked honestly in [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## Internal benchmarks (our hardware, fixed conditions)

Measured on a single RTX 3090 24 GB, warm runs, fixed seed — *internal, not yet externally reproduced*:

- **LTX-13B** 1280×704 · 49 frames · 40 steps ≈ **99.6 s** (9.55 GB peak — no OOM)
- **CogVideoX** 720×480 · 49 frames · 50 steps ≈ **387.6 s**

Full conditions and the run-manifest schema: [veizik.com/results.html](https://veizik.com/results.html). If you get different numbers on your hardware, **please open an issue — that data is exactly what we want.**

## Pricing

Free key = install, verify, watermarked non-commercial renders. Paid tiers (Creator $24 / Pro $59, merchant-of-record checkout, tax handled): **[veizik.com/#pricing](https://veizik.com/#pricing)**.

**Founding 100** — the first 100 paid subscriptions keep their price locked for 12 months. Live seat counter (server-computed, real paid subscriptions only) on [veizik.com](https://veizik.com).

## Privacy

Your images, videos, and prompts never leave your machine. The CLI contacts veizik.com only for license entitlement/session lease. Details: [veizik.com/privacy.html](https://veizik.com/privacy.html).

## Support

- Bugs / compatibility reports: [GitHub Issues](https://github.com/veizikhq/veizik/issues)
- Email: support@veizik.com
- Terms: [veizik.com/terms.html](https://veizik.com/terms.html) · Refunds: [veizik.com/refund.html](https://veizik.com/refund.html)

© 2026 Veizik. Proprietary. All rights reserved. Engine: **LimML**.
