# Benchmark

How Veizik measures performance, what kind of claim each published number is, and how to submit a
reproduction from your own hardware.

> ## Current status: render times are `measurement in progress`
>
> **No render-time or throughput figure is published.** Not a preliminary one, not a range, not an
> estimate. The only end-to-end number published today is **peak VRAM for LTX-13B: 9.55 GB**
> (RTX 3090, internally measured), plus block/engine-level numerical equivalence between the native
> engine path and its reference implementation.
>
> **External reproductions to date: none.** That is the honest state of a Public Preview, and
> closing that gap is what this directory is for.
>
> If you see a Veizik render-time number anywhere, it did not come from us.

---

## 1. Claim types

A number without its claim type is not meaningful. We separate them and never blur the boundary.

| Claim type | What it asserts | Published today? |
|---|---|---|
| **End-to-end, internally measured** | Wall-clock time, throughput or peak VRAM for a full generation on our hardware | Peak VRAM only (LTX-13B, 9.55 GB). All time and throughput: `measurement in progress` |
| **Engine-block numerical equivalence** | The native engine reproduces the reference math at block/engine level (relative L2 error) | Yes — 6 model-family engine paths checked at the **block/engine** level |
| **Component benchmark** | Isolated subsystem timing: a kernel, one DiT step, VAE decode, an offload swap | Internal only, not published |
| **External reproduction** | A third party reproduced a claim on their own hardware | **None yet** |

Wording rules that follow directly:

- Block/engine equivalence is a **component-level** check. It is not end-to-end generation
  verification, and we do not write it as "6/6 verified."
- 9.55 GB is a **memory** measurement, not a render time, and is labelled `internal`.
- No absolute phrasing — "all GPUs", "always fits", "never OOMs". Preview scope only.

The full methodology document is [`../benchmark-method.md`](../benchmark-method.md). This README is
the summary and the submission path.

---

## 2. Fixed conditions

A render-time claim is comparable only if every one of these is pinned and recorded. A run that
omits any of them is not publishable:

- **Model + commit** — family, exact weights identifier, repo/weights commit hash
- **Engine path** — native CUDA engine vs universal fallback, plus engine build commit
- **Resolution** — width × height
- **Frames** — frame count (`1` for a still)
- **Steps** — sampler step count
- **Sampler / scheduler** — name and parameters (shift, sigmas)
- **Seed** — fixed integer, recorded so the run regenerates
- **Precision** — compute dtype, and exactly which layers are quantized if any
- **Offload / placement** — hot/warm/cold tiering, offload target, whether CPU or disk offload engaged
- **Warm vs cold** — warm-up is **mandatory** before any timed window; cold and warm are never mixed
  in a single figure
- **OS / driver / runtime** — OS and kernel, driver, CUDA version, torch/diffusers versions
- **GPU** — model, VRAM class, and the **configured power limit**, which bounds clocks and is a
  first-order factor in throughput
- **Wall-clock boundaries** — the exact start and end of the timed window. Timing that does not
  state its boundaries is not accepted.

---

## 3. Timing boundaries

**Wall-clock is the headline**, not device-event-only timing. GPU event timers may accompany it;
they never replace it.

- **Start** = the first denoising step, *after* warm-up and *after* weight load and compile — unless
  the run is explicitly labelled **cold**, in which case start is process entry and load time is
  included by definition.
- **End** = the last step completes and the output tensor is materialized. VAE decode and file write
  are reported as separate stages, never folded silently into the denoise figure.

Contention invalidates a measurement. A timed run requires an otherwise idle GPU; a figure taken
while another job shares the device is not a benchmark, and we have thrown away our own numbers for
exactly this reason.

---

## 4. Run reports

Every measurement is captured as a **[`run-report-v1`](../schemas/run-report-v1.json)** document —
the same schema the CLI emits after a render when a user has given the separate, optional
performance-data consent.

The schema is a strict allowlist: `additionalProperties: false` at every level, and a mandatory
`privacy` object whose four members must all be `false`. It carries the machine (`hardware`), the
job shape (`workload`), and the outcome (`result`) — including per-stage timings, peak VRAM, and a
stable error code with the stage it stopped at when a run fails.

It carries no prompt, no input, no output, no filenames and no local paths. **Failure reports are as
valuable as successes** and are collected identically: an OOM at 24 GB on a specific model and
resolution is exactly the compatibility signal the preview needs.

See [`../TELEMETRY.md`](../TELEMETRY.md) for what is transmitted and how to turn it off
(`veizik telemetry disable`), and [`../PRIVACY.md`](../PRIVACY.md) for the legal basis. Declining
performance reporting never locks a feature you paid for.

---

## 5. Submitting a reproduction

External reproductions on hardware we do not own are the point of the Public Preview. We want the
failures as much as the successes.

**Open a GitHub issue** using the **Compatibility report** template (`.github/ISSUE_TEMPLATE/`),
and add the `benchmark` label. If the run failed rather than completed, use the **Failed render /
run report** template instead — see §5.6 below. Include:

1. **A `run-report-v1` JSON document per run.** Validate it before attaching:

   ```sh
   pip install check-jsonschema
   check-jsonschema --schemafile schemas/run-report-v1.json your-report.json
   ```

2. **All fixed conditions from §2** that the report does not already capture — particularly the
   **GPU power limit**, driver version, and whether the run was cold or warm.

3. **How many runs, and the spread.** A single run is an anecdote. Three or more of the same
   configuration, with the median and the range, is a measurement. Report the spread; do not report
   only your best run.

4. **Contention state.** Confirm the GPU was otherwise idle, or say plainly that it was not.

5. **Anything that surprised you.** A fallback that engaged, a profile that resolved differently
   than expected, a warning you ignored.

Do **not** include prompts, input files, generated media, or local paths. They are not needed to
verify a timing, and a bug report is not a place to publish your work.

**What we do with it.** Confirmed reproductions are published with attribution (or anonymously, your
choice) alongside the internal figure, including where they disagree with ours. A reproduction that
contradicts our number is the most useful thing you can send us, and we will publish that too.

**5.6 — If your run fails, submit it anyway.** OOM, an unsupported configuration, a driver
combination that will not load: use the **Failed render / run report** template. A machine that
cannot run Veizik is a compatibility data point, and it is the reason the hardware fields exist in
the report schema at all.

---

## 6. Contributing without running a benchmark

Enabling optional performance reporting (`veizik telemetry enable`) sends the same
`run-report-v1` documents automatically, and feeds the aggregate hardware-compatibility view that
`veizik benchmark` reports back to you for your own machine. It is opt-in, revocable, and never a
condition of any purchased feature.

---

## Related

- [`../benchmark-method.md`](../benchmark-method.md) — full methodology
- [`../schemas/run-report-v1.json`](../schemas/run-report-v1.json) — the report contract
- [`../examples/`](../examples/) — capsule examples; note their `timing` blocks are `measurement in progress` for the same reason as this page
- [`../SUPPORTED.md`](../SUPPORTED.md) — hardware support tiers
- [`../TELEMETRY.md`](../TELEMETRY.md) · [`../PRIVACY.md`](../PRIVACY.md)
