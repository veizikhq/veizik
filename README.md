# Veizik

**Run local AI media workloads with hardware-aware compatibility checks and execution
profiles.** Veizik checks your GPU and reports a runnable support tier and recommended
execution profile. Rendering happens on hardware you own — no cloud render service, and
your media and prompts stay on your machine. Powered by a native CUDA engine.

This README describes only what the **public download actually does**. Anything not yet in
the download is labeled as planned or experimental, on purpose.

---

## Public Preview v0.1.0

**Live** (confirmed working in the public download):
- `doctor` — hardware scan + per-model-family support tier table (Python stdlib only; runs with no GPU)
- `login` / `status` / `logout` — veizik.com server-signed entitlement client
- free entitlement issuance

**Experimental** (public, but constrained):
- universal `t2v` / `t2i` — Linux + NVIDIA only; you bring your own torch/diffusers environment

**Not yet public** (not in the download — do not read these as available today):
- ComfyUI drop-in (`run` / `serve`) — ComfyUI integration, upcoming preview
- TimeMachine preview build — planned for a Preview build
- native CUDA DiT engine assets — planned platform release assets

External reproduction so far: none. This is a Public Preview, and independent verification
on your own hardware is exactly what it is for.

---

## Install

Linux or Windows WSL2, with `git` and Python 3.10+:

```sh
curl -fsSL https://veizik.com/install.sh | sh
```

Nothing renders on install. The next step is to check your hardware.

---

## Usage

### Check your hardware

```sh
veizik doctor
```

`veizik doctor` scans this machine and prints a support tier per model family, so you know
what fits before you render. It uses the Python standard library only and runs even without
a GPU present.

### Activate a free entitlement

```sh
veizik login <key>     # redeem a free key from veizik.com for a server-signed entitlement
veizik status          # show the current tier and entitlement
veizik logout          # clear the local entitlement
```

Get a free key at [veizik.com](https://veizik.com). Your media and prompts stay local; only
license data is exchanged with the server.

### Experimental render (Linux + NVIDIA)

```sh
# universal render path — you provide a torch/diffusers environment
veizik t2v "a barista pouring latte art" --model ltx
veizik t2i "a warm-lit product shot on a wooden table" --model flux
```

The universal `t2v` / `t2i` path is experimental. It targets Linux + NVIDIA and depends on a
torch/diffusers environment you set up yourself.

---

## What is measured

Veizik separates claim types deliberately. Two things are checked internally today; render
timings are not yet published.

**Peak VRAM (internal):** LTX-13B peak VRAM **9.55 GB** on an RTX 3090 (internal). This is a
VRAM figure, not a render time.

**Block/engine-level numerical checks (internal):** engine paths for 6 model families
(Step-Video 30B, HunyuanVideo, Wan 2.1, LTX-Video, CogVideoX, and FLUX.1-dev) are numerically
checked at the block/engine level against a reference implementation. This is an internal,
component-level numerical check — **not** an end-to-end generation benchmark — and no
numerical-fidelity figure is published here until it is externally reproducible.

**Render time / throughput:** the README body inlines no throughput number. The exact model,
parameters, power-cap setup and verification, and the verified results published so far (each
labelled with its verification status) are in [REPRODUCE.md](REPRODUCE.md) — re-run it and post
your result; that external reproduction is exactly what unlocks comparative claims. See also
[veizik.com](https://veizik.com).

---

## Support tiers

Honest scope for the Public Preview. See [SUPPORTED.md](SUPPORTED.md) for the full matrix.

| Tier | Environment |
| --- | --- |
| Internally verified | Linux x86_64 + NVIDIA CUDA + 24 GB VRAM class |
| Experimental | Windows WSL2 + NVIDIA |
| Planned | additional NVIDIA memory classes · Apple Silicon · mobile adapters |

This is a Public Preview with no external reproductions yet. We do not claim universal GPU
support, and we do not claim that any given model always runs or that a profile swap never
fails — that is precisely what external testing is meant to establish.

Known limitations and the honest issue ledger: [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

---

## Pricing

Pricing is a **local runtime license** — not cloud credits. You supply the GPU.
Full details at [veizik.com/#pricing](https://veizik.com/#pricing).

| Plan | Price |
| --- | --- |
| Free Preview | $0 — install, hardware check, free entitlement, experimental render |
| Founding Creator | **$19 / month** (or $149/year) |
| Founding Pro | **$49 / month** (or $399/year) |
| Studio Pilot | **from $299** — pilot evaluation |
| Founding Lifetime | **$299 one-time** — first 200 seats, price locked for the life of the seat |

Reservation (free key) is open now; **no payment is collected yet** — paid checkout is not live.

### Founding Lifetime — first 200 seats

The first 200 Founding Lifetime seats ($299 one-time) get:

- Lifetime license, price locked for the life of the seat
- Founder feedback access
- Priority compatibility support

**License unit — a personal seat, not a PC.** One user may register up to **2 computers**
and run on **1 at a time** (concurrency is what is metered, never install count). Device
change is self-service up to 3×/year; registration binds to a machine fingerprint, not the
GPU alone, so a GPU swap does not break the license.

The Founding-Lifetime counter (first 200 seats) is computed server-side from **real paid
purchases only** (not checkout starts).

---

## Operator & legal

- **Brand / operator:** Veizik
- **Registered business name:** LinkPick
- **Governing law / jurisdiction:** Republic of Korea
- **Business registration number:** available on request
- **Payments:** Polar (Merchant of Record) — USD, tax handled at checkout
- **Support:** [support@veizik.com](mailto:support@veizik.com)

[Privacy](https://veizik.com/privacy.html) ·
[Terms](https://veizik.com/terms.html) ·
[Refunds](https://veizik.com/refund.html)

Veizik is a proprietary local runtime, not open source. The engine is not
distributed as source. Installation and evaluation are free; production use requires a
license.

---

© 2026 Veizik (operated by LinkPick, Republic of Korea) · powered by a native CUDA engine ·
benchmarks are being finalized — see [veizik.com](https://veizik.com)
