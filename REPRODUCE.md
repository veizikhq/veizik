# Reproducing Veizik Benchmarks

This guide lists the **exact model, parameters, hardware, and power condition** behind every Veizik
performance number, so anyone can re-run the identical workload — either with Veizik or on their own
`torch`/`diffusers`/ComfyUI stack — and get a directly comparable result.

It publishes **the fixed configuration and the signed run-manifest** for each measurement. It does
**not** describe Veizik's internal engine techniques (kernels, quantization, scheduling); those are
not required to reproduce a *measurement*, only to build the runtime. Numbers here are governed by
the claim ledger via the status ladder in [`benchmark-method.md`](benchmark-method.md);
a figure may appear at or below its verification status and never above it.

> **Status of these numbers:** internal, repeated, single machine. `0` external reproductions so far.
> No "fastest / SOTA / beats" language is used or permitted until the numbers are reproduced on ≥2
> independent environments. Re-running this guide and posting your result is exactly the external
> reproduction we are asking for.

---

## 1. Fixed hardware & environment

| Item | Value |
|---|---|
| GPU | NVIDIA RTX 3090, 24 GB (physical GPU1, UUID `GPU-11b9c5ca…`) |
| **Power cap** | **420 W** — commercial condition, **verified** (see §2) |
| GPU pinning | `CUDA_VISIBLE_DEVICES=1` (sole tenant; no other GPU job) |
| PyTorch | `2.7.0+cu126` |
| diffusers | `0.39.0` |
| Alloc | `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` |
| Env lockfile | [`benchmark/results/diffsynth_venv.lock`](benchmark/results/diffsynth_venv.lock) (full `pip freeze`) |
| Seed | `42` (fixed for every run) |

## 2. Setting and **verifying** the 420 W power cap

The commercial condition is the card at its full 420 W board limit. A benchmark taken under a lower
cap is a **different measurement** and must be labelled as such. Set and — critically — **verify**
the cap holds for the whole run:

```bash
# set (per GPU); requires the host's normal GPU admin rights
nvidia-smi -i 1 -pl 420          # GPU1 -> 420 W
nvidia-smi -i 1 --query-gpu=power.limit --format=csv,noheader   # confirm 420.00 W

# during a render, confirm the card is actually allowed past ~300 W:
nvidia-smi -i 1 --query-gpu=power.draw,utilization.gpu --format=csv,noheader
# a compute-bound render should show >300 W (our LTX run was observed at 418.78 W @ 100% util).
```

If any background service re-applies a lower cap on a timer, the observed `power.draw` will never
exceed that cap — that is the tell that your measurement is **not** at 420 W. Disable such a service
before measuring and re-confirm the cap held afterwards.

## 3. Per-model workloads (fixed config)

Each row is the exact commercial workload. **Veizik command** is the one-line reproduction with the
Veizik runtime; **framework-agnostic config** is what you set to reproduce the *same workload* on any
other stack for a fair comparison (all 14 fair-comparison conditions in
[`benchmark-method.md`](benchmark-method.md) must match, or the result is a *separate measurement*).

| Model (task) | Repo | Resolution × frames | Steps / guidance | Sampler shift · fps |
|---|---|---|---|---|
| **LTX-Video** (t2v) | `Lightricks/LTX-Video` | 768×448 × 49 | 30 / 3.0 | 6.0 · 24 |
| **FLUX.1-dev** (t2i) | `black-forest-labs/FLUX.1-dev` | 1024×1024 × 1 | 24 / 3.5 | 1.15 · — |
| **CogVideoX-2b** (t2v) | `THUDM/CogVideoX-2b` | 720×480 × 49 | 50 / 6.0 | 1.0 · 8 |
| **Wan 2.1 T2V-1.3B** (t2v) | `Wan-AI/Wan2.1-T2V-1.3B-Diffusers` | 832×480 × 49 | 30 / 5.0 | 5.0 · 16 |
| **Wan 2.1 T2V-14B** (t2v) | `Wan-AI/Wan2.1-T2V-14B-Diffusers` | 832×480 × 49 | 30 / 5.0 | 5.0 · 16 |
| **HunyuanVideo** (t2v) | `hunyuanvideo-community/HunyuanVideo` | 720×480 × 49 | 30 / 6.0 | 7.0 · 24 |
| **Step-Video T2V** (t2v) | `stepfun-ai/Step-Video-T2V` (30B) | 992×544 × 51 | 30 / 9.0 | 0.0 · 25 |

Precision is chosen automatically by Veizik as the highest-fidelity setting that fits the detected
GPU with headroom (small models → bf16; large models → weight-only fp8; largest → resident/tiered).
The precision actually used is recorded per run in the manifest so a comparison stack can match it.

### Example — LTX-Video (the published 18.5 s figure)

```bash
# Veizik (guidance 3.0 / shift 6.0 / fps 24 come from the LTX model-card defaults in the table above):
veizik t2v "a cinematic commercial shot, coastal cliff at golden hour, 35mm film" \
           --model ltx --w 768 --h 448 --frames 49 --steps 30 --seed 42

# Framework-agnostic (reproduce the SAME workload on your own diffusers stack):
#   pipe = LTXPipeline.from_pretrained("Lightricks/LTX-Video", torch_dtype=bfloat16)
#   pipe(prompt, negative_prompt, width=768, height=448, num_frames=49,
#        num_inference_steps=30, guidance_scale=3.0, generator=torch.manual_seed(42))
```

### Example — FLUX.1-dev (the published 49.25 s figure)

```bash
# guidance 3.5 comes from the FLUX model-card default in the table above:
veizik t2i "an ultra-detailed luxury product shot, studio rim light" \
           --model flux --w 1024 --h 1024 --steps 24 --seed 42
```

## 4. Verified results so far

Verified = measured at the **confirmed** 420 W cap, sole tenant, on physical GPU1. `wall` is the
render (denoise) time reported by the runtime, separate from model load. Full per-run records
(JSONL + stats) are in [`benchmark/results/`](benchmark/results/); each figure is governed by the
claim ledger's status field. The tamper-evident run-manifest **format** is defined in
[`schemas/`](schemas/) with an example ([`run-manifest.json`](run-manifest.json)); a signed manifest
is attached as each result is packaged for external reproduction.

| Model | Workload | Wall (median) | Range | Runs | Peak VRAM | Peak host RAM | Status |
|---|---|---|---|---|---|---|---|
| LTX-Video | 768×448 · 49f · 30 steps | **18.5 s** | 17.4–20.4 s | 5 | 9.55 GB | ~18.7 GB | internal · repeated · 420 W verified |
| FLUX.1-dev | 1024² · 24 steps | **49.25 s** | 48.8–50.5 s | 4 warm | 12.78 GB | ~45.2 GB | internal · repeated · 420 W verified |
| CogVideoX-2b | 720×480 · 49f · 50 steps | **195.2 s** | 195–213 s | 3 | 16.29 GB | ~18.6 GB | internal · repeated · 420 W verified |
| Wan 2.1 T2V-1.3B | 832×480 · 49f · 30 steps | **149.4 s** | 149–150 s | 3 | 11.60 GB | ~23.4 GB | internal · repeated · 420 W verified |
| Wan 2.1 T2V-14B | 832×480 · 49f · 30 steps (seq offload) | **854.5 s** | — | 1 | 16.68 GB | **94.9 GB** | internal · single · 420 W verified |
| Step-Video T2V 30B | 992×544 · 51f · 30 steps | **1730 s** | — | 1 | 12.48 GB | **96.0 GB** | internal · single · 420 W verified |
| HunyuanVideo | 720×480 · 49f · 30 steps | **296.5 s** | — | 1 | 9.85 GB | 40.6 GB | internal · single · 420 W verified |

Two things a buyer should read off this: (1) the large models keep **peak VRAM low (12–17 GB)** by streaming
weights, but at the cost of **~95 GB host RAM** — so the real requirement for the 14B/30B tiers is a 24 GB GPU
**plus ~96 GB system RAM**. (2) HunyuanVideo now completes after a DiffSynth block-swap fix (RMSNorm wrapper attribute handling): 9.85 GB VRAM + 40.6 GB RAM, wall ~296 s.

## 5. What a Veizik number means

- **Every** figure carries its verification status (internal → externally reproduced → audited). We
  do not use comparative or "fastest/SOTA" language until a number is reproduced on ≥2 outside
  environments.
- A comparison against another framework is only labelled a **direct comparison** if all 14
  conditions in [`benchmark-method.md`](benchmark-method.md) match; otherwise it is a **separate
  measurement**.
- Runs are packaged with a tamper-evident **run-manifest** (model SHA, env lock, GPU UUID salted-hash,
  power cap, seed, output checksum, log signature) — see [`schemas/`](schemas/) and
  [`run-manifest.json`](run-manifest.json).
