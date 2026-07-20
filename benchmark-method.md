# Veizik Benchmark Method

This document describes **how** Veizik measures performance and **what kind of claim**
each published number is. It is a methodology document, not a results table. No render-time
figures appear here on purpose.

> **Current status.** Render-time numbers are being finalized; only peak VRAM
> (LTX-13B, **9.55 GB**, RTX 3090, internal) and engine-block numerical equivalence are
> published so far. Every render TIME and throughput figure is intentionally left blank
> (`measurement in progress`) until it is repeated and externally reproducible. This is a
> Public Preview with **zero external reproductions** to date.

---

## 1. Claim types

We separate claims by strength and never blur them. A number is meaningless without its type.

| Claim type | What it asserts | How it is produced | Published today? |
|---|---|---|---|
| **End-to-end, internally measured** | Wall-clock render time / throughput / peak VRAM for a full generation, on our own hardware | One fixed-condition run (§2) under the harness, captured in a run-manifest (§4) | Only **peak VRAM** (LTX-13B). All render TIME / throughput: `measurement in progress` |
| **Engine-block numerical equivalence** | The native engine path reproduces the reference math at the block/engine level | Feed identical inputs to reference and native paths, compare relative L2 error (~1e-6..1e-7) | Yes — **6 model-family engine paths numerically checked at block/engine level** |
| **Component benchmark** | Isolated subsystem timing (a kernel, a single DiT step, VAE, offload swap) | Micro-benchmark, warm, isolated from full pipeline | Not published (internal only) |
| **External reproduction** | A third party reproduced a claim on their own hardware | Independent run against this method + published manifest | **None yet** — that is what the Preview is for |

Wording rules that follow from the table:

- Do **not** write "6/6 verified." Write "6 model-family engine paths numerically checked at
  the **block/engine** level." This is a component-level check, **not** end-to-end generation
  verification.
- The 9.55 GB figure is a **memory** measurement, not a render time, and is labelled `internal`.
- Never use absolute phrasing ("all GPUs", "any model always runs", "swap never fails",
  "one job's fault never affects another"). Preview scope only.

---

## 2. Fixed conditions (must be pinned per run)

A render-time claim is only comparable if every one of these is fixed and recorded in the
run-manifest. A run that omits any field is not publishable.

- **Model + commit** — model family, exact weights identifier, and the model/repo commit hash.
- **Engine path** — native CUDA DiT engine vs universal fallback path, plus engine build commit.
- **Resolution** — width × height (e.g. `1280×704`).
- **Frames** — frame count (e.g. `49f`); `1` for a single image (t2i).
- **Steps** — sampler step count (e.g. `40 steps`).
- **Sampler / scheduler** — sampler name and any scheduler parameters (shift, sigmas).
- **Seed** — fixed integer seed; recorded so the run is regenerable.
- **Precision** — compute dtype (e.g. bf16/fp16) and any quantization applied
  (which layers are int8, which are left in full precision).
- **Offload / placement** — capacity-compute tiering configuration: hot/warm/cold placement,
  offload target, and whether CPU/disk offload was engaged.
- **Warm vs cold** — cold = first run after process start (includes weight load / compile /
  cache fill); warm = steady-state after warm-up iterations. **Warm-up is mandatory** before any
  timed render window; cold and warm are never mixed in one figure.
- **OS / driver / runtime** — OS + kernel, NVIDIA driver, CUDA/runtime version, torch/diffusers
  versions.
- **GPU** — GPU model and VRAM class; **GPU power limit** (as configured, since it bounds clocks).
- **Wall-clock boundaries** — the exact start point and end point of the timed window
  (see §3). Timing that does not state its boundaries is not accepted.

---

## 3. Measured fields and timing boundaries

**Fields captured per run**

- Peak VRAM (device-reported peak allocated over the timed window).
- End-to-end wall-clock render time (`measurement in progress`).
- Throughput — frames per second and/or seconds per step (`measurement in progress`).
- Whether the run OOM'd or fell back to a lower profile (yes/no + which).

**Timing boundaries (declared, not implied)**

- **Wall-clock**, not device-event-only timing, is the headline. GPU-event timers may accompany
  it but never replace it.
- **Start** = first denoising step of the timed generation, *after* warm-up and *after* weight
  load / compile, unless a run is explicitly labelled **cold** (in which case start = process /
  generate() entry and load time is included by definition).
- **End** = last step completes and the output tensor is materialized. VAE decode and file
  encode are timed as **separate** segments, not folded silently into the DiT figure.
- Warm figures require ≥1 warm-up iteration discarded before the timed window. A number without a
  stated warm/cold label is treated as unlabelled and is not published.

**Environment hygiene**

- No concurrent GPU tenant during a timed render. Contention invalidates timing; under contention
  we trust only numerical-equivalence (quality) checks, never speed.
- Power limit and clocks recorded as configured. Thermal throttling during the window voids the run.

---

## 4. Run-manifest schema

Every publishable run is captured as a signed **run-manifest** so a claim can be tied back to the
exact conditions that produced it. The canonical-bytes + sign/verify pipeline lives in
[`veizik/manifest.py`](../../veizik/manifest.py) (`veizik manifest build` / `veizik manifest verify`).

A run-manifest records, at minimum:

- `model`, `model_commit`, `engine_path`, `engine_commit`
- `resolution`, `frames`, `steps`, `sampler`, `seed`, `precision`, `quantization`
- `offload` / placement configuration, `warm_or_cold`
- `os`, `driver`, `cuda_runtime`, `torch`, `diffusers`
- `gpu`, `vram_class`, `power_limit`
- `wall_clock_start`, `wall_clock_end` (boundary definitions per §3)
- measured fields (§3), each tagged with its **claim type** (§1)
- `signature` — content digest today (`sha256`), keyed `hmac-sha256` where a shared secret is
  used; production upgrades the algorithm over the same canonical bytes.

The manifest is the unit of reproduction: publishing a number means publishing its manifest so an
external party can pin the same conditions and re-run.

> **Note.** The GPU **profile** manifest in `veizik/manifest.py` (per-model saturation / PIT
> recommendations the runtime trusts) and the **run** manifest described here share the same
> canonical-bytes signing pipeline but are distinct documents. Do not conflate a signed profile
> with a measured render result.

---

## 5. Numerical-equivalence method (engine-block claim)

For each model family with a native engine path, we feed **identical inputs** to the reference
implementation and to the native path and compare outputs by **relative L2 error** at the
block/engine level. Passing paths land in the ~1e-6..1e-7 range (numerically indistinguishable,
not bit-identical). Quantization is applied only where it holds up numerically and left off where
it does not.

Scope and honesty of this claim:

- It is a **component-level** check: block and engine outputs, **not** an end-to-end generated
  clip compared frame-for-frame.
- Reported as "**6 model-family engine paths numerically checked at block/engine level**", never
  "6/6 verified" and never as proof that full generation is validated.

---

## 6. Support tiers (what "verified" scopes to)

- **Internally verified** — Linux x86_64 + NVIDIA CUDA + 24 GB VRAM class. Reproduced on our own
  hardware; not a claim about every GPU or configuration.
- **Experimental** — Windows WSL2 + NVIDIA.
- **Planned** — additional NVIDIA memory classes · Apple Silicon · mobile adapters.

"Internally verified" ≠ externally reproduced. Until an independent run lands, no figure carries
more weight than "internal".
