# Known issues (v0.1.0 Public Preview)

Kept honestly, per release. Also see SUPPORTED.md for environment status.

1. **`veizik doctor` on machines without CUDA prints the default capability
   table** (it says so: `torch probe failed -> defaults`), which lists an
   RTX 3090-class profile even on a Mac. Cosmetic — the header shows `0 GPU(s)`.
   Fix queued for v0.1.1.
2. **`veizik run` / `veizik serve` (ComfyUI drop-in) are not in this preview**
   — the module ships next release. Calling them raises `ModuleNotFoundError`.
3. **`veizik t2v` requires your own Python env with `torch`/`diffusers`**
   (Linux + NVIDIA). The installer intentionally does not pull multi-GB
   dependencies; a pinned lockfile ships with the first stable release.
4. **Native CUDA DiT engines are not distributed yet** — the universal
   (diffusers-based) path is what renders in this preview. Native engine
   release assets (with checksums) ship per-platform later.
5. **Full-INT8 quantization of the residual-projection path is disabled** — it
   failed our accuracy bar (rel. err ≈ 6.6e-3) in internal testing. Mixed INT8
   only. We publish this so you don't wonder why the flag is off.
6. No code signing yet on any platform (the installer is `git clone` + venv,
   checksummed engine assets come with binary releases).
