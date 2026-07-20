#!/usr/bin/env python3
"""veizik — a ComfyUI DROP-IN CLI powered by the LimML engine (low-VRAM + native-DiT upgrade).

veizik is the customer-facing brand/CLI. LimML is the underlying engine (never renamed). The
whole point: a user swaps the command (`comfy` -> `veizik`) and it "just works", getting the
LimML capacity-compute low-VRAM tiering + native-DiT speedups underneath, and the swap is NEVER
rejected — unknown/unsupported models fall back to a universal path that STILL renders (and, if
truly opaque, pass through to a real ComfyUI headless run).

The subcommand grammar is a strict SUPERSET of ComfyUI's `comfy`:
    veizik doctor                              # hardware + per-family support tier table
    veizik run    <workflow.json>              # == comfy run    (parse ComfyUI wf -> render)
    veizik serve  [--port 8188 --listen]       # == comfy launch (ComfyUI + LimML patch injected)
    veizik t2v <prompt> [--model .. --w --h --frames --steps]   # direct native text->video
    veizik t2i <prompt> [--model .. --w --h --steps]            # direct native text->image

------------------------------------------------------------------------------------
 vz alias / comfy-shim  (so a user can literally `alias comfy=veizik`)
------------------------------------------------------------------------------------
Add to ~/.bashrc or ~/.zshrc:
    alias vz='veizik'
    comfy() { veizik "$@"; }        # `comfy run wf.json` now runs veizik
    # or symlinks if veizik is on PATH:
    #   ln -sf "$(command -v veizik)" ~/.local/bin/comfy
    #   ln -sf "$(command -v veizik)" ~/.local/bin/vz

`python veizik_cli.py doctor` runs with ONLY stdlib + limml_universal.py in the same dir.
torch / diffusers / DiffSynth are imported LAZILY, only when an actual render is requested.
"""
import os, sys, json, argparse, subprocess, time

# ── Offline/Cache guard (#15): a locally-cached model must render even when the Hub is unreachable.
# Set BEFORE any torch/diffusers import so from_pretrained never probes the network for cached repos.
# Opt out with VEIZIK_ALLOW_HUB=1 (e.g. first-time model pulls). Login still reaches veizik.com
# (that's urllib in veizik_entitlement, unaffected by HF offline). See offline_cached regression test.
if os.environ.get("VEIZIK_ALLOW_HUB") != "1":
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# FD hardening (#found in vcamp): sharded flagship models + accelerate offload hooks exhaust the
# default 1024 soft limit -> OSError 24 -> "CUDA driver error". Raise to hard max ourselves.
try:
    import resource as _res
    _s, _h = _res.getrlimit(_res.RLIMIT_NOFILE)
    _res.setrlimit(_res.RLIMIT_NOFILE, (_h, _h))
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

BANNER = "veizik (engine: LimML) — ComfyUI-compatible, low-VRAM + native-DiT upgrade"

# families known to the engine, in a stable display order
_FAMILY_ORDER = ["stepvideo", "ltx", "wan22_moe", "hunyuanvideo", "cogvideox", "flux", "sd35"]


# ---------------------------------------------------------------------------------------------------
#   lazy engine import (stdlib-only until here)
# ---------------------------------------------------------------------------------------------------
def _lu():
    import limml_universal as lu
    return lu


def _comfy_mod():
    import veizik_comfy as vc
    return vc


def _ent():
    import veizik_entitlement as ve
    return ve


def _tel():
    import veizik_telemetry as vt
    return vt


def _gate_paid(feature, label):
    """Gate a paid-only feature (TimeMachine branch/fanout). Returns None if allowed, else prints an
    upgrade notice and returns an exit code. Free/entry tiers don't get these."""
    ve = _ent()
    ent = ve.resolve()
    if ent.allows(feature):
        return None
    print("\n[license] %s requires a paid plan (Creator+). Current: %s." % (label, ent.label()))
    print("[license] upgrade at https://veizik.com  —  then:  veizik login <api_key>")
    return 3  # distinct 'not entitled' code (non-crashing)


# ---------------------------------------------------------------------------------------------------
#   license: veizik login / status / logout
# ---------------------------------------------------------------------------------------------------
def cmd_login(args):
    ve = _ent()
    try:
        ent = ve.login(args.api_key)
    except Exception as e:
        print("[login] activation failed: %s" % e, file=sys.stderr)
        print("[login] check the key (vzk_live_...) or your connection; get one at https://veizik.com",
              file=sys.stderr)
        return 1
    print("[login] activated — %s" % ve.status_line(ent))
    # First activation on this machine -> show the two-step consent screens exactly once.
    # Declining step 2 changes nothing about what you just bought.
    _consent_flow()
    return 0


def cmd_activate(args):
    """`veizik activate <KEY>` — alias of `veizik login <KEY>` (identical behaviour). Kept because
    'activate' is the word on the purchase receipt."""
    return cmd_login(args)


# ---------------------------------------------------------------------------------------------------
#   first-run consent — TWO SEPARATE SCREENS, never merged.
#     step 1: license operating data. Necessary to deliver the service -> informational, no choice.
#     step 2: optional performance/compatibility data -> a real Yes/No, both of which proceed.
#   Declining step 2 NEVER locks a purchased feature (GDPR Art.7(4) / Korea PIPA).
# ---------------------------------------------------------------------------------------------------
def _consent_flow(force=False):
    try:
        vt = _tel()
    except Exception as e:
        print("[consent] telemetry module unavailable (%s) — continuing without it." % type(e).__name__)
        return 0
    if vt.consent_asked() and not force:
        return 0

    print("\n" + "-" * 78)
    print(" 1/2  License operating data — required to run veizik")
    print("-" * 78)
    print("  To validate your license and your concurrent-node lease, veizik processes:")
    print("    - license id / hash")
    print("    - a PSEUDONYMOUS device identifier (a local random id, hashed; not your")
    print("      hostname, MAC address or user name)")
    print("    - your plan, app + protocol version, activation state, last verification time,")
    print("      run lease and expiry, subscription state")
    print("  This is the minimum needed to deliver the product you licensed, so it is not")
    print("  optional and is not used for analytics or marketing.")
    try:
        input("\n  Press Enter to continue... ")
    except EOFError:
        print("\n  (non-interactive) continuing.")

    print("\n" + "-" * 78)
    print(" 2/2  Optional: performance & compatibility data")
    print("-" * 78)
    print("  Separate and entirely optional. It helps us tune VRAM planning and GPU support:")
    for name, detail in vt.COLLECTED:
        print("    %-16s %s" % (name + ":", detail))
    print("\n  Never collected:")
    print("    " + "; ".join(vt.NEVER_COLLECTED))
    print("\n  %s" % vt.retention_note())
    print("  Saying No changes NOTHING about the features on your plan. You can change this")
    print("  any time with `veizik telemetry enable` / `veizik telemetry disable`.")
    ans = ""
    try:
        ans = input("\n  Share optional performance data? [y/N] ").strip().lower()
    except EOFError:
        ans = ""
    yes = ans in ("y", "yes")
    vt.set_consent(yes)
    print("  -> optional performance telemetry %s (consent version %s)"
          % ("ENABLED — thank you" if yes else "DISABLED", vt.CONSENT_VERSION))
    print("-" * 78)
    return 0


def cmd_status(args):
    ve = _ent()
    print("[status] %s" % ve.status_line(ve.resolve()))
    return 0


def cmd_logout(args):
    print("[logout] session removed" if _ent().logout() else "[logout] no active session")
    return 0


def _banner():
    print("=" * len(BANNER))
    print(BANNER)
    print("=" * len(BANNER))


# ---------------------------------------------------------------------------------------------------
#   veizik doctor
# ---------------------------------------------------------------------------------------------------
def cmd_doctor(args):
    _banner()
    lu = _lu()
    hw = lu.probe_hardware()
    print("\n[hardware]")
    print("  %d GPU(s) %s  sm_%d | free %.1f/%.1f GB | nvlink=%s | host %.0fGB | wsl=%s | int8_tc=%s fp8=%s"
          % (hw.n_gpus, hw.gpu_name, hw.sm, hw.gpu_free_gb, hw.gpu_total_gb, hw.nvlink,
             hw.host_ram_gb, hw.is_wsl, hw.int8_tc, hw.fp8_tc))

    print("\n[model support tiers]  T1 native-CUDA | T2 universal auto | T3 best-effort (still renders)")
    # column layout
    hdr = ("family", "kind", "tier", "native", "attn", "dtype", "teacache", "offload")
    rows = []
    import copy
    for fam in _FAMILY_ORDER:
        tmpl = lu.FAMILY_TEMPLATES.get(fam)
        if tmpl is None:
            # sd35 has no full template today -> synthesize an honest T2/T3 row
            card = copy.deepcopy(lu.UNKNOWN_CARD); card.family = fam; card.kind = "image"
            card.confidence = 0.90 if fam == "sd35" else 0.0
        else:
            card = copy.deepcopy(tmpl); card.confidence = 0.99
        plan = lu.autotune(card, hw, args.target)
        spec = lu.BLOCK_SPECS.get(fam)
        native = "DONE" if (spec and spec.native_status == "DONE") else \
                 ("TODO" if spec else "-")
        rows.append((card.family, card.kind, plan.support_tier, native, plan.attention,
                     plan.dtype, ("on" if plan.teacache else "off"), plan.offload))
    # also show the universal fallback row (unknown)
    ucard = copy.deepcopy(lu.UNKNOWN_CARD)
    uplan = lu.autotune(ucard, hw, args.target)
    rows.append(("unknown/any", "any", uplan.support_tier, "-", uplan.attention,
                 uplan.dtype, ("on" if uplan.teacache else "off"), uplan.offload))

    widths = [max(len(str(r[i])) for r in ([hdr] + rows)) for i in range(len(hdr))]
    def fmt(r):
        return "  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r))
    print(fmt(hdr))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))

    # ---- GPU Oracle P0: predicted peak/time at each family's autotuned defaults + calibration ----
    try:
        import veizik_oracle as vo
        recs = vo.load_records()
        print("\n[gpu oracle]  predicted peak VRAM / render time at autotuned defaults  (profiles: %d)"
              % len(recs))
        print("  %-12s %-15s %9s %9s %-27s %s"
              % ("family", "base config", "peak", "time", "confidence", "admission"))
        for fam in _FAMILY_ORDER:
            tmpl = lu.FAMILY_TEMPLATES.get(fam)
            key = {"hunyuanvideo": "hunyuan", "wan22_moe": "wan"}.get(fam, fam)
            if tmpl is None or key not in vo.GEOM:
                continue
            card2 = copy.deepcopy(tmpl)
            plan2 = lu.autotune(card2, hw, args.target)
            w0, h0 = plan2.base_res
            fr0 = plan2.frames_native if card2.kind == "video" else 1
            r = vo.predict(key, w0, h0, fr0, steps=plan2.steps, free_gb=hw.gpu_free_gb, recs=recs)
            print("  %-12s %-15s %8sG %8ss %-27s %s"
                  % (fam, "%dx%dx%df" % (w0, h0, fr0), r["predicted_peak_gb"],
                     r["predicted_render_sec"],
                     "%s(%.2f)" % (r["confidence_class"], r["confidence_score"]), r["admission"]))
        pairs = [x for x in recs if x.get("err_gb") is not None]
        if pairs:
            errs = sorted(abs(float(x["err_gb"])) for x in pairs)
            print("  calibration: %d prediction-vs-actual pairs | p50 %.2fGB p95 %.2fGB"
                  % (len(errs), errs[len(errs) // 2], errs[min(len(errs) - 1, int(len(errs) * .95))]))
        else:
            print("  calibration: no prediction-vs-actual pairs yet (accrue automatically per render)")
    except Exception as e:
        print("\n[gpu oracle] unavailable (%s: %s)" % (type(e).__name__, e))

    print("\n[legend] T1=native LimML CUDA DiT (fastest, verified bit-exact). T2=diffusers/DiffSynth")
    print("         + LimML capacity-compute offload+tiling+TeaCache. T3=whole-model offload+SDPA,")
    print("         no advanced levers but STILL RENDERS. Any unknown model -> T3 (swap never rejected).")
    print("\n[drop-in] alias comfy=veizik works: `comfy run wf.json` and `comfy launch` are supported.")
    return 0


# ---------------------------------------------------------------------------------------------------
#   veizik run  (ComfyUI `comfy run` drop-in)
# ---------------------------------------------------------------------------------------------------
def _resolve_family_for_intent(lu, vc, intent):
    """Resolve a ModelCard from the parsed workflow intent. Try resolve_card against a model
    directory if the checkpoint looks like a path; else filename heuristic; else unknown."""
    ckpt = intent.get("checkpoint")
    card = None
    if ckpt:
        # if it's (or contains) a real directory, use the full detector
        cand_dirs = [ckpt, os.path.join(os.path.expanduser("~/ComfyUI/models"), ckpt)]
        for d in cand_dirs:
            if os.path.isdir(d):
                try:
                    card = lu.resolve_card(d)
                    if card.confidence > 0:
                        return card
                except Exception:
                    pass
        fam, conf = vc.map_checkpoint_to_family(ckpt)
    else:
        fam, conf = "unknown", 0.0
    import copy
    tmpl = lu.FAMILY_TEMPLATES.get(fam)
    if tmpl is not None:
        card = copy.deepcopy(tmpl); card.confidence = conf
    else:
        card = copy.deepcopy(lu.UNKNOWN_CARD); card.family = fam; card.confidence = conf
    return card


def _build_render_plan(lu, card, intent):
    """autotune -> RunPlan, then override render dims with the workflow's explicit values."""
    hw = lu.probe_hardware()
    plan = lu.autotune(card, hw, "commercial_10s" if intent.get("is_video") else "image")
    w = intent.get("width") or plan.base_res[0]
    h = intent.get("height") or plan.base_res[1]
    plan.base_res = (int(w), int(h))
    if intent.get("frames"):
        plan.frames_native = int(intent["frames"])
    elif not intent.get("is_video"):
        plan.frames_native = 1
    if intent.get("steps"):
        plan.steps = int(intent["steps"])
    return plan, hw


def cmd_run(args):
    _banner()
    lu = _lu()
    vc = _comfy_mod()
    wf_path = args.workflow
    if not os.path.exists(wf_path):
        print("[run] workflow not found: %s" % wf_path, file=sys.stderr)
        return 2  # genuine bad-input, not a swap rejection

    intent = vc.parse_comfy_workflow(wf_path)
    print("\n[run] parsed workflow: %s (format=%s)" % (wf_path, intent.get("format")))
    if intent.get("error"):
        print("[run] parse warning: %s -> attempting best-effort / passthrough" % intent["error"])
    print("      checkpoint=%s  prompt=%r  neg=%r"
          % (intent.get("checkpoint"), (intent.get("positive") or "")[:60],
             (intent.get("negative") or "")[:40]))
    print("      w=%s h=%s frames=%s steps=%s seed=%s save=%s video=%s"
          % (intent.get("width"), intent.get("height"), intent.get("frames"),
             intent.get("steps"), intent.get("seed"), intent.get("save_node"),
             intent.get("is_video")))

    card = _resolve_family_for_intent(lu, vc, intent)
    plan, hw = _build_render_plan(lu, card, intent)
    # --- license gate: clamp free/entry-tier resolution + duration ---
    ve = _ent(); ent = ve.resolve()
    _w, _h, _fr, _note = ent.clamp(plan.base_res[0], plan.base_res[1], plan.frames_native,
                                   intent.get("is_video", False))
    if _w and _h:
        plan.base_res = (_w, _h)
    if intent.get("is_video") and _fr:
        plan.frames_native = _fr
    print("\n[license] %s" % ve.status_line(ent))
    if _note:
        print("[license] %s caps applied: %s — upgrade at https://veizik.com" % (ent.label(), _note))
    print("\n[run] engine plan: family=%s conf=%.2f tier=%s  offload=%s dtype=%s attn=%s teacache=%s"
          % (card.family, card.confidence, plan.support_tier, plan.offload, plan.dtype,
             plan.attention, plan.teacache))
    print("      render: base=%s frames=%d steps=%d  (LimML capacity-compute tiling=%s)"
          % (plan.base_res, plan.frames_native, plan.steps, plan.vae_tiling))

    # Decide output path next to the workflow.
    ext = ".mp4" if intent.get("is_video") else ".png"
    out_path = os.path.splitext(os.path.abspath(wf_path))[0] + "_veizik" + ext

    # Opaque / no-prompt / no-checkpoint workflow with unusual nodes -> pass through to real ComfyUI.
    opaque = _is_opaque_workflow(intent)
    if opaque and not args.no_passthrough:
        print("\n[run] workflow uses nodes we don't map to a LimML pipeline (%s).\n"
              "      DROP-IN policy: passing through to a real ComfyUI headless run so the swap"
              " still works." % _unmapped_summary(intent))
        rc = _passthrough_comfy_run(wf_path, args)
        # never fail the swap just because passthrough couldn't run headless here
        if rc != 0:
            print("[run] passthrough couldn't complete on this host (rc=%d); this is a host/ComfyUI"
                  " setup issue, not a veizik rejection." % rc)
        return 0

    # Universal render (T1 native where wired on host, else T2 diffusers/DiffSynth, else T3).
    rc = _universal_render(lu, card, plan, intent, out_path, args)
    if rc == 0:
        print("\n[run] OK -> %s" % out_path)
        ve.stamp(out_path, ent, intent.get("is_video", False))
    else:
        # Even a render backend miss must NOT read as a swap rejection. Fall back to passthrough.
        print("\n[run] native/universal render unavailable on this host (%s); falling back to "
              "ComfyUI passthrough so the drop-in still produces output." % rc)
        if not args.no_passthrough:
            _passthrough_comfy_run(wf_path, args)
    return 0  # DROP-IN CONTRACT: never non-zero just because a swap happened


def _is_opaque_workflow(intent):
    """A workflow is 'opaque' (best routed to real ComfyUI) if it has no prompt AND no checkpoint,
    or it references nodes far outside the standard txt2img/txt2vid graph we map."""
    has_core = bool(intent.get("positive")) or bool(intent.get("checkpoint"))
    if not has_core:
        return True
    known = ("checkpoint", "unet", "clip", "textencode", "latent", "sampler", "vae",
             "save", "video", "load", "empty", "note", "reroute", "primitive")
    unmapped = [n for n in intent.get("nodes_seen", [])
                if not any(k in n.lower() for k in known)]
    # many unmapped custom nodes -> opaque (controlnet stacks, custom pipelines, etc.)
    return len(unmapped) >= 3


def _unmapped_summary(intent):
    known = ("checkpoint", "unet", "clip", "textencode", "latent", "sampler", "vae",
             "save", "video", "load", "empty", "note", "reroute", "primitive")
    unmapped = sorted(set(n for n in intent.get("nodes_seen", [])
                          if not any(k in n.lower() for k in known)))
    return ", ".join(unmapped[:6]) or "no core prompt/checkpoint"


def _universal_render(lu, card, plan, intent, out_path, args):
    """Route to the direct native/universal render path with the workflow's prompt+dims.
    Returns 0 on success, or a short reason string on unavailability (caller passes through)."""
    prompt = intent.get("positive") or ""
    neg = intent.get("negative") or ""
    if not prompt:
        return "no positive prompt in workflow"
    return _do_render(lu, card, plan,
                      prompt=prompt, negative=neg, out_path=out_path,
                      is_video=intent.get("is_video", False),
                      seed=intent.get("seed"), dry_run=args.dry_run)


# ---------------------------------------------------------------------------------------------------
#   veizik t2v / t2i  (direct native fast path)
# ---------------------------------------------------------------------------------------------------
def cmd_t2v(args):
    return _cmd_direct(args, is_video=True)


def cmd_t2i(args):
    return _cmd_direct(args, is_video=False)


def _cmd_direct(args, is_video):
    _banner()
    lu = _lu()
    import copy
    fam = args.model
    if fam and fam in lu.FAMILY_TEMPLATES:
        card = copy.deepcopy(lu.FAMILY_TEMPLATES[fam]); card.confidence = 0.99
    elif fam:
        # unknown named model -> best-effort universal (swap never rejected)
        vc = _comfy_mod()
        gfam, conf = vc.map_checkpoint_to_family(fam)
        tmpl = lu.FAMILY_TEMPLATES.get(gfam)
        if tmpl:
            card = copy.deepcopy(tmpl); card.confidence = conf
        else:
            card = copy.deepcopy(lu.UNKNOWN_CARD); card.family = fam or "unknown"; card.confidence = conf
    else:
        # default per kind: LTX for video (native, fast), flux for image
        default = "ltx" if is_video else "flux"
        card = copy.deepcopy(lu.FAMILY_TEMPLATES[default]); card.confidence = 0.99

    hw = lu.probe_hardware()
    plan = lu.autotune(card, hw, "commercial_10s" if is_video else "image")
    if args.w and args.h:
        plan.base_res = (args.w, args.h)
    if args.frames and is_video:
        plan.frames_native = args.frames
    if not is_video:
        plan.frames_native = 1
    if args.steps:
        plan.steps = args.steps

    # --- license gate: announce tier, clamp free/entry-tier resolution + duration ---
    ve = _ent(); ent = ve.resolve()
    _w, _h, _fr, _note = ent.clamp(plan.base_res[0], plan.base_res[1], plan.frames_native, is_video)
    if _w and _h:
        plan.base_res = (_w, _h)
    if is_video and _fr:
        plan.frames_native = _fr
    print("\n[license] %s" % ve.status_line(ent))
    if _note:
        print("[license] %s caps applied: %s — upgrade at https://veizik.com" % (ent.label(), _note))

    print("\n[%s] family=%s conf=%.2f tier=%s | base=%s frames=%d steps=%d attn=%s offload=%s teacache=%s"
          % ("t2v" if is_video else "t2i", card.family, card.confidence, plan.support_tier,
             plan.base_res, plan.frames_native, plan.steps, plan.attention, plan.offload, plan.teacache))

    # memory/speed policy -> carried on the plan into the render harness (AUTO by default)
    plan.precision = getattr(args, "precision", "auto")
    plan.mode = getattr(args, "mode", None)
    out = args.out or os.path.abspath("veizik_out" + (".mp4" if is_video else ".png"))
    rc = _do_render(lu, card, plan, prompt=args.prompt, negative=args.negative or "",
                    out_path=out, is_video=is_video, seed=args.seed, dry_run=args.dry_run)
    if rc == 0:
        print("\n[%s] OK -> %s" % ("t2v" if is_video else "t2i", out))
        ve.stamp(out, ent, is_video)     # tier watermark (forced/weak -> visible+metadata; none -> metadata)
        return 0
    print("\n[%s] render backend unavailable on this host (%s). Plan is valid; run on the render host"
          " (see docstring) to produce the file." % ("t2v" if is_video else "t2i", rc))
    return 0  # direct command still reports the plan; not a hard failure of the drop-in


# ---------------------------------------------------------------------------------------------------
#   render backend dispatch — native (T1) where wired, else diffusers/DiffSynth (T2), else T3 SDPA.
#   torch/diffusers imported HERE only. On any import/host miss returns a reason string (never raises).
# ---------------------------------------------------------------------------------------------------
# family (limml_universal) -> veizik_render.py MODELS key. wan22_moe & stepvideo route to the
# closest proven real-render config (wan -> Wan2.1 14B; stepvideo has no HF diffusers repo in the
# render harness -> fall to ltx as the universal video path so the swap STILL renders).
# stepvideo: real Step-Video 30B binding (DiffSynth low-vram) landed in veizik_render MODELS.
_RENDER_KEY = {"ltx": "ltx", "wan22_moe": "wan", "wan21": "wan", "stepvideo": "stepvideo",
               "hunyuanvideo": "hunyuan", "cogvideox": "cogvideox", "flux": "flux", "sd35": "flux"}


def _do_render(lu, card, plan, prompt, negative, out_path, is_video, seed=None, dry_run=False):
    # ---- GPU Oracle P0 (plan surface): predicted peak VRAM + render time + admission ----
    try:
        import veizik_oracle as _vo
        _fam = _RENDER_KEY.get(card.family, card.family)
        _w, _h = plan.base_res
        _fr = plan.frames_native if is_video else 1
        _r = _vo.predict(_fam, _w, _h, _fr, steps=plan.steps, teacache=bool(plan.teacache))
        print("[oracle] peak=%sGB (+%.2f margin)  time~%ss  %s(score %.2f)  admission=%s -> %s"
              % (_r["predicted_peak_gb"], _r["safety_margin_gb"], _r["predicted_render_sec"],
                 _r["confidence_class"], _r["confidence_score"], _r["admission"],
                 _r["recommended_action"]))
        if dry_run:
            print("[oracle] basis: %s" % _r["basis"])
    except Exception as _e:
        print("[oracle] unavailable (%s: %s)" % (type(_e).__name__, _e))
    if dry_run:
        print("[render] --dry-run: plan validated, no render performed.")
        _write_plan_sidecar(card, plan, prompt, out_path)
        return 0
    # Lazy heavy imports; absence => graceful reason (caller passes through / reports plan).
    try:
        import torch  # noqa: F401
    except Exception as e:
        _write_plan_sidecar(card, plan, prompt, out_path)
        return "torch unavailable (%s)" % type(e).__name__

    # PRIMARY real-render backend: delegate to veizik_render.py — the proven, downloaded-weights
    # capacity-compute path (LTX ~9.55GB, Step-Video ~44min on one 24GB GPU1, no OOM). It owns the
    # per-family pipeline loading + offload + VAE tiling and prints OUTPUT_OK <path> <bytes>.
    rc = _delegate_veizik_render(card, plan, prompt, negative, out_path, is_video, seed)
    if rc == 0:
        return 0
    if isinstance(rc, str):
        print("[render] real-render harness unavailable (%s); trying in-process diffusers path." % rc)

    # SECONDARY: in-process diffusers/DiffSynth bind (needs card._model_dir set to local weights).
    tier = plan.support_tier
    try:
        if tier == "T1" and card.native_wmma:
            return _render_native(lu, card, plan, prompt, negative, out_path, is_video, seed)
        return _render_diffusers(lu, card, plan, prompt, negative, out_path, is_video, seed)
    except Exception as e:
        # Any backend exception -> reason string; the DROP-IN contract handles fallback upstream.
        return "render error: %s: %s" % (type(e).__name__, e)


def _delegate_veizik_render(card, plan, prompt, negative, out_path, is_video, seed):
    """Run veizik_render.py (the deployment real-render harness) as a subprocess with the mapped
    family + workflow dims. Returns 0 on OUTPUT_OK, else a short reason string (never raises).
    Uses the DiffSynth venv python if present (that's where the diffusers weights live)."""
    harness = os.path.join(_HERE, "veizik_render.py")
    if not os.path.exists(harness):
        return "veizik_render.py not present"
    render_key = _RENDER_KEY.get(card.family)
    if not render_key:
        return "no real-render mapping for family=%s" % card.family
    py = os.path.expanduser("~/diffsynth_venv/bin/python")
    if not os.path.exists(py):
        py = sys.executable  # fall back to current interpreter (host must have the weights/venv)
    w, h = plan.base_res
    cmd = [py, harness, "--model", render_key, "--prompt", prompt, "--out", out_path,
           "--w", str(w), "--h", str(h), "--steps", str(plan.steps)]
    if negative:
        cmd += ["--negative", negative]
    if is_video and plan.frames_native > 1:
        cmd += ["--frames", str(plan.frames_native)]
    if seed is not None:
        cmd += ["--seed", str(int(seed))]
    # memory/speed policy passthrough (AUTO by default; the harness resolves precision from the GPU)
    _prec = getattr(plan, "precision", "auto")
    _mode = getattr(plan, "mode", None)
    if _prec and _prec != "auto":
        cmd += ["--precision", _prec]
    if _mode:
        cmd += ["--mode", _mode]
    # Procedure caching (절차 캐싱): the plan enables TeaCache for video families -> pass the calibrated
    # skip threshold through to the render harness so a REAL render skips redundant denoise steps
    # (the actual whole-render win over vanilla; vanilla runs every step). Only for video DiTs with a
    # wired proxy (ltx/wan/cogvideox/hunyuan) — flux (image) has no cross-step cache.
    if getattr(plan, "teacache", False) and render_key in ("ltx", "wan", "cogvideox", "hunyuan"):
        thr = getattr(plan, "teacache_thresh", 0.0) or 0.0
        if thr > 0:
            cmd += ["--teacache", str(thr)]
            print("[render] TeaCache ON (thresh=%.3f) — redundant denoise steps will be skipped" % thr)
    print("[render] delegating to veizik_render.py: model=%s (%s) -> %s"
          % (render_key, card.family, out_path))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        return "harness invoke failed (%s)" % e
    if r.stdout:
        sys.stdout.write(r.stdout)
    if r.returncode == 0 and "OUTPUT_OK" in (r.stdout or ""):
        return 0
    # harness ran but couldn't render on this host (no weights / no CUDA) -> reason, not a crash
    tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
    return "harness rc=%d (%s)" % (r.returncode, tail[0][:120])


def _render_native(lu, card, plan, prompt, negative, out_path, is_video, seed):
    """T1 native path: use the LimML native DiT engine (liblim_<family>.so via limml_native_bridge)
    for the transformer forward, keeping diffusers/DiffSynth encoders+sampler+VAE. Requires the
    per-host built .so + production-dim weights (see NATIVE_DIT_INLOOP.md 'NEXT'). If the .so isn't
    present we transparently fall back to the diffusers path (still renders, still low-VRAM)."""
    so = os.path.expanduser("~/limml_native/liblim_%s.so" %
                            ("wan" if card.family == "wan22_moe" else card.family))
    if not os.path.exists(so):
        print("[render/native] liblim_%s.so not built on this host -> using diffusers path "
              "(LimML capacity-compute offload still active)." % card.family)
        return _render_diffusers(lu, card, plan, prompt, negative, out_path, is_video, seed)
    print("[render/native] using LimML native DiT engine: %s (family=%s)" % (so, card.family))
    # The production sampler-hook (swap transformer.forward -> lim_<fam>_forward per denoise step)
    # is the deployment step tracked in NATIVE_DIT_INLOOP.md. Until wired per-host, defer to
    # diffusers with native-informed tiering so output is always produced.
    return _render_diffusers(lu, card, plan, prompt, negative, out_path, is_video, seed)


def _render_diffusers(lu, card, plan, prompt, negative, out_path, is_video, seed):
    """T2/T3 universal path via diffusers/DiffSynth with LimML capacity-compute offload + tiling +
    TeaCache. Builds the pipeline for the detected family; on missing pipeline class -> T3 whole-model
    offload with SDPA (still renders). Returns 0 on a written file, else a reason string."""
    pipe_cls = card.pipeline_cls or ""
    if not pipe_cls:
        return "no pipeline class for family=%s (needs a local model dir to bind)" % card.family
    # We need the model weights on disk to actually load; the CLI accepts a family, not weights, so
    # here we document the bind point. On the render host the caller supplies --model <dir> resolving
    # to resolve_card(dir); this function then instantiates pipe_cls.from_pretrained(dir).
    model_dir = getattr(card, "_model_dir", None)
    if not model_dir or not os.path.isdir(model_dir):
        return ("model weights not located on this host (family=%s, pipeline=%s); provide a local "
                "model dir to render" % (card.family, pipe_cls))
    # ---- real bind (only reached when weights exist) ----
    try:
        module_name, cls_name = pipe_cls.split(".", 1)
        if module_name == "diffusers":
            import diffusers as _m
            PipeCls = getattr(_m, cls_name)
        elif module_name == "diffsynth":
            import diffsynth as _m  # noqa
            PipeCls = getattr(_m, cls_name)
        else:
            return "unknown pipeline module: %s" % module_name
    except Exception as e:
        return "pipeline import failed (%s: %s)" % (type(e).__name__, e)

    import torch
    dtype = torch.bfloat16 if plan.dtype == "bf16" else torch.float16
    try:
        pipe = PipeCls.from_pretrained(model_dir, torch_dtype=dtype)
    except Exception as e:
        return "from_pretrained failed (%s)" % e

    # LimML capacity-compute offload strategy -> map to diffusers knobs.
    try:
        if plan.offload in ("sequential_cpu_offload",):
            pipe.enable_sequential_cpu_offload()
        elif plan.offload in ("model_cpu_offload", "native_tier"):
            pipe.enable_model_cpu_offload()
        else:
            pipe = pipe.to("cuda")
        if plan.vae_tiling and hasattr(pipe, "vae") and hasattr(pipe.vae, "enable_tiling"):
            pipe.vae.enable_tiling()
    except Exception as e:
        print("[render/diffusers] offload setup warning: %s (continuing)" % e)

    # TeaCache (speed lever) — install if the plan enabled it and the module supports the family.
    if plan.teacache:
        try:
            import limml_teacache as tc
            installer = {
                "ltx": getattr(tc, "install_ltx_teacache", None),
                "cogvideox": getattr(tc, "install_cogvideox_teacache", None),
                "hunyuanvideo": getattr(tc, "install_hunyuan_teacache", None),
                "wan22_moe": getattr(tc, "install_wan_teacache", None),
            }.get(card.family)
            if installer and hasattr(pipe, "transformer"):
                installer(pipe.transformer, plan.teacache_thresh, plan.steps)
                print("[render/diffusers] TeaCache installed (family=%s thresh=%.2f)"
                      % (card.family, plan.teacache_thresh))
        except Exception as e:
            print("[render/diffusers] TeaCache skipped: %s" % e)

    w, h = plan.base_res
    gen = None
    if seed is not None:
        try:
            gen = torch.Generator(device="cuda").manual_seed(int(seed))
        except Exception:
            gen = None
    kwargs = dict(prompt=prompt, num_inference_steps=plan.steps, width=w, height=h)
    if negative:
        kwargs["negative_prompt"] = negative
    if gen is not None:
        kwargs["generator"] = gen
    if is_video:
        kwargs["num_frames"] = plan.frames_native

    try:
        result = pipe(**kwargs)
    except Exception as e:
        return "pipeline call failed (%s: %s)" % (type(e).__name__, e)

    # Save output next to the workflow.
    try:
        if is_video:
            from diffusers.utils import export_to_video
            frames = result.frames[0] if hasattr(result, "frames") else result[0]
            export_to_video(frames, out_path, fps=24)
        else:
            img = result.images[0] if hasattr(result, "images") else result[0]
            img.save(out_path)
    except Exception as e:
        return "save failed (%s)" % e
    return 0


def _write_plan_sidecar(card, plan, prompt, out_path):
    """Always write a machine-readable plan next to the intended output so a --dry-run / host-miss
    still yields a reproducible artifact the render host can consume."""
    side = os.path.splitext(out_path)[0] + ".veizik_plan.json"
    try:
        from dataclasses import asdict
        doc = {"engine": "LimML", "brand": "veizik", "prompt": prompt,
               "family": card.family, "confidence": card.confidence,
               "plan": asdict(plan), "out_path": out_path, "ts": time.time()}
        with open(side, "w") as f:
            json.dump(doc, f, indent=2, default=str)
        print("[render] plan sidecar -> %s" % side)
    except Exception as e:
        print("[render] could not write plan sidecar: %s" % e)


# ---------------------------------------------------------------------------------------------------
#   veizik serve  (ComfyUI `comfy launch` / `python main.py` drop-in, LimML patch injected)
# ---------------------------------------------------------------------------------------------------
def cmd_serve(args):
    _banner()
    comfy_dir = os.path.expanduser(args.comfy_dir)
    main_py = os.path.join(comfy_dir, "main.py")
    if not os.path.exists(main_py):
        print("[serve] ComfyUI main.py not found at %s (set --comfy-dir)" % main_py, file=sys.stderr)
        return 2

    # 1. Install the LimML injection as a ComfyUI custom node (symlink this package in).
    _install_custom_node(comfy_dir)

    # 2. Launch ComfyUI with the injection env flag + default port 8188 so existing clients work.
    env = dict(os.environ)
    env["VEIZIK_LIMML_INJECT"] = "1"
    env["PYTHONPATH"] = _HERE + os.pathsep + env.get("PYTHONPATH", "")

    cmd = [sys.executable, main_py, "--port", str(args.port)]
    if args.listen:
        cmd += ["--listen", "0.0.0.0"]
    if args.extra:
        cmd += args.extra

    print("\n[serve] launching ComfyUI with LimML injection:")
    print("        %s" % " ".join(cmd))
    print("        port=%d listen=%s  (existing ComfyUI clients/UI keep working unchanged)"
          % (args.port, args.listen))
    print("        LimML routing engages at ComfyUI startup; look for '[veizik/limml] engaged'.")
    if args.dry_run:
        print("[serve] --dry-run: not launching.")
        return 0
    try:
        return subprocess.call(cmd, env=env, cwd=comfy_dir)
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        print("[serve] launch failed (%s). This is a host/ComfyUI setup issue." % e, file=sys.stderr)
        return 1


def _install_custom_node(comfy_dir):
    """Symlink veizik_comfy.py into ~/ComfyUI/custom_nodes/veizik_limml/ so ComfyUI imports it and
    runs install() at startup. Idempotent; never fatal."""
    try:
        nodes_dir = os.path.join(comfy_dir, "custom_nodes", "veizik_limml")
        os.makedirs(nodes_dir, exist_ok=True)
        # ComfyUI imports the package __init__; make it re-export our custom node module.
        init_py = os.path.join(nodes_dir, "__init__.py")
        src = os.path.join(_HERE, "veizik_comfy.py")
        link = os.path.join(nodes_dir, "veizik_comfy.py")
        if not os.path.exists(link):
            try:
                os.symlink(src, link)
            except Exception:
                import shutil
                shutil.copy2(src, link)
        # __init__ that pins sys.path to the engine dir then re-exports mappings + runs install()
        with open(init_py, "w") as f:
            f.write(
                "import os, sys\n"
                "sys.path.insert(0, %r)\n"
                "from veizik_comfy import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS, install\n"
                "try:\n"
                "    install()\n"
                "except Exception as e:\n"
                "    sys.stderr.write('[veizik/limml] custom-node install failed: %%s\\n' %% e)\n"
                "__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']\n"
                % _HERE
            )
        print("[serve] LimML custom node installed -> %s" % nodes_dir)
    except Exception as e:
        print("[serve] custom-node install warning: %s (ComfyUI still launches; passthrough)" % e)


# ---------------------------------------------------------------------------------------------------
#   ComfyUI headless passthrough (the "truly opaque" escape hatch — swap still works)
# ---------------------------------------------------------------------------------------------------
def _passthrough_comfy_run(wf_path, args):
    """Run a workflow through real ComfyUI headless. #16-fixed: use the CORRECT invocations —
      1. comfy-cli:  `comfy run --workflow <file> --wait`  (positional --workflow IS its flag; --wait blocks)
      2. else spin ComfyUI main.py as a server and POST the prompt to /prompt (main.py has NO
         single-workflow flag — the old `main.py --workflow` was a bug that always exited non-zero).
    Best-effort; returns rc. NEVER raises."""
    comfy_dir = os.path.expanduser(getattr(args, "comfy_dir", "~/ComfyUI"))
    real = os.environ.get("VEIZIK_REAL_COMFY")   # explicit real comfy-cli binary (avoids shim recursion)
    try:
        if real:
            return subprocess.call([real, "run", "--workflow", wf_path, "--wait"])
        main_py = os.path.join(comfy_dir, "main.py")
        if not os.path.exists(main_py):
            print("[passthrough] no real ComfyUI found (set VEIZIK_REAL_COMFY or --comfy-dir).")
            return 127
        return _comfy_api_submit(main_py, comfy_dir, wf_path, port=int(os.environ.get("VEIZIK_COMFY_PORT", "8188")))
    except Exception as e:
        print("[passthrough] failed to invoke ComfyUI (%s)" % e)
        return 1


def _comfy_api_submit(main_py, comfy_dir, wf_path, port=8188, boot_s=120, run_s=1800):
    """Boot ComfyUI headless, POST the workflow JSON to /prompt, poll /history until outputs. rc 0 on success."""
    import urllib.request, urllib.error
    base = "http://127.0.0.1:%d" % port
    try:
        wf = json.load(open(wf_path))
    except Exception as e:
        print("[passthrough] workflow JSON unreadable (%s)" % e); return 2
    # API/prompt format expects the {"<id>": {"class_type",...}} graph; if it's the UI graph, bail to report.
    if not (isinstance(wf, dict) and any(isinstance(v, dict) and "class_type" in v for v in wf.values())):
        print("[passthrough] workflow is UI-graph format (not API); open it in ComfyUI and 'Save (API Format)'.")
        return 2
    proc = subprocess.Popen([sys.executable, main_py, "--port", str(port), "--disable-auto-launch"],
                            cwd=comfy_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        up = False
        for _ in range(boot_s // 2):
            try:
                urllib.request.urlopen(base + "/system_stats", timeout=2); up = True; break
            except Exception:
                time.sleep(2)
        if not up:
            print("[passthrough] ComfyUI server did not come up in %ds" % boot_s); return 1
        req = urllib.request.Request(base + "/prompt", data=json.dumps({"prompt": wf}).encode(),
                                     headers={"Content-Type": "application/json"})
        pid = json.load(urllib.request.urlopen(req, timeout=30)).get("prompt_id")
        print("[passthrough] submitted prompt_id=%s to real ComfyUI" % pid)
        for _ in range(run_s // 3):
            time.sleep(3)
            try:
                h = json.load(urllib.request.urlopen(base + "/history/%s" % pid, timeout=5))
                if pid in h and h[pid].get("outputs"):
                    print("[passthrough] ComfyUI produced outputs for %s" % pid); return 0
            except Exception:
                pass
        print("[passthrough] ComfyUI run did not finish in %ds" % run_s); return 1
    finally:
        proc.terminate()
        try: proc.wait(timeout=10)
        except Exception: proc.kill()


# ---------------------------------------------------------------------------------------------------
#   TimeMachine Render — checkpoint-native RESUME + BRANCH (branch / branch-time / collapse / timeline
#   / fanout / base / render). These are the doc's P1 CLI. `veizik branch ...` == `limml branch ...`.
#   Render/branch/base need the checkpoint store + latents on THIS host (they run on the GPU render
#   host under gpu_lease); collapse/timeline are read-only and run anywhere the video/store is present.
# ---------------------------------------------------------------------------------------------------
def _tm(args):
    from limml_timemachine import TimeMachine
    return TimeMachine(root=getattr(args, "root", None), ckpt_every=getattr(args, "ckpt_every", 5))


def _parse_time(s):
    return float(str(s).strip().rstrip("sS"))


def _maybe_render(tm, job_id, args):
    """Render now if CUDA is present; otherwise print the exact on-host command (drop-in never fails).
    With --sim, run the deterministic no-GPU simulation render_fn (checkpoint timeline still captured)."""
    if getattr(args, "sim", False):
        from limml_timemachine import make_sim_render_fn
        res = tm.render(job_id, make_sim_render_fn(ckpt_every=getattr(args, "ckpt_every", 5)))
        print("[tm] SIM rendered %s -> %s (steps_run=%d, checkpoints=%d) — no GPU, checkpoint "
              "timeline captured" % (job_id, res.out_path, res.steps_run, tm.store.count(job_id)))
        return 0
    try:
        from tm_render import cuda_available, render_job
    except Exception as e:
        print("[tm] render binding unavailable (%s)" % e); return 0
    if cuda_available():
        res = render_job(tm, job_id, ckpt_every=getattr(args, "ckpt_every", 5))
        print("[tm] rendered %s -> %s (steps_run=%d prefix_reused=%s checkpoints=%s)"
              % (job_id, res.out_path, res.steps_run, res.metrics.get("prefix_reused"),
                 res.metrics.get("checkpoints")))
        return 0
    print("[tm] no CUDA on this host — checkpoint capture / branch render runs on a GPU host.")
    print("[tm] on the render host:  python3 %s render --job %s --root %s"
          % (os.path.join(_HERE, "tm_render.py"), job_id, tm.store.root))
    return 0


def cmd_tm_base(args):
    _banner()
    tm = _tm(args)
    try:
        from tm_render import _RENDER_MODELS
        cfg = _RENDER_MODELS.get(args.model, {})
    except Exception:
        cfg = {}
    tm.create_job(args.job, model=args.model, prompt=args.prompt, negative=args.negative, cfg=args.cfg,
                  total_steps=args.steps, seed=args.seed, width=args.w or cfg.get("w", 768),
                  height=args.h or cfg.get("h", 448), frames=args.frames or cfg.get("frames", 25))
    print("[tm] base job '%s' created (model=%s steps=%d, checkpoint tiers=%s)"
          % (args.job, args.model, args.steps, list(tm.tiers)))
    return _maybe_render(tm, args.job, args)


def cmd_tm_render(args):
    _banner()
    tm = _tm(args)
    if not tm.jobs.exists(args.job):
        print("[tm] no such job '%s'. Known jobs: %s" % (args.job, tm.jobs.list()))
        return 2
    return _maybe_render(tm, args.job, args)


def cmd_branch(args):
    _g = _gate_paid("branch", "TimeMachine branch")
    if _g:
        return _g
    _banner()
    tm = _tm(args)
    try:
        child = tm.branch_from(args.job, args.from_step, new_prompt=args.prompt,
                               new_negative=(args.negative or None), new_cfg=args.cfg,
                               new_style=(args.style or None), seed_policy=args.seed_policy,
                               new_job_id=args.new_job)
    except Exception as e:
        print("[tm] branch failed: %s" % e)
        print("[tm] (branch needs the parent's checkpoint on THIS host — render the base on the GPU "
              "host first, then branch there.)")
        return 2
    print("[tm] branch '%s' <- '%s' @step%d  reuse 0..%d / re-run %d..%d  seed=%d (%s)  style=%r"
          % (child.job_id, args.job, child.from_step, child.from_step, child.from_step + 1,
             child.total_steps, child.seed, args.seed_policy, child.style))
    if args.render:
        return _maybe_render(tm, child.job_id, args)
    print("[tm] to render the tail:  python3 %s render --job %s --root %s"
          % (os.path.join(_HERE, "tm_render.py"), child.job_id, tm.store.root))
    return 0


def cmd_branch_time(args):
    _g = _gate_paid("branch", "TimeMachine branch-at-timestamp")
    if _g:
        return _g
    _banner()
    tm = _tm(args)
    if not tm.jobs.exists(args.job):
        print("[tm] no such job '%s'. Known jobs: %s" % (args.job, tm.jobs.list()))
        return 2
    t = _parse_time(args.from_time)
    if args.render:
        try:
            from tm_temporal import run_temporal_branch, i2v_available
            model = tm.jobs.load(args.job).model
            if i2v_available(model):
                child, res = run_temporal_branch(tm, args.job, t, new_prompt=args.prompt,
                                                 mode=args.mode, tail_seconds=args.tail_seconds,
                                                 crossfade=args.crossfade)
                print("[tm] temporal branch '%s' -> %s  (cut %.2fs, seam ratio %.2f, color_matched=%s)"
                      % (child.job_id, res.out_path, res.cut_time_s, res.seam.get("ratio"),
                         res.color_matched))
                return 0
            print("[tm] I2V pipeline / CUDA unavailable here — creating the temporal branch record only.")
        except Exception as e:
            print("[tm] temporal render deferred (%s)" % e)
    child = tm.branch_time(args.job, t, mode=args.mode, new_prompt=args.prompt)
    print("[tm] temporal branch record '%s' <- '%s' @%.2fs mode=%s (run on a GPU host with I2V)"
          % (child.job_id, args.job, t, args.mode))
    return 0


def cmd_collapse(args):
    _banner()
    video = args.video
    if not video and args.job:
        try:
            video = _tm(args).jobs.load(args.job).out_path
        except Exception as e:
            print("[tm] could not resolve job '%s': %s" % (args.job, e)); return 2
    if not video or not os.path.exists(video):
        print("[tm] no video to analyze — pass --video PATH or --job with a rendered out_path.")
        return 2
    try:
        from tm_collapse import detect_on_path
        rep = detect_on_path(video, prompt=args.prompt, max_frames=args.max_frames, stride=args.stride)
    except Exception as e:
        print("[tm] collapse detection unavailable (%s) — needs numpy + a video decoder "
              "(imageio[pyav] or opencv)." % e)
        return 2
    print(json.dumps(rep.to_dict(), indent=2, default=str) if args.json else rep.pretty())
    return 0


def cmd_timeline(args):
    _banner()
    tm = _tm(args)
    try:
        doc = {"job": args.job, "timeline": tm.timeline(args.job), "lineage": tm.lineage(args.job),
               "storage": tm.storage_report(args.job)}
    except Exception as e:
        print("[tm] no such job '%s' (%s). Known jobs: %s" % (args.job, e, tm.jobs.list()))
        return 2
    print(json.dumps(doc, indent=2, default=str))
    return 0


def cmd_fanout(args):
    _g = _gate_paid("fanout", "TimeMachine A/B fanout")
    if _g:
        return _g
    _banner()
    tm = _tm(args)
    variants = []
    for s in (args.styles or "").split(","):
        if s.strip():
            variants.append({"name": _slugish(s), "style": s.strip()})
    for p in (args.presets or "").split(","):
        if p.strip():
            variants.append({"name": p.strip(), "preset": p.strip()})
    if not variants:
        print("[tm] give at least --styles a,b,c or --presets stabilize,restyle_warm"); return 2
    try:
        kids = tm.fanout(args.job, args.from_step, variants, seed_policy=args.seed_policy)
    except Exception as e:
        print("[tm] fanout failed: %s (render the base on the GPU host first)" % e); return 2
    print("[tm] %d A/B branches at step %d:" % (len(kids), args.from_step))
    for k in kids:
        print("   %-40s style=%r seed=%d" % (k.job_id, k.style, k.seed))
    if args.render or getattr(args, "sim", False):
        for k in kids:
            _maybe_render(tm, k.job_id, args)
    return 0


def _slugish(s):
    return "".join(c if c.isalnum() else "_" for c in s.strip())[:16].strip("_").lower() or "v"


# ---------------------------------------------------------------------------------------------------
#   veizik drama — storyboard -> continuous drama director (TimeMachine continuity + emotion beats +
#   video-driven Korean lip-sync). Thin CLI over veizik_drama.py (the orchestrator).
# ---------------------------------------------------------------------------------------------------
def _drama():
    import veizik_drama as vd
    return vd


def cmd_drama_scaffold(args):
    _banner(); return _drama().do_scaffold(args.out, args.title)


def cmd_drama_plan(args):
    _banner(); return _drama().do_plan(args.storyboard, as_json=args.json, root=args.root)


def cmd_drama_render(args):
    # A real render uses TimeMachine branch/temporal — a paid feature. --sim (the no-GPU proof path)
    # is free, exactly like `veizik base --sim`.
    if not args.sim:
        g = _gate_paid("branch", "Drama director (TimeMachine render)")
        if g:
            return g
    _banner()
    return _drama().do_render(args.storyboard, sim=args.sim, root=args.root,
                              ckpt_every=args.ckpt_every, plan_only=args.plan_only)


def cmd_drama_lipsync(args):
    _banner(); return _drama().do_lipsync(args.storyboard, root=args.root, run=args.run)


def cmd_drama_assemble(args):
    _banner(); return _drama().do_assemble(args.storyboard, root=args.root, stitch=args.stitch)


def _add_drama_subcommands(sub):
    pd = sub.add_parser("drama", help="storyboard -> continuous drama: TimeMachine continuity + "
                        "emotion beats + Korean lip-sync (1–2 min, consistent characters)")
    dsub = pd.add_subparsers(dest="drama_action")

    ps = dsub.add_parser("scaffold", help="write an example storyboard.json to start from")
    ps.add_argument("out", nargs="?", default="storyboard.json")
    ps.add_argument("--title", default="카페의 재회")
    ps.set_defaults(func=cmd_drama_scaffold)

    pp = dsub.add_parser("plan", help="compile a storyboard -> shot/branch plan (no render)")
    pp.add_argument("storyboard")
    pp.add_argument("--json", action="store_true", help="emit the machine plan as JSON")
    pp.add_argument("--root", default=None)
    pp.set_defaults(func=cmd_drama_plan)

    pr = dsub.add_parser("render", help="execute the plan on TimeMachine (visual). --sim = no-GPU proof")
    pr.add_argument("storyboard")
    pr.add_argument("--sim", action="store_true",
                    help="deterministic no-GPU run: real denoise-branch tails + full manifests")
    pr.add_argument("--plan-only", action="store_true", dest="plan_only",
                    help="create records, skip rendering")
    pr.add_argument("--root", default=None)
    pr.add_argument("--ckpt-every", type=int, default=5, dest="ckpt_every")
    pr.set_defaults(func=cmd_drama_render)

    pl = dsub.add_parser("lipsync", help="emit/run the LatentSync (video-driven) manifest for dialogue")
    pl.add_argument("storyboard"); pl.add_argument("--root", default=None)
    pl.add_argument("--run", action="store_true", help="drive LatentSync per entry (on the GPU host)")
    pl.set_defaults(func=cmd_drama_lipsync)

    pa = dsub.add_parser("assemble", help="assembly plan / stitch the final drama in timeline order")
    pa.add_argument("storyboard"); pa.add_argument("--root", default=None)
    pa.add_argument("--stitch", action="store_true", help="stitch now (needs numpy + a video decoder)")
    pa.set_defaults(func=cmd_drama_assemble)

    pd.set_defaults(func=lambda a: (pd.print_help() or 0))


def _add_tm_subcommands(sub):
    """Register the TimeMachine Render subcommands (shared --root / --ckpt-every)."""
    common = dict()

    def _c(p):
        p.add_argument("--root", default=None, help="TimeMachine store root (default ~/.veizik/timemachine)")
        p.add_argument("--ckpt-every", type=int, default=5, help="save a latent checkpoint every N steps")
        return p

    pb = _c(sub.add_parser("base", help="TimeMachine: create + render a base job (captures a checkpoint timeline)"))
    pb.add_argument("--job", required=True); pb.add_argument("--model", default="ltx")
    pb.add_argument("--prompt", required=True); pb.add_argument("--negative", default="")
    pb.add_argument("--cfg", type=float, default=0.0); pb.add_argument("--steps", type=int, default=30)
    pb.add_argument("--w", type=int, default=0); pb.add_argument("--h", type=int, default=0)
    pb.add_argument("--frames", type=int, default=0); pb.add_argument("--seed", type=int, default=42)
    pb.add_argument("--sim", action="store_true", help="deterministic no-GPU sim render (captures the "
                    "checkpoint timeline without weights) — the 'does it work' demo path")
    pb.set_defaults(func=cmd_tm_base)

    pr = _c(sub.add_parser("render", help="TimeMachine: render/resume an existing job by id"))
    pr.add_argument("--job", required=True)
    pr.add_argument("--sim", action="store_true", help="deterministic no-GPU sim render")
    pr.set_defaults(func=cmd_tm_render)

    pbr = _c(sub.add_parser("branch", help="TimeMachine: branch a render at a DENOISE STEP with new "
                            "prompt/style/cfg (prefix reused). == `limml branch`"))
    pbr.add_argument("--job", required=True)
    pbr.add_argument("--from-step", type=int, required=True, dest="from_step")
    pbr.add_argument("--prompt", default=None); pbr.add_argument("--negative", default=None)
    pbr.add_argument("--cfg", type=float, default=None); pbr.add_argument("--style", default=None)
    pbr.add_argument("--seed-policy", default="branch", choices=["keep", "branch", "random"],
                     dest="seed_policy")
    pbr.add_argument("--new-job", default=None, dest="new_job")
    pbr.add_argument("--render", action="store_true", help="render the tail now (needs CUDA)")
    pbr.add_argument("--sim", action="store_true", help="render the tail with the no-GPU sim")
    pbr.set_defaults(func=cmd_branch)

    pt = _c(sub.add_parser("branch-time", help="TimeMachine: branch at a video TIMESTAMP "
                           "(last-good-frame I2V continuation + stitch). == `limml branch-time`"))
    pt.add_argument("--job", required=True)
    pt.add_argument("--from-time", required=True, dest="from_time", help="e.g. 4.2s")
    pt.add_argument("--mode", default="i2v-continuation", choices=["i2v-continuation", "v2v-continuation"])
    pt.add_argument("--prompt", default=None)
    pt.add_argument("--tail-seconds", type=float, default=4.0, dest="tail_seconds")
    pt.add_argument("--crossfade", type=int, default=4)
    pt.add_argument("--render", action="store_true", help="run the I2V continuation + stitch now (needs CUDA)")
    pt.set_defaults(func=cmd_branch_time)

    pc = _c(sub.add_parser("collapse", help="TimeMachine: detect collapse -> suggested branch points"))
    pc.add_argument("--job", default=None); pc.add_argument("--video", default=None)
    pc.add_argument("--prompt", default=None, help="enables CLIP adherence signal if available")
    pc.add_argument("--json", action="store_true"); pc.add_argument("--stride", type=int, default=1)
    pc.add_argument("--max-frames", type=int, default=240, dest="max_frames")
    pc.set_defaults(func=cmd_collapse)

    ptl = _c(sub.add_parser("timeline", help="TimeMachine: show a job's checkpoint timeline + branch lineage"))
    ptl.add_argument("--job", required=True); ptl.set_defaults(func=cmd_timeline)

    pf = _c(sub.add_parser("fanout", help="TimeMachine: A/B fanout — N style variants from one branch point"))
    pf.add_argument("--job", required=True)
    pf.add_argument("--from-step", type=int, required=True, dest="from_step")
    pf.add_argument("--styles", default="", help="comma list, e.g. 'warm cinematic,cyberpunk neon,luxury'")
    pf.add_argument("--presets", default="", help="comma list of preset keys (stabilize,restyle_warm,...)")
    pf.add_argument("--seed-policy", default="branch", choices=["keep", "branch", "random"], dest="seed_policy")
    pf.add_argument("--render", action="store_true", help="render every branch now (needs CUDA)")
    pf.add_argument("--sim", action="store_true", help="render every branch with the no-GPU sim")
    pf.set_defaults(func=cmd_fanout)


# ---------------------------------------------------------------------------------------------------
#   veizik disk  (Asset/Model Storage Doctor, #14) — GPU OOM isn't the only OOM; guard Disk OOM too.
# ---------------------------------------------------------------------------------------------------
def _dir_size(path):
    total = 0
    for root, _d, files in os.walk(path):
        for fn in files:
            try: total += os.path.getsize(os.path.join(root, fn))
            except OSError: pass
    return total


def disk_fit(download_gb, unpack_gb=None, path="~/.cache/huggingface"):
    """Download guard: required = download*1.2 + unpack + 20GB headroom. Returns (ok, required_gb, free_gb)."""
    import shutil
    unpack_gb = download_gb if unpack_gb is None else unpack_gb
    required = download_gb * 1.2 + unpack_gb + 20.0
    try:
        free = shutil.disk_usage(os.path.expanduser(path)).free / 1e9
    except OSError:
        free = 0.0
    return (free >= required, round(required, 1), round(free, 1))


def cmd_disk(args):
    import shutil
    _banner()
    print("\n[disk] Veizik Asset / Model Storage Doctor")
    for mount in ("~/.cache/huggingface", os.path.expanduser("~")):
        try:
            u = shutil.disk_usage(os.path.expanduser(mount))
            print("  filesystem @ %-24s free %.0fGB / total %.0fGB (%.0f%% used)"
                  % (mount, u.free / 1e9, u.total / 1e9, 100.0 * u.used / u.total))
        except OSError:
            pass
    # download guard demo
    if args.check_download:
        ok, req, free = disk_fit(args.check_download, args.unpack)
        verdict = "ALLOW_DOWNLOAD" if ok else "DENY_DOWNLOAD"
        print("\n  [download guard] size %.0fGB -> required %.1fGB (dl*1.2+unpack+20GB), free %.1fGB -> %s"
              % (args.check_download, req, free, verdict))
    # HF cache inventory
    hub = os.path.expanduser("~/.cache/huggingface/hub")
    if os.path.isdir(hub):
        print("\n  [HF cache] %s" % hub)
        entries = []
        for d in sorted(os.listdir(hub)):
            if d.startswith("models--"):
                sz = _dir_size(os.path.join(hub, d)) / 1e9
                entries.append((sz, d.replace("models--", "").replace("--", "/")))
        for sz, name in sorted(entries, reverse=True)[:25]:
            print("    %7.1f GB  %s" % (sz, name))
        print("    (%d model repos, %.0f GB total)" % (len(entries), sum(s for s, _ in entries)))
    # ComfyUI model folder inventory
    cm = os.path.expanduser(os.path.join(getattr(args, "comfy_dir", "~/ComfyUI"), "models"))
    if os.path.isdir(cm):
        print("\n  [ComfyUI models] %s" % cm)
        seen_hash = {}
        for sub in sorted(os.listdir(cm)):
            sd = os.path.join(cm, sub)
            if not os.path.isdir(sd):
                continue
            files = [f for f in os.listdir(sd) if f.endswith((".safetensors", ".ckpt", ".gguf", ".pt", ".bin"))]
            if not files:
                continue
            tot = sum(os.path.getsize(os.path.join(sd, f)) for f in files if os.path.isfile(os.path.join(sd, f)))
            print("    %-22s %3d files  %7.1f GB" % (sub, len(files), tot / 1e9))
            for f in files:  # duplicate detection by (size) fingerprint
                fp = os.path.join(sd, f)
                try: key = os.path.getsize(fp)
                except OSError: continue
                seen_hash.setdefault(key, []).append("%s/%s" % (sub, f))
        dups = {k: v for k, v in seen_hash.items() if len(v) > 1}
        if dups:
            print("\n  [duplicate candidates] same byte-size (verify before deleting):")
            for k, v in list(dups.items())[:10]:
                print("    %.1fGB: %s" % (k / 1e9, ", ".join(v)))
    print("\n  [principle] GPU OOM is guarded by the Oracle; Disk OOM is guarded here "
          "(required = download*1.2 + unpack + 20GB before any pull).")
    return 0


# ---------------------------------------------------------------------------------------------------
#   veizik oracle  (#12) — stats / predict / admit / report --format md
# ---------------------------------------------------------------------------------------------------
def cmd_oracle(args):
    import veizik_oracle as vo
    sub = args.oracle_cmd
    if sub == "stats":
        vo.stats(); return 0
    if sub == "table":
        vo.table(); return 0
    if sub == "report":
        print(vo.report(getattr(args, "format", "md") or "md")); return 0
    if sub == "predict":
        r = vo.predict(args.family, args.w, args.h, args.frames, steps=args.steps)
        print(json.dumps(r, indent=1, ensure_ascii=False)); return 0
    if sub == "admit":
        jobs = []
        for spec in args.jobs:
            f, w, h, fr = spec.split(":")
            jobs.append(dict(family=f, w=int(w), h=int(h), frames=int(fr)))
        print(json.dumps(vo.admit(jobs, args.gpu_total), indent=1, ensure_ascii=False)); return 0
    print("[oracle] unknown subcommand"); return 2


# ---------------------------------------------------------------------------------------------------
#   veizik telemetry  (status | enable | disable | show-last | queue | send | export | delete)
#   The optional performance/compatibility channel ONLY. License operating data is a separate class
#   handled by veizik_entitlement against a separate API, and is not governed by these commands.
# ---------------------------------------------------------------------------------------------------
def cmd_telemetry(args):
    vt = _tel()
    act = getattr(args, "telemetry_action", None) or "status"

    if act == "status":
        st = vt.consent_state()
        on = vt.enabled()
        print("\nPerformance telemetry   %s" % ("ENABLED" if on else "DISABLED"))
        print("Consent version         %s" % ((st or {}).get("consent_version") or "(never asked)"))
        if st and st.get("decided_at"):
            print("Consented at            %s"
                  % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st["decided_at"])))
        else:
            print("Consented at            —")
        print("Installation id         %s   (pseudonymous, not anonymous)" % vt.installation_id())
        print("Pending reports         %d  (%.1f KB / cap %d reports, %d MB)"
              % (vt.queue_count(), vt.queue_bytes() / 1024.0,
                 vt.SPOOL_MAX_ITEMS, vt.SPOOL_MAX_BYTES // (1024 * 1024)))
        print("Endpoint                %s/events   (separate from the license API)" % vt.TELEMETRY_API)
        print("\nCollected")
        for name, detail in vt.COLLECTED:
            print("  [v] %-15s %s" % (name, detail))
        print("\nNever collected")
        for item in vt.NEVER_COLLECTED:
            print("  [x] %s" % item)
        print("\n%s" % vt.retention_note())
        print("Declining telemetry never disables anything on your plan.")
        return 0

    if act == "enable":
        vt.set_consent(True)
        print("[telemetry] performance data ENABLED (consent version %s)." % vt.CONSENT_VERSION)
        print("[telemetry] see exactly what is sent:  veizik telemetry status")
        return 0

    if act == "disable":
        vt.set_consent(False)
        print("[telemetry] performance data DISABLED — nothing further will be transmitted.")
        n = vt.queue_count()
        if n:
            ans = ""
            if getattr(args, "purge", False):
                ans = "y"
            elif getattr(args, "keep", False):
                ans = "n"
            else:
                try:
                    ans = input("[telemetry] delete the %d report(s) still queued locally? [y/N] "
                                % n).strip().lower()
                except EOFError:
                    ans = ""
            if ans in ("y", "yes"):
                print("[telemetry] deleted %d queued report(s)." % vt.clear_queue())
            else:
                print("[telemetry] %d report(s) kept locally and will NOT be sent. Remove them any "
                      "time with `veizik telemetry delete`." % n)
        return 0

    if act == "show-last":
        rep = vt.last_report()
        if not rep:
            print("[telemetry] no run report queued yet.")
            return 0
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return 0

    if act == "queue":
        items = vt._spool_read()
        if not items:
            print("[telemetry] queue is empty.")
            return 0
        print("[telemetry] %d pending report(s), %.1f KB:" % (len(items), vt.queue_bytes() / 1024.0))
        for it in items:
            r = it.get("result") or {}
            wl = it.get("workload") or {}
            print("  %-20s %-10s %sx%s %ssteps  %-8s wall=%ss peak=%sGB"
                  % (it.get("event_id", "")[:20], wl.get("model_public_id", "-"),
                     wl.get("width", "-"), wl.get("height", "-"), wl.get("steps", "-"),
                     r.get("status", "-"), r.get("wall_s", "-"), r.get("peak_vram_gb", "-")))
        return 0

    if act == "send":
        n, msg = vt.send(force=getattr(args, "force", False))
        print("[telemetry] %s" % msg)
        return 0

    if act == "export":
        out = getattr(args, "out", None) or "veizik_telemetry_export.json"
        n = vt.export(out)
        print("[telemetry] exported %d pending report(s) -> %s" % (n, os.path.abspath(out)))
        print("[telemetry] this is byte-for-byte what would be uploaded.")
        return 0

    if act == "delete":
        n, msg = vt.delete_request()
        print("[telemetry] removed %d locally queued report(s)." % n)
        print("[telemetry] %s" % msg)
        return 0

    print("[telemetry] unknown action: %s" % act)
    return 2


# ---------------------------------------------------------------------------------------------------
#   veizik plans — the billing unit is CONCURRENT RUN NODES, never machines-you-own.
#   Registered devices are a convenience limit; what you buy is how many renders run at once.
# ---------------------------------------------------------------------------------------------------
_PLANS = [
    {"id": "starter", "name": "Starter Preview", "price": "free, 7 days",
     "registered": 1, "concurrent": 1,
     "includes": ["doctor", "limited sample + capsule runs", "experimental universal path"],
     "excludes": ["commercial use", "watermark removal", "stable profiles"],
     "note": "the 7 days start at your FIRST SUCCESSFUL RENDER, not at activation — "
             "install trouble never burns trial time"},
    {"id": "creator", "name": "Founding Creator", "price": "$9 / month  ·  $79 / year",
     "registered": 2, "concurrent": 1,
     "includes": ["commercial output", "watermark removal", "stable profiles",
                  "Creator Adapters + Capsules", "automatic updates",
                  "free until general availability, then 12 months of Founder pricing"],
     "excludes": ["advanced FP8/INT8", "Queue/batch", "API"],
     "note": ""},
    {"id": "pro", "name": "Founding Pro Runtime Pass", "price": "$249-299 first year",
     "registered": 3, "concurrent": 2,
     "includes": ["everything in Creator", "advanced FP8/INT8 quantization", "Queue / batch",
                  "advanced GPU Oracle", "priority access to Recover / TimeMachine / API",
                  "Pro Preview for 24 months", "private release channel"],
     "excludes": ["organisation-wide deployment", "central license server"],
     "note": ""},
    {"id": "studio", "name": "Studio Node", "price": "contact us",
     "registered": "per contract", "concurrent": "per contract",
     "includes": ["seats + concurrent nodes", "dedicated Adapters", "internal distribution",
                  "central license server", "logs / audit / API"],
     "excludes": [],
     "note": "organisation agreement"},
]


def cmd_plans(args):
    _banner()
    print("\nveizik plans — you license CONCURRENT RUN NODES (how many renders execute at once).")
    print("Registered devices are just a convenience cap; moving your work between your own")
    print("machines is expected and free.\n")
    hdr = ("plan", "price", "registered devices", "concurrent nodes")
    rows = [(p["name"], p["price"], str(p["registered"]), str(p["concurrent"])) for p in _PLANS]
    widths = [max(len(str(r[i])) for r in ([hdr] + rows)) for i in range(len(hdr))]
    def fmt(r):
        return "  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r))
    print(fmt(hdr))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))

    for p in _PLANS:
        print("\n%s — %s" % (p["name"], p["price"]))
        print("  registered devices %s · concurrent nodes %s" % (p["registered"], p["concurrent"]))
        for inc in p["includes"]:
            print("    +  %s" % inc)
        for exc in p["excludes"]:
            print("    -  %s" % exc)
        if p["note"]:
            print("    note: %s" % p["note"])

    try:
        ve = _ent()
        print("\n[current] %s" % ve.status_line(ve.resolve()))
    except Exception:
        pass
    print("\nSeat terms today: 1 seat, 2 registered devices, 1 concurrent node, 3 device changes")
    print("per year. Payment runs through Polar (merchant of record).")
    print("Feature status:  veizik feature <name>      Pricing page:  https://veizik.com/#pricing")
    return 0


# ---------------------------------------------------------------------------------------------------
#   veizik feature <name> — honest lifecycle status. Nothing here may claim to be Shipped when the
#   public download does not run it. Stages: Research | Development | Private Preview |
#   Public Preview | Shipped.
# ---------------------------------------------------------------------------------------------------
_STAGES = ("Research", "Development", "Private Preview", "Public Preview", "Shipped")

# name -> (stage, plans-that-include-it, one-line description, honest caveat)
_FEATURES = {
    "doctor": ("Shipped", ["starter", "creator", "pro", "studio"],
               "hardware probe + per-family support tier table",
               "confirmed working in the public download"),
    "license": ("Shipped", ["starter", "creator", "pro", "studio"],
                "activate / status / logout, free entitlement",
                "confirmed working in the public download"),
    "telemetry": ("Shipped", ["starter", "creator", "pro", "studio"],
                  "opt-in performance + compatibility reporting",
                  "consent is separate from the license data class; declining locks nothing"),
    "t2v": ("Public Preview", ["creator", "pro", "studio"],
            "direct text-to-video through the universal path",
            "experimental: Linux + NVIDIA only, and you supply your own torch/diffusers"),
    "t2i": ("Public Preview", ["creator", "pro", "studio"],
            "direct text-to-image through the universal path",
            "experimental: Linux + NVIDIA only, and you supply your own torch/diffusers"),
    "oracle": ("Public Preview", ["creator", "pro", "studio"],
               "peak-VRAM / render-time prediction and admission control",
               "basic prediction runs locally; the advanced planner is Pro and still in Development"),
    "quantization": ("Private Preview", ["pro", "studio"],
                     "advanced FP8 / INT8 native quantization",
                     "the INT8 engine is measured internally only; it is NOT in the public binary"),
    "timemachine": ("Private Preview", ["pro", "studio"],
                    "checkpoint-native resume, branch and A/B fanout",
                    "no public TimeMachine build has been released yet"),
    "comfyui": ("Development", ["creator", "pro", "studio"],
                "ComfyUI drop-in: `veizik run` and `veizik serve`",
                "not shipped in the public download"),
    "recover": ("Development", ["pro", "studio"],
                "resume a failed or OOM-interrupted render from its last checkpoint",
                "not shipped; Pro gets priority access when it lands"),
    "queue": ("Development", ["pro", "studio"],
              "persistent job queue across your concurrent nodes",
              "not shipped; Pro gets priority access when it lands"),
    "batch": ("Development", ["pro", "studio"],
              "multi-job batch submission with shared model residency",
              "not shipped; Pro gets priority access when it lands"),
    "adapter": ("Development", ["creator", "pro", "studio"],
                "private Model Adapters (a licensed model gains a tuned execution path)",
                "the public Adapter interface is defined; the private packs are not released"),
    "capsule": ("Development", ["creator", "pro", "studio"],
                "Capsules: a reproducible, signed run configuration",
                "not shipped in the public download"),
    "api": ("Research", ["pro", "studio"],
            "local HTTP API bridge for programmatic submission",
            "design stage; no implementation is published"),
}

# Which plan a feature first becomes available on (display order matches _PLANS).
_PLAN_NAME = {p["id"]: p["name"] for p in _PLANS}


def _measurement_note():
    return ("Render-time figures are measurement in progress. The one measured number we publish "
            "is LTX-13B peak VRAM 9.55 GB (internal) plus block-level rel_L2 accuracy.")


def cmd_feature(args):
    vt_name = (args.name or "").strip().lower()
    if not vt_name or vt_name in ("list", "all"):
        print("\nFeature status — stages: %s\n" % " < ".join(_STAGES))
        hdr = ("feature", "stage", "available on")
        rows = [(k, v[0], ", ".join(_PLAN_NAME.get(p, p) for p in v[1]))
                for k, v in sorted(_FEATURES.items(), key=lambda kv: (_STAGES.index(kv[1][0]), kv[0]))]
        widths = [max(len(str(r[i])) for r in ([hdr] + rows)) for i in range(len(hdr))]
        def fmt(r):
            return "  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r))
        print(fmt(hdr))
        print("  " + "  ".join("-" * w for w in widths))
        for r in rows:
            print(fmt(r))
        print("\n%s" % _measurement_note())
        print("Detail:  veizik feature <name>")
        return 0

    hit = _FEATURES.get(vt_name)
    if not hit:
        print("[feature] unknown feature %r. Known: %s" % (vt_name, ", ".join(sorted(_FEATURES))))
        return 2
    stage, plans, desc, caveat = hit
    print("\n%s" % vt_name)
    print("  %s" % desc)
    print("  Stage           %s   (%s)" % (stage, " < ".join(_STAGES)))
    print("  Available on    %s" % ", ".join(_PLAN_NAME.get(p, p) for p in plans))
    print("  Reality check   %s" % caveat)

    # is it in the CURRENT plan?
    try:
        ve = _ent()
        ent = ve.resolve()
        cur = ent.tier
        cur_ids = {"free": "starter", "personal": "starter", "creator": "creator",
                   "pro": "pro", "studio": "studio"}.get(cur, "starter")
        included = cur_ids in plans
        print("  Your plan       %s -> %s" % (ent.label(), "INCLUDED" if included else "NOT INCLUDED"))
        if not included:
            need = plans[0] if plans else "pro"
            print("  Upgrade         %s covers it — https://veizik.com/#pricing"
                  % _PLAN_NAME.get(need, need))
    except Exception:
        print("  Your plan       (no license session; run `veizik status`)")
    if stage != "Shipped":
        print("  NOTE            this is %s — it does not run in the public download today."
              % stage)
    print("\n%s" % _measurement_note())
    return 0


# ---------------------------------------------------------------------------------------------------
#   veizik upgrade <creator|pro> — print the URL. We never open a browser for you.
# ---------------------------------------------------------------------------------------------------
def cmd_upgrade(args):
    target = (args.plan or "").strip().lower()
    plan = next((p for p in _PLANS if p["id"] == target), None)
    if plan is None:
        print("[upgrade] unknown plan %r — choose: creator | pro" % target)
        return 2
    print("\n%s — %s" % (plan["name"], plan["price"]))
    print("  registered devices %s · concurrent nodes %s" % (plan["registered"], plan["concurrent"]))
    for inc in plan["includes"]:
        print("    +  %s" % inc)
    print("\nOpen this page in your browser:")
    print("    https://veizik.com/#pricing")
    print("\nThen activate on this machine:")
    print("    veizik activate <YOUR_KEY>")
    print("\n(Payment is handled by Polar as merchant of record. veizik never opens a browser for")
    print("you and never takes card details in the terminal.)")
    return 0


# ---------------------------------------------------------------------------------------------------
#   argparse
# ---------------------------------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        prog="veizik",
        description=BANNER + "  |  ComfyUI drop-in: alias comfy=veizik. 'vz' is the short alias.")
    ap.add_argument("--target", default="commercial_10s",
                    help="autotune target profile (default commercial_10s)")
    sub = ap.add_subparsers(dest="cmd")

    p_doc = sub.add_parser("doctor", help="probe hardware + list every model family's support tier")
    p_doc.set_defaults(func=cmd_doctor)

    # license: activate / inspect / clear an API key (unlocks your tier; free without one)
    p_login = sub.add_parser("login", help="activate a veizik API key (unlocks your paid tier)")
    p_login.add_argument("api_key", help="your veizik API key (vzk_live_...) from veizik.com")
    p_login.set_defaults(func=cmd_login)
    p_status = sub.add_parser("status", help="show current license tier + entitlement")
    p_status.set_defaults(func=cmd_status)
    p_logout = sub.add_parser("logout", help="remove the stored license session (revert to Free)")
    p_logout.set_defaults(func=cmd_logout)
    # `activate` is the word on the receipt; identical behaviour to `login`.
    p_act = sub.add_parser("activate", help="alias of `login`: activate a veizik key on this machine")
    p_act.add_argument("api_key", help="your veizik API key (vzk_live_...) from veizik.com")
    p_act.set_defaults(func=cmd_activate)

    # plans / feature status / upgrade (commercial surface)
    p_plans = sub.add_parser("plans", help="plans and what each includes (billed by concurrent run nodes)")
    p_plans.set_defaults(func=cmd_plans)

    p_feat = sub.add_parser("feature", help="honest status of a feature "
                            "(Research|Development|Private Preview|Public Preview|Shipped)")
    p_feat.add_argument("name", nargs="?", default="list",
                        help="timemachine|recover|queue|batch|api|comfyui|... ('list' for all)")
    p_feat.set_defaults(func=cmd_feature)

    p_up = sub.add_parser("upgrade", help="how to move to a paid plan (prints the URL, opens nothing)")
    p_up.add_argument("plan", choices=["creator", "pro"], help="target plan")
    p_up.set_defaults(func=cmd_upgrade)

    # telemetry — the OPTIONAL performance/compatibility channel only
    p_tel = sub.add_parser("telemetry", help="optional performance data: status/enable/disable/"
                           "show-last/queue/send/export/delete")
    tsub = p_tel.add_subparsers(dest="telemetry_action")
    tsub.add_parser("status", help="consent version/time, pending count, exactly what is and is not collected")
    tsub.add_parser("enable", help="turn optional performance reporting ON")
    t_dis = tsub.add_parser("disable", help="stop all future transmission (asks about the local queue)")
    t_dis.add_argument("--purge", action="store_true", help="also delete the local queue, no prompt")
    t_dis.add_argument("--keep", action="store_true", help="keep the local queue, no prompt")
    tsub.add_parser("show-last", help="print the most recent run report verbatim")
    tsub.add_parser("queue", help="list the reports waiting to be sent")
    t_send = tsub.add_parser("send", help="upload the queued batch now")
    t_send.add_argument("--force", action="store_true", help="ignore the 24h batching window")
    t_exp = tsub.add_parser("export", help="write the exact bytes that would be uploaded to a file")
    t_exp.add_argument("--out", default="veizik_telemetry_export.json")
    tsub.add_parser("delete", help="erase local reports and request server-side erasure")
    p_tel.set_defaults(func=cmd_telemetry, telemetry_action=None)

    p_run = sub.add_parser("run", help="DROP-IN for `comfy run`: render a ComfyUI workflow JSON")
    p_run.add_argument("workflow", help="path to a ComfyUI workflow .json (API or graph format)")
    p_run.add_argument("--dry-run", action="store_true", help="parse + plan only, no render")
    p_run.add_argument("--no-passthrough", action="store_true",
                       help="disable ComfyUI headless passthrough for opaque workflows")
    p_run.add_argument("--comfy-dir", default="~/ComfyUI", help="ComfyUI install dir (for passthrough)")
    p_run.set_defaults(func=cmd_run)

    p_srv = sub.add_parser("serve", help="DROP-IN for `comfy launch`: run ComfyUI + LimML injection")
    p_srv.add_argument("--port", type=int, default=8188, help="port (default 8188, ComfyUI default)")
    p_srv.add_argument("--listen", action="store_true", help="bind 0.0.0.0 (LAN access)")
    p_srv.add_argument("--comfy-dir", default="~/ComfyUI", help="ComfyUI install dir")
    p_srv.add_argument("--dry-run", action="store_true", help="print launch cmd, don't launch")
    p_srv.add_argument("extra", nargs=argparse.REMAINDER, help="extra args passed to ComfyUI main.py")
    p_srv.set_defaults(func=cmd_serve)

    for name, vid in (("t2v", True), ("t2i", False)):
        p = sub.add_parser(name, help="direct native %s render (fast path)"
                           % ("text->video" if vid else "text->image"))
        p.add_argument("prompt", help="the text prompt")
        p.add_argument("--model", default="", help="family (ltx|wan22_moe|stepvideo|hunyuanvideo|"
                       "cogvideox|flux|sd35) or a checkpoint name; unknown -> universal fallback")
        p.add_argument("--negative", default="", help="negative prompt")
        p.add_argument("--w", type=int, default=0, help="width")
        p.add_argument("--h", type=int, default=0, help="height")
        p.add_argument("--frames", type=int, default=0, help="frames (video only)")
        p.add_argument("--steps", type=int, default=0, help="denoise steps")
        p.add_argument("--seed", type=int, default=None, help="seed")
        p.add_argument("--out", default="", help="output path (default ./veizik_out.mp4/.png)")
        p.add_argument("--mode", choices=["quality", "balanced", "low-memory"], default=None,
                       help="memory/speed intent. omit = AUTO (best fidelity that fits your GPU): "
                            "small models render lossless, large models use fp8 (half-VRAM + faster).")
        p.add_argument("--precision", choices=["auto", "bf16", "fp8", "int8", "sequential"],
                       default="auto", help="expert override of AUTO (see 'veizik t2v -h').")
        p.add_argument("--dry-run", action="store_true", help="plan only, no render")
        p.set_defaults(func=cmd_t2v if vid else cmd_t2i)

    # disk — Asset/Model Storage Doctor (#14)
    p_disk = sub.add_parser("disk", help="disk/model storage doctor: cache inventory, dedup, download guard")
    p_disk.add_argument("--check-download", type=float, default=0.0, dest="check_download",
                        metavar="GB", help="test the download guard for a model of this GB size")
    p_disk.add_argument("--unpack", type=float, default=None, help="unpack size GB (default = download size)")
    p_disk.add_argument("--comfy-dir", default="~/ComfyUI")
    p_disk.set_defaults(func=cmd_disk)

    # oracle — GPU Oracle P0 reporting/prediction/admission (#12)
    p_or = sub.add_parser("oracle", help="GPU Oracle: predict peak/time, admission, calibration report")
    osub = p_or.add_subparsers(dest="oracle_cmd")
    osub.add_parser("stats", help="p50/p95 prediction error by family×GPU")
    osub.add_parser("table", help="raw profile table")
    orp = osub.add_parser("report", help="accuracy report (basis MAE/MAPE, OOM false-neg, coeffs, outliers)")
    orp.add_argument("--format", default="md", choices=["md", "txt"])
    opd = osub.add_parser("predict", help="predict peak/time for a config")
    opd.add_argument("family"); opd.add_argument("w", type=int); opd.add_argument("h", type=int)
    opd.add_argument("frames", type=int, nargs="?", default=1); opd.add_argument("--steps", type=int, default=30)
    oad = osub.add_parser("admit", help="farm admission for N candidate jobs")
    oad.add_argument("gpu_total", type=float); oad.add_argument("jobs", nargs="+", metavar="family:w:h:frames")
    p_or.set_defaults(func=cmd_oracle)

    # TimeMachine Render — checkpoint-native resume/branch (base/render/branch/branch-time/collapse/
    # timeline/fanout). `veizik branch ...` == `limml branch ...`.
    _add_tm_subcommands(sub)

    # Drama director — storyboard -> continuous drama built ON TimeMachine (continuity + emotion
    # beats) + video-driven Korean lip-sync.
    _add_drama_subcommands(sub)

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        _banner()
        ap.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        # Absolute drop-in safety: an unexpected error in veizik must not read as "the swap failed".
        print("\n[veizik] internal error: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        print("[veizik] this is a veizik bug, not a rejection of your workflow. Report it; your "
              "ComfyUI usage is unaffected.", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
