# Veizik — Local AI Video Runtime

Run large AI video and language models on the GPU you already own. **Low-VRAM, memory-resident execution for RTX 3090 / 4090 (24 GB)-class GPUs** — Wan · HunyuanVideo · LTX · Step-Video · FLUX · CogVideoX · Llama / Qwen. Local. No cloud rendering.

> 🚧 **In active development — launching soon.** Follow along at **[veizik.com](https://veizik.com)**.

*Veizik is a proprietary commercial runtime — **not open source**.*

---

## Why Veizik

- **Low-VRAM execution** — run 30B-class video models on 24 GB, no OOM. *Measured on an RTX 3090: LTX-13B (1280×704, 49f) ~100 s warm at **9.55 GB peak**; Step-Video 30B on a single 24 GB GPU.*
- **Weight-resident, fp16 tensor-core paths** — no per-step reloading; native CUDA DiT engines, numerically verified against the reference.
- **Crash-resume checkpointing** — a fault in one render doesn't throw away the good frames.
- **TimeMachine Render (beta)** — branch from a saved checkpoint: keep the approved prefix, regenerate only the tail with new prompt / CFG / style. Fix ~40% instead of re-rendering 100%.
- **ComfyUI drop-in** — swap `comfy` → `veizik` on your existing workflows and get low-VRAM + native-engine acceleration. Unsupported models still render via a universal fallback — no workflow left behind.

*Numbers are profile-conditional (resolution, frames, steps, model). VRAM is reported from framework allocators.*

---

## Status

Veizik is in active development. **Downloads, pricing, and documentation will be available at launch on [veizik.com](https://veizik.com).**

Want early access or updates? Watch this repo and check **[veizik.com](https://veizik.com)**.

---

**Keywords:** local AI video runtime · low VRAM · RTX 3090 / 4090 24 GB · Wan 2.2 · HunyuanVideo · LTX · Step-Video 30B · FLUX · CogVideoX · ComfyUI · crash resume · checkpoint branch rendering · text-to-video · image-to-video.

© Veizik. Proprietary. All rights reserved.
