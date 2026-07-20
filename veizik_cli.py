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


def _packs():
    import veizik_packs as vp
    return vp


# ---------------------------------------------------------------------------------------------------
#   shared local state + version + honest hardware probe
#
#   These back `setup` / `benchmark` / `deactivate` / `update`. Two rules govern everything below:
#     1. Never invent a number. limml_universal.probe_hardware() FALLS BACK to a synthetic
#        "RTX 3090 / 24GB" HwProfile when torch is absent — perfectly fine for planning, fatal for a
#        benchmark report. So benchmark uses its own probe which records WHERE each value came from
#        and leaves anything unmeasured explicitly unmeasured.
#     2. Never claim a render time. The native engine is not in the public download, so every
#        render-time cell is "measurement in progress" (see _measurement_note()).
# ---------------------------------------------------------------------------------------------------
_UNMEASURED = "measurement in progress"

# Kept in sync with web/install.sh (VZ_REPO / VZ_HOME); overridable for private mirrors.
_INSTALL_URL = os.environ.get("VEIZIK_INSTALL_URL", "https://veizik.com/install.sh")
_VZ_REPO = os.environ.get("VZ_REPO", "https://github.com/veizikhq/veizik.git")
_VZ_HOME = os.path.expanduser(os.environ.get("VZ_HOME", "~/.veizik/app"))


def _app_version():
    """(version, source). 'unknown' is a legitimate answer — better than a made-up number."""
    env = os.environ.get("VEIZIK_VERSION")
    if env:
        return env.strip(), "VEIZIK_VERSION"
    for cand in (os.path.join(_HERE, "VERSION"),
                 os.path.abspath(os.path.join(_HERE, "..", "VERSION")),
                 os.path.abspath(os.path.join(_HERE, "..", "..", "VERSION")),
                 os.path.join(_VZ_HOME, "VERSION")):
        try:
            with open(cand) as f:
                v = f.read().strip()
            if v:
                return v, cand
        except OSError:
            continue
    # a git checkout of the distribution repo can describe itself
    if os.path.isdir(os.path.join(_VZ_HOME, ".git")):
        try:
            r = subprocess.run(["git", "-C", _VZ_HOME, "describe", "--tags", "--always"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip(), "git describe (%s)" % _VZ_HOME
        except Exception:
            pass
    return "unknown", "no VERSION file found"


def _run_out(cmd, timeout=10):
    """Best-effort stdout of a helper binary. Never raises; missing tool -> None."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


def _probe_hw_honest():
    """Hardware facts with provenance. Every field carries how it was obtained, and anything we
    could not read stays None instead of being defaulted. Pure stdlib except an optional torch peek."""
    import platform
    hw = {"os": "%s %s" % (platform.system(), platform.release()),
          "os_major": "%s-%s" % (platform.system().lower(), (platform.release() or "?").split(".")[0]),
          "arch": platform.machine(), "python": platform.python_version(),
          "cpu_count": os.cpu_count(), "sources": {}}
    hw["sources"]["os"] = "platform"

    try:
        hw["ram_gb"] = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9, 1)
        hw["sources"]["ram_gb"] = "sysconf"
    except Exception:
        hw["ram_gb"] = None

    # --- NVIDIA: nvidia-smi is authoritative and needs no python packages -------------------------
    out = _run_out(["nvidia-smi",
                    "--query-gpu=name,memory.total,memory.free,driver_version,compute_cap",
                    "--format=csv,noheader,nounits"])
    gpus = []
    if out:
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue
            def _f(x):
                try: return float(x)
                except ValueError: return None
            tot, free = _f(parts[1]), _f(parts[2])
            gpus.append({"name": parts[0],
                         "vram_total_gb": round(tot / 1024.0, 2) if tot is not None else None,
                         "vram_free_gb": round(free / 1024.0, 2) if free is not None else None,
                         "driver": parts[3],
                         "compute_cap": parts[4] if len(parts) > 4 else None})
    if gpus:
        hw["gpu_vendor"] = "nvidia"
        hw["gpus"] = gpus
        hw["sources"]["gpu"] = "nvidia-smi"
        cu = _run_out(["nvidia-smi", "--query", "--display=COMPUTE"]) or ""
        for ln in cu.splitlines():
            if "CUDA Version" in ln:
                hw["cuda_version"] = ln.split(":", 1)[1].strip()
                hw["sources"]["cuda_version"] = "nvidia-smi"
                break

    # --- torch peek: confirms the runtime can actually SEE the GPU (a driver is not enough) -------
    try:
        import torch
        hw["torch"] = torch.__version__
        hw["sources"]["torch"] = "import torch"
        if torch.cuda.is_available():
            hw["accel"] = "cuda"
            hw["cuda_version"] = hw.get("cuda_version") or (torch.version.cuda or None)
            hw["gpu_count"] = torch.cuda.device_count()
            if not gpus:                              # driver hidden (WSL/container) but torch sees it
                props = torch.cuda.get_device_properties(0)
                hw["gpu_vendor"] = "nvidia"
                hw["gpus"] = [{"name": props.name,
                               "vram_total_gb": round(props.total_memory / 1e9, 2),
                               "vram_free_gb": None, "driver": None,
                               "compute_cap": "%d.%d" % (props.major, props.minor)}]
                hw["sources"]["gpu"] = "torch.cuda"
        elif getattr(getattr(torch, "backends", None), "mps", None) is not None \
                and torch.backends.mps.is_available():
            hw["accel"] = "mps"                        # Apple Silicon: not a supported render target
            hw["gpu_vendor"] = "apple"
        else:
            hw["accel"] = "cpu"
    except Exception as e:
        hw["torch"] = None
        hw["torch_error"] = "%s: %s" % (type(e).__name__, e)
        hw["accel"] = "cuda" if gpus else "none"       # a driver exists, but nothing can drive it here

    hw["gpu_count"] = hw.get("gpu_count") or len(hw.get("gpus") or [])
    return hw


def _tier_verdict(hw):
    """What this machine can run TODAY, in the public download. Honest about the gap between
    'the engine supports it' and 'the engine is downloadable'."""
    gpus = hw.get("gpus") or []
    if hw.get("gpu_vendor") == "nvidia" and gpus:
        cc = (gpus[0].get("compute_cap") or "0").split(".")
        try:
            sm = int(cc[0]) * 10 + int(cc[1])
        except (ValueError, IndexError):
            sm = 0
        if hw.get("accel") != "cuda":
            return ("T3", "an NVIDIA driver is present but no CUDA-capable torch is installed here — "
                          "install torch to reach the universal path")
        levers = []
        if sm >= 89:
            levers.append("fp8 tensor cores")
        if sm >= 75:
            levers.append("int8 tensor cores")
        return ("T2", "universal path available (torch %s, CUDA %s)%s. T1 native-CUDA needs the "
                      "native engine pack, which is NOT published yet."
                % (hw.get("torch"), hw.get("cuda_version") or "?",
                   "; hardware also has " + " + ".join(levers) if levers else ""))
    if hw.get("accel") == "mps":
        return ("T3", "Apple Silicon (MPS). The experimental universal t2v/t2i path is Linux + NVIDIA "
                      "only; doctor/license/telemetry/pack all work here.")
    return ("T3", "no CUDA GPU detected — doctor/license/telemetry/pack work, rendering does not.")


def _gate_paid(feature, label):
    """Gate a paid-only feature (TimeMachine branch/fanout). Returns None if allowed, else prints an
    upgrade notice and returns an exit code. Free/entry tiers don't get these."""
    ve = _ent()
    ent = ve.resolve()
    if ent.allows(feature):
        return None
    print("\n[license] %s requires a paid plan (Creator+). Current: %s." % (label, ent.label()))
    print("[license] upgrade at https://veizik.com  —  then:  veizik login <api_key>")
    # §14: a refused TimeMachine attempt is exactly the moment the Pro Preview path is worth showing
    # (once a day, and never claiming the command runs in this build).
    _usage_note(event="pro_attempt", feature="timemachine")
    return 3  # distinct 'not entitled' code (non-crashing)


# ---------------------------------------------------------------------------------------------------
#   §14  usage-behaviour upsell — one hint, only at the moment it is useful.
#
#   This is deliberately NOT advertising: every line is triggered by something the user just did, so
#   it reads as the next step rather than a pitch. The rules that keep it from becoming spam:
#     - at most ONE hint per command invocation (never a stack of suggestions)
#     - the same hint is printed at most once per calendar day
#     - VEIZIK_NO_HINTS=1 silences all of them (scripts, CI, screen recordings)
#     - nothing here may claim an unshipped feature works. Every Pro-Preview line prints the real
#       lifecycle stage from _FEATURES and says plainly that the command is not in this download.
#
#   State: ~/.veizik/usage.json. Local only — this file is never uploaded by this module, and it is
#   separate from veizik_telemetry's telemetry_state.json (that one is the CONSENTED channel and only
#   moves under the allowlist). Keeping them apart means the funnel keeps working for a user who
#   declined telemetry, which §5 requires.
# ---------------------------------------------------------------------------------------------------
_VEIZIK_HOME = os.path.expanduser(os.environ.get("VEIZIK_HOME", "~/.veizik"))
_USAGE_PATH = os.path.join(_VEIZIK_HOME, "usage.json")


def _iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _usage_load():
    """Read usage.json. Any corruption reads as 'no history' — a bad state file must never be able to
    break a command, at worst it re-shows a hint."""
    try:
        with open(_USAGE_PATH) as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def _usage_save(st):
    """Atomic-ish write, 0700 dir (it records what this machine did). Never raises."""
    try:
        os.makedirs(_VEIZIK_HOME, exist_ok=True)
        try:
            os.chmod(_VEIZIK_HOME, 0o700)
        except OSError:
            pass
        tmp = _USAGE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=2, sort_keys=True)
        os.replace(tmp, _USAGE_PATH)
    except Exception:
        pass  # bookkeeping is never allowed to fail a render
    return st


def _mark_activated():
    """`veizik activate/login` succeeded on this machine."""
    st = _usage_load()
    st.setdefault("activated_at", _iso_now())
    st["last_activate_at"] = _iso_now()
    return _usage_save(st)


def _mark_doctor_ok():
    """`veizik doctor` ran to completion (it probed the hardware and printed the tier table)."""
    st = _usage_load()
    st.setdefault("doctor_ok_at", _iso_now())
    st["doctor_runs"] = int(st.get("doctor_runs") or 0) + 1
    return _usage_save(st)


def _mark_render_success():
    """Count a render that actually produced a file. Returns the new cumulative count.

    The first one also pings the server (see _report_first_success): §2 puts the start of the 7-day
    Starter clock at the FIRST SUCCESSFUL RENDER, not at activation, so install/driver trouble never
    burns trial time — but only the server can hold that clock."""
    st = _usage_load()
    n = int(st.get("render_successes") or 0) + 1
    st["render_successes"] = n
    if not st.get("first_success_at"):
        st["first_success_at"] = _iso_now()
    st["last_success_at"] = _iso_now()
    _usage_save(st)
    if not st.get("first_success_reported"):
        _report_first_success(st)
    return n


# The endpoint may legitimately not exist yet on a given deployment; cap the retries so an
# unreachable/404 server cannot add a network round-trip to every render forever.
_FIRST_SUCCESS_MAX_TRIES = 5


def _report_first_success(st):
    """POST /api/trial/first-success — best effort, and silent by design.

    Offline laptop, captive portal, blocked egress, 404, a server that has not shipped this route:
    all of them are no-ops here. It is retried on later successful renders until acknowledged (capped)
    so a machine that rendered while offline still gets its trial start recorded.

    Body carries the pseudonymous installation id and the timestamp only — no prompt, no input file,
    no output path, no hostname. That matches what §5 step 1 says license operation transmits."""
    tries = int(st.get("first_success_tries") or 0)
    if tries >= _FIRST_SUCCESS_MAX_TRIES:
        return
    body = {"occurred_at": st.get("first_success_at"), "event": "first_render_success"}
    base = os.environ.get("VEIZIK_API_BASE", "https://veizik.com").rstrip("/")
    try:
        vt = _tel()
        body["installation_id"] = vt.installation_id()   # same pseudonymous id the license flow uses
    except Exception:
        pass
    try:
        ve = _ent()
        base = ve.API_BASE
        # session accessor is module-private in some builds; absence just means "not logged in"
        _ls = getattr(ve, "load_session", None) or getattr(ve, "_load_session", None)
        sess = _ls() if _ls else None
        if sess and sess.get("api_key"):
            body["api_key"] = sess["api_key"]
    except Exception:
        pass

    st["first_success_tries"] = tries + 1
    _usage_save(st)
    try:
        import urllib.request as _u
        req = _u.Request(base + "/api/trial/first-success", data=json.dumps(body).encode(),
                         headers={"Content-Type": "application/json",
                                  "User-Agent": "veizik-cli/1.0 (+https://veizik.com)"},
                         method="POST")
        with _u.urlopen(req, timeout=6) as r:
            ok = 200 <= int(getattr(r, "status", None) or r.getcode()) < 300
            r.read()
    except Exception:
        return  # ignored, exactly as specified
    if ok:
        st["first_success_reported"] = True
        _usage_save(st)


def _pick_hint(st, event, feature):
    """Choose the single most relevant hint, or None. Order is 'what is the user blocked on right
    now', most specific first — an explicit Pro-Preview attempt beats any funnel-stage nudge."""
    successes = int(st.get("render_successes") or 0)

    # (a) they just typed a command that only exists in the Pro Preview track.
    if event == "pro_attempt" and feature:
        stage, plans, _desc, caveat = _FEATURES.get(
            feature, ("Development", ["pro"], feature, "not shipped in the public download"))
        return ("pro_preview:" + feature, [
            "  `veizik %s` is available in Pro Preview — Founding Pro Runtime Pass" % feature,
            "  ($249-299 first year: 3 registered devices, 2 concurrent nodes, advanced FP8/INT8,",
            "  Queue/batch, advanced GPU Oracle, Pro Preview channel for 24 months).",
            "  Status: %s — %s." % (stage, caveat),
            "  Upgrading does NOT make this command run today; Pro simply gets it first when it",
            "  ships.   Detail:  veizik feature %s      https://veizik.com/#pricing" % feature,
        ])

    # (b) activated but never ran doctor -> the install is not actually finished.
    if st.get("activated_at") and not st.get("doctor_ok_at"):
        return ("next_doctor", [
            "  Next step — check what this machine can run (GPU, VRAM, per-family support tier):",
            "      veizik doctor",
        ])

    # (c) doctor passed, nothing rendered yet -> hand them the exact command, honestly labelled.
    if st.get("doctor_ok_at") and successes == 0:
        return ("next_sample", [
            "  Nothing has been rendered on this machine yet. Try a sample:",
            '      veizik t2i "a red apple on a worn oak table, morning light" --steps 20',
            "  (universal path, experimental: Linux + NVIDIA, using your own torch / diffusers)",
        ])

    # (d) the first successful render — the one moment the offer is genuinely earned.
    if successes == 1:
        return ("founder_offer", [
            "  This machine completed its first Veizik render.",
            "  Founding Creator — $9 monthly / $79 annual: commercial output, watermark removal,",
            "  stable profiles, Creator Adapters + Capsules, automatic updates.",
            "      https://veizik.com/#pricing     then:  veizik activate <YOUR_KEY>",
        ])

    # (e) a repeat user — annual is simply the cheaper shape, and Pro interest is worth registering.
    if successes >= 3:
        return ("repeat_user", [
            "  %d successful renders on this machine." % successes,
            "  $79 annual costs less than 9 months at $9 — https://veizik.com/#pricing",
            "  Want Queue/batch, advanced FP8/INT8, Recover or TimeMachine? Register interest with",
            "      veizik upgrade pro",
            "  (those are Development / Private Preview — not shipped in the public download yet).",
        ])
    return None


def _usage_note(event=None, feature=None):
    """Print AT MOST ONE situational hint for the command that just ran (§14).

    Call it at the END of a successful command path only — a hint after a failure reads as noise.
    Same-hint-once-per-day is enforced here, so callers never have to think about repetition."""
    if os.environ.get("VEIZIK_NO_HINTS") == "1":
        return 0
    try:
        st = _usage_load()
        hit = _pick_hint(st, event, feature)
        if not hit:
            return 0
        hint_id, lines = hit
        today = time.strftime("%Y-%m-%d", time.localtime())
        shown = dict(st.get("hints_shown") or {})
        if shown.get(hint_id) == today:
            return 0            # already said this today; say nothing rather than repeat
        shown[hint_id] = today
        st["hints_shown"] = shown
        _usage_save(st)
        print("")
        for ln in lines:
            print(ln)
    except Exception:
        pass  # a hint is never worth an error
    return 0


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
    # §14: an activated machine that has never run `doctor` is a half-finished install.
    _mark_activated()
    _usage_note()
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
_RULE = "-" * 78


def _license_gate_is_telemetry_free():
    """Runtime PROOF of the promise the consent screen makes: declining optional telemetry
    cannot lock a purchased feature.

    We do not merely assert this in prose. Every feature gate in this CLI (`_gate_paid`,
    `ent.allows`, `ent.clamp`, `ent.stamp`) reads the signed entitlement payload and nothing
    else, so the check is mechanical: the license module must never reference the telemetry
    module. If a future edit wires consent into the gate, this flips to False and the CLI stops
    printing the guarantee instead of printing a lie.

    Returns (ok, detail).
    """
    try:
        import veizik_entitlement as _ve
        src = open(_ve.__file__, "r", encoding="utf-8", errors="replace").read().lower()
    except Exception as e:
        return None, "could not inspect the license module (%s)" % type(e).__name__
    if "telemetry" in src:
        return False, "the license module references telemetry — the guarantee is NOT verified"
    return True, "verified at runtime: the license gate never reads your telemetry choice"


def _print_consent_step(step, index, total):
    """Render one screen of the spec §5 flow. Copy comes from veizik_telemetry so the CLI and
    any future GUI cannot drift apart in wording."""
    print("\n" + _RULE)
    print(" Step %d of %d   %s" % (index, total, step["title"]))
    print(_RULE)
    for para in step.get("body", []):
        print("  %s" % para)
    for item in step.get("items", []):
        print("    - %s" % item)
    if step.get("assurance"):
        print("\n  %s" % step["assurance"])


def _consent_flow(force=False):
    """First-run consent — TWO screens, shown in order, never merged into one checkbox.

    Screen 1 is a notice ([Continue] only): license operation data is what makes the licensed
    product function, so pretending it is optional would be dishonest.
    Screen 2 is the real, separable choice. BOTH answers continue, and No is a first-class
    outcome: no feature is gated on it anywhere in this codebase (see
    _license_gate_is_telemetry_free, which checks that rather than claiming it).
    """
    try:
        vt = _tel()
    except Exception as e:
        print("[consent] telemetry module unavailable (%s) — continuing without it." % type(e).__name__)
        return 0
    if vt.consent_asked() and not force:
        return 0

    copy = vt.consent_screen_text()
    step1, step2 = copy["step_1"], copy["step_2"]
    interactive = sys.stdin.isatty()

    # ---- 1/2  Veizik license operation  → [Continue] ----
    _print_consent_step(step1, 1, 2)
    if interactive:
        try:
            input("\n  [Continue] press Enter... ")
        except (EOFError, KeyboardInterrupt):
            print()

    # ---- 2/2  Help improve hardware compatibility → Yes / No, both proceed ----
    _print_consent_step(step2, 2, 2)
    print("\n  You stay in control of this at any time:")
    for line in step2.get("commands", []):
        print("    %s" % line)
    print("\n  %s" % vt.retention_note())
    print("\n  %s" % step2["no_lockout"])

    yes_label, no_label = step2["actions"][0], step2["actions"][1]
    if not interactive:
        # No terminal to answer with. We do NOT record a decision here: silence is not consent,
        # and recording "asked" would rob the user of ever seeing the real screen. Sharing stays
        # off, and the flow will run again on the next interactive invocation.
        print("\n  [%s] / [%s]" % (yes_label, no_label))
        print("  No answer recorded (not an interactive terminal). Sharing stays OFF until you")
        print("  choose. Decide any time with `veizik telemetry enable` / `veizik telemetry disable`.")
        print(_RULE)
        return 0

    print("\n    [1] %s" % yes_label)
    print("    [2] %s   (default)" % no_label)
    ans = ""
    try:
        ans = input("\n  Choose [1/2]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    yes = ans in ("1", "y", "yes")

    vt.set_consent(yes)
    print("\n" + _RULE)
    if yes:
        print("  Sharing performance data: ON  (consent version %s). Thank you." % vt.CONSENT_VERSION)
        _apply_contributor_benefits(vt)
    else:
        print("  Sharing performance data: OFF  (consent version %s)." % vt.CONSENT_VERSION)
        print("  Nothing is locked. %s" % step2["no_lockout"])
        ok, detail = _license_gate_is_telemetry_free()
        if ok:
            print("  %s" % detail)
        elif ok is False:
            print("  WARNING: %s" % detail)
    print(_RULE)
    return 0


def _apply_contributor_benefits(vt):
    """Grant the §6 benefits for consenting. Local record first (so it holds offline), server
    sync on a daemon thread. Any failure is silent by design — a benefit that fails to sync must
    never turn into an error the user has to deal with mid-render."""
    try:
        grant = vt.grant_contributor_benefits()
    except Exception:
        return
    if grant.get("granted_at"):
        print("  Granted: Starter Preview +%d days, plus the contributor benefits below."
              % grant.get("trial_extension_days", 3))
    print("  See them all:  veizik telemetry benefits")
    print("  Your report:   veizik telemetry contributor")


def _contributor_hardware():
    """Flatten _probe_hw_honest() into the display shape the contributor report prints.

    Anything we could not read stays absent rather than being defaulted to a plausible value —
    an unknown GPU printed as a guess is exactly the class of fake number this report exists to
    avoid."""
    hw = _probe_hw_honest()
    gpus = hw.get("gpus") or []
    out = {}
    if gpus:
        out["gpu"] = gpus[0].get("name")
        if gpus[0].get("vram_total_gb") is not None:
            out["vram_gb"] = "%.1f" % gpus[0]["vram_total_gb"]
        if gpus[0].get("driver"):
            out["driver"] = gpus[0]["driver"]
    if hw.get("gpu_count"):
        out["gpu_count"] = hw["gpu_count"]
    if hw.get("cuda_version"):
        out["cuda"] = hw["cuda_version"]
    out["os"] = hw.get("os")
    if hw.get("cpu_count"):
        out["cpu"] = "%s cores (%s)" % (hw["cpu_count"], hw.get("arch") or "?")
    if hw.get("ram_gb"):
        out["ram_gb"] = hw["ram_gb"]
    ver, _src = _app_version()
    out["runtime"] = "veizik %s / python %s%s" % (
        ver, hw.get("python") or "?",
        (" / torch %s" % hw["torch"]) if hw.get("torch") else " / torch not installed")
    return out


def cmd_status(args):
    ve = _ent()
    print("[status] %s" % ve.status_line(ve.resolve()))
    _usage_note()   # §14: status is where a stuck user looks; give them the one next step
    return 0


def cmd_logout(args):
    print("[logout] session removed" if _ent().logout() else "[logout] no active session")
    return 0


def _pack_files_intact(vp, pack_id):
    """True when the pack's files on disk still match the digests recorded at install.

    Used so `pack install` repairs a tampered/corrupted pack instead of reporting it as current.
    Any doubt returns False (reinstall is always safe — it re-verifies the signature).
    """
    try:
        for row in vp.verify_installed():
            if row.get("pack_id") == pack_id:
                return bool(row.get("ok"))
    except Exception:
        pass
    return False


def cmd_verify(args):
    """Prove you control the license email, so a payment binds to THIS key.

    Optional but recommended: without it the server falls back to a heuristic when deciding which
    key a payment upgrades. Verifying makes that binding provable, and it is what stops someone who
    merely knows your email from riding your subscription.
    """
    import json as _json
    import urllib.request as _u
    ve = _ent()
    # the session accessor is module-private (_load_session); fall back gracefully so `verify`
    # still works when nobody is logged in (email must then be supplied with --email).
    _ls = getattr(ve, "load_session", None) or getattr(ve, "_load_session", None)
    sess = None
    if _ls:
        try:
            sess = _ls()
        except Exception:
            sess = None
    # the license email lives inside the signed entitlement payload, not at the session top level
    payload = (sess or {}).get("payload") or {}
    email = getattr(args, "email", None) or (sess or {}).get("email") or payload.get("email")
    api_key = (sess or {}).get("api_key")
    if not email:
        print("[verify] no license email found — run `veizik login <KEY>` first, or pass --email")
        return 2
    body = {"email": email}
    if api_key:
        body["api_key"] = api_key
    req = _u.Request(ve.API_BASE + "/api/auth/magic-link", data=_json.dumps(body).encode(),
                     headers={"Content-Type": "application/json",
                              "User-Agent": "veizik-cli/1.0 (+https://veizik.com)"}, method="POST")
    try:
        with _u.urlopen(req, timeout=20) as r:
            out = _json.loads(r.read())
    except Exception as e:
        print("[verify] could not reach veizik.com: %s" % e)
        print("[verify] this is optional — your license keeps working.")
        return 1
    if not out.get("ok"):
        print("[verify] %s" % out.get("error", "request rejected"))
        return 1
    print("[verify] verification link sent to %s" % email)
    print("[verify] open it within 30 minutes; it can be used once.")
    if not out.get("delivered"):
        print("[verify] (queued for delivery — it may take a minute to arrive)")
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
    # §14: doctor completed -> if they have never rendered, the next step is a sample command.
    _mark_doctor_ok()
    _usage_note()
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
        # §14: a render that produced a file is the signal — count it, ping the trial clock on the
        # very first one, then show at most one hint (Founder offer / annual+Pro).
        _mark_render_success()
        _usage_note()
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
        # §14: same success signal as `veizik run` — only a real written file counts.
        _mark_render_success()
        _usage_note()
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
        ok, detail = _license_gate_is_telemetry_free()
        if ok:
            print("(%s)" % detail)
        elif ok is False:
            print("WARNING: %s" % detail)

        # §6 — what sharing buys. Shown to everyone: someone who declined is entitled to know
        # exactly what they are declining, and someone who consented is entitled to see that they
        # actually got it.
        print("\n" + vt.benefits_text())
        g = vt.contributor_grant()
        if g.get("granted_at"):
            print("\nYour grant: Starter Preview +%s days, recorded %s (server sync: %s)."
                  % (g.get("trial_extension_days", vt.TRIAL_EXTENSION_DAYS), g["granted_at"],
                     "confirmed" if g.get("server_synced") else "pending"))
        elif not on:
            print("\nTurn sharing on to claim these:  veizik telemetry enable")
        print("Your own report:  veizik telemetry contributor")
        return 0

    if act == "benefits":
        print("\n" + vt.benefits_text())
        g = vt.contributor_grant()
        if g.get("granted_at"):
            print("\nGranted on this machine: Starter Preview +%s days (%s, server sync: %s)."
                  % (g.get("trial_extension_days", vt.TRIAL_EXTENSION_DAYS), g["granted_at"],
                     "confirmed" if g.get("server_synced") else "pending"))
        elif not vt.enabled():
            print("\nNot claimed yet on this machine:  veizik telemetry enable")
        print("\nSee your own numbers:  veizik telemetry contributor")
        return 0

    if act == "contributor":
        # Hardware is probed HERE and passed in, so veizik_telemetry stays stdlib-only and never
        # imports the engine (a doctor-less, torch-less host must still print this report).
        hw = {}
        try:
            hw = _contributor_hardware()
        except Exception:
            hw = {}
        print("\n" + vt.contributor_report_text(hw))
        if not vt.enabled():
            print("\n(Sharing is OFF. The figures above are local-only and were never transmitted.)")
        return 0

    if act == "consent":
        # Re-show the two-step §5 flow on demand, e.g. to change your mind or to read it again.
        return _consent_flow(force=True)

    if act == "enable":
        vt.set_consent(True)
        print("[telemetry] performance data ENABLED (consent version %s)." % vt.CONSENT_VERSION)
        print("[telemetry] see exactly what is sent:  veizik telemetry status")
        _apply_contributor_benefits(vt)
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
            # _spool_read() yields {"file": ..., "report": {...}} — the run report is NESTED.
            # Reading the fields off the wrapper printed a table of "-" for every column.
            rep = it.get("report") or {}
            r = rep.get("result") or {}
            wl = rep.get("workload") or {}
            print("  %-20s %-10s %sx%s %ssteps  %-8s wall=%ss peak=%sGB"
                  % (str(rep.get("event_id", ""))[:20], wl.get("model_public_id", "-"),
                     wl.get("width", "-"), wl.get("height", "-"), wl.get("steps", "-"),
                     r.get("status", "-"), r.get("wall_s", "-"), r.get("peak_vram_gb", "-")))
        return 0

    if act == "send":
        # send() returns a bool ("did a batch flush cleanly"), not (n, msg). Unpacking it raised
        # TypeError and made `veizik telemetry send` fail outright.
        pending = vt.queue_count()
        ok = vt.send(force=getattr(args, "force", False))
        if ok:
            print("[telemetry] uploaded %d report(s); the local queue now holds %d."
                  % (pending - vt.queue_count(), vt.queue_count()))
        elif not pending:
            print("[telemetry] nothing queued to send.")
        elif not vt.enabled():
            print("[telemetry] sharing is disabled — %d report(s) held locally, none sent." % pending)
        else:
            print("[telemetry] not sent (endpoint unreachable, or the 24h batch window is not open "
                  "yet — use `--force`). %d report(s) still queued; your renders are unaffected."
                  % pending)
        return 0

    if act == "export":
        out = getattr(args, "out", None) or "veizik_telemetry_export.json"
        n = vt.queue_count()
        path = vt.export(out)          # returns the written path, or None on failure
        if not path:
            print("[telemetry] export failed (could not write %s)." % os.path.abspath(out))
            return 1
        print("[telemetry] exported consent, status, local state and %d pending report(s) -> %s"
              % (n, path))
        print("[telemetry] this is byte-for-byte what would be uploaded.")
        return 0

    if act == "delete":
        res = vt.delete_request()      # returns a dict, not (n, msg)
        print("[telemetry] removed %d locally queued report(s)." % res.get("local_purged", 0))
        print("[telemetry] %s" % res.get("message", ""))
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
    "pack": ("Shipped", ["starter", "creator", "pro", "studio"],
             "signed runtime packs: list/install/verify/status, Ed25519 verified",
             "the loader and verifier are in the public download and work today; the only pack "
             "published so far is Creator profiles/capsules — no native runtime pack exists yet"),
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
#   veizik queue | batch | recover | api | timemachine  (§14, last bullet)
#
#   None of these run in the public download. They exist in the grammar anyway, because a user who
#   types `veizik queue` deserves a real answer — stage, "not in this build", which plan gets it
#   first — instead of argparse's "invalid choice". This handler prints the FACTS every time; the
#   upgrade path is the one-per-day hint from _usage_note, so repeated attempts don't nag.
#
#   Honesty rule for this whole block: never imply that buying Pro makes the command work today.
# ---------------------------------------------------------------------------------------------------
_PRO_PREVIEW_CMDS = ("queue", "batch", "recover", "api", "timemachine")


def cmd_pro_preview(args):
    name = getattr(args, "preview_feature", "") or ""
    stage, plans, desc, caveat = _FEATURES.get(
        name, ("Development", ["pro"], name, "not shipped in the public download"))
    print("\n%s — %s" % (name, desc))
    print("  Stage           %s   (%s)" % (stage, " < ".join(_STAGES)))
    print("  In this build   NO — `veizik %s` is not implemented in the public download." % name)
    print("  Reality check   %s" % caveat)
    print("  Available on    %s" % ", ".join(_PLAN_NAME.get(p, p) for p in plans))

    tier = "free"
    try:
        ent = _ent().resolve(quiet=True)
        tier = ent.tier
        print("  Your plan       %s" % ent.label())
    except Exception:
        print("  Your plan       (no license session; run `veizik status`)")
    covered = {"free": "starter", "personal": "starter", "creator": "creator",
               "pro": "pro", "studio": "studio"}.get(tier, "starter") in plans

    if covered:
        # Already on a plan that includes it. There is nothing to sell — say so, and don't pretend
        # the command became runnable.
        print("\n  Your plan already covers %s for when it ships, through the Pro Preview channel." % name)
        print("  Nothing to buy. Watch the stage with:  veizik feature %s" % name)
    else:
        _usage_note(event="pro_attempt", feature=name)
    print("\n%s" % _measurement_note())
    # 3 = "this command did no work on this plan/build" — the same non-crashing code _gate_paid uses,
    # so a script can distinguish it from a real failure.
    return 3


# ---------------------------------------------------------------------------------------------------
#   veizik pack  (list | install | verify | status) — the tier-unlock mechanism.
#
#   There is ONE public binary. It contains the CLI parser, updater, doctor, license client,
#   telemetry client, pack loader, signature verifier and the public Adapter interface — and nothing
#   that a paid tier pays for. What a tier buys is a signed entitlement plus the right to fetch
#   private runtime packs. No per-tier executables exist and none are planned.
#
#   The sequence below is fixed and has no bypass flag:
#       license token -> feature manifest -> download -> SIGNATURE VERIFY -> unlock
#   Ed25519, with the private key offline and only the public key embedded in the client, so a
#   compromised veizik.com can serve any bytes it likes and every one of them is refused. All the
#   verification logic lives in veizik_packs.py (lazily imported — `veizik doctor` must keep working
#   on a stdlib-only host).
#
#   HONESTY: the native runtime is not distributable yet, so the only pack that exists today is the
#   Creator profile pack, and it is configuration — stable execution profiles, capsule definitions
#   and the selection rules. No .so, no .cu, no kernel. Say so on screen, every time.
# ---------------------------------------------------------------------------------------------------
def _pack_tier():
    """(tier_id, human_label) for the current seat. Free when there is no session."""
    try:
        ent = _ent().resolve(quiet=True)
        return ent.tier, ent.label()
    except Exception:
        return "free", "Free"


def _pack_reality_note():
    return ("Only configuration packs exist today (profiles, capsules, selection rules).\n"
            "  The LimML native runtime, advanced kernels and quantization engine are NOT yet\n"
            "  distributable and are in no pack. Render-time figures: measurement in progress.")


def _pack_install_results(vp, pack_id, tier, dry_run):
    """Adapt veizik_packs' install API to one uniform result list for printing.

    veizik_packs.install() takes a single verified manifest ENTRY and raises on
    refusal, while ensure_for_tier() syncs everything the tier allows and returns
    installed/skipped/failed buckets. The CLI wants one flat list either way, and
    it must NOT re-implement any of the checks — every decision below still comes
    out of veizik_packs, which is where the security properties live.
    """
    if pack_id:
        doc = vp.fetch_manifest()
        src = doc.get("_source") or vp.manifest_url()
        entry = next((e for e in doc.get("packs", [])
                      if isinstance(e, dict) and str(e.get("pack_id") or "") == pack_id), None)
        if entry is None:
            known = ", ".join(sorted(str(e.get("pack_id")) for e in doc.get("packs", [])
                                     if isinstance(e, dict))) or "(none)"
            raise vp.PackError("no pack named %r in the manifest. Available: %s" % (pack_id, known))
        cur = vp.installed().get(pack_id)
        if cur and cur.get("version") == entry.get("version") \
                and cur.get("sha256") == str(entry.get("sha256") or "").lower() \
                and _pack_files_intact(vp, pack_id):
            # Matching version+sha is not enough: if the files on disk were modified since install,
            # `pack verify` tells the user to reinstall, so install must actually repair rather than
            # short-circuit — otherwise the remediation we print is a dead end.
            return [{"pack_id": pack_id, "action": "already-current", "version": cur.get("version")}]
        try:
            if dry_run:
                # The signature is verified even for --dry-run: "what would happen"
                # is only a useful answer if it reflects the real gate.
                signed = vp.verify_entry(entry)
                return [{"pack_id": pack_id, "action": "would-install",
                         "version": signed["version"], "tier": signed["tier"]}]
            rec = vp.install(entry, tier=tier, manifest_src=src)
        except vp.PackError as exc:
            return [{"pack_id": pack_id, "action": "refused", "reason": str(exc)}]
        return [{"pack_id": pack_id, "action": "installed", "version": rec["version"],
                 "tier": rec["tier"], "files": len(rec.get("files") or {}),
                 "path": rec.get("path", "")}]

    res = vp.ensure_for_tier(tier, dry_run=dry_run)
    out = []
    seen = set()
    for item in res.get("installed", []):
        pid = item["pack_id"]
        seen.add(pid)
        if item.get("planned"):
            out.append({"pack_id": pid, "action": "would-install",
                        "version": item.get("version"), "tier": item.get("tier")})
            continue
        rec = vp.installed().get(pid, {})
        out.append({"pack_id": pid, "action": "installed", "version": item.get("version"),
                    "tier": rec.get("tier", tier), "files": len(rec.get("files") or {}),
                    "path": rec.get("path", "")})
    for item in res.get("skipped", []):
        pid = item["pack_id"]
        seen.add(pid)
        if item.get("reason") == "already current":
            out.append({"pack_id": pid, "action": "already-current", "version": item.get("version")})
        else:
            # 'above tier' — say which plan covers it and where to get it.
            out.append({"pack_id": pid, "action": "refused",
                        "reason": "%s is a %s pack and your licence is %s.\n"
                                  "  Upgrade at %s, then run:  veizik activate <YOUR_KEY>"
                                  % (pid, vp.TIER_LABEL.get(item.get("tier"), item.get("tier")),
                                     vp.TIER_LABEL.get(tier, tier), vp.UPGRADE_URL)})
    for item in res.get("failed", []):
        seen.add(item.get("pack_id", "?"))
        out.append({"pack_id": item.get("pack_id", "?"), "action": "refused",
                    "reason": item.get("error", "unknown error")})

    # Reconcile against the RAW manifest. ensure_for_tier() builds its worklist from
    # manifest_entries(), which DROPS entries that fail signature or shape validation
    # — correct for installing (a poisoned entry must not stop the packs you paid
    # for), but it means a wholly tampered manifest would otherwise surface here as a
    # cheerful "nothing to install". That is the one message a user must never get
    # when their mirror is hostile, so re-verify anything unaccounted for and report
    # the real reason. Nothing is installed on this path; it only turns silence into
    # an explanation.
    try:
        doc = vp.fetch_manifest()
    except vp.PackError:
        return out                           # transport failure already reported above
    for entry in doc.get("packs", []):
        if not isinstance(entry, dict):
            continue
        pid = str(entry.get("pack_id") or "?")
        if pid in seen:
            continue
        try:
            vp.verify_entry(entry)
            reason = ("entry verified but was not offered for install on this tier "
                      "(it may be superseded by another entry for the same pack)")
        except vp.PackError as exc:
            reason = str(exc)
        out.append({"pack_id": pid, "action": "refused", "reason": reason})
    return out


def cmd_pack(args):
    vp = _packs()
    act = getattr(args, "pack_action", None) or "list"
    tier, label = _pack_tier()

    # --- pack list ------------------------------------------------------------------------------
    if act == "list":
        _banner()
        print("\n[pack] licence: %s   manifest: %s" % (label, vp.manifest_url()))
        if not vp.crypto_available():
            print("[pack] WARNING: `cryptography` is not installed, so no signature can be checked.")
            print("       Installation will be REFUSED until it is:  python3 -m pip install cryptography")
        try:
            rows = vp.list_packs(tier)
        except vp.PackError as e:
            print("\n[pack] %s" % e)
            return 2
        if not rows:
            print("\n[pack] the manifest lists no packs.")
            return 0
        hdr = ("pack", "version", "tier", "signature", "your access", "installed")
        table = []
        for r in rows:
            sig = "verified" if r["signature_ok"] else "REFUSED"
            if not r["signature_ok"]:
                access = "-"
            elif r["eligible"]:
                access = "available"
            else:
                access = "needs %s" % vp.TIER_LABEL.get(r["tier"], r["tier"])
            table.append((r["pack_id"], r["version"], r["tier"], sig, access,
                          r["installed_version"] or "-"))
        widths = [max(len(str(x[i])) for x in ([hdr] + table)) for i in range(len(hdr))]
        def fmt(row):
            return "  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row))
        print("")
        print(fmt(hdr))
        print("  " + "  ".join("-" * w for w in widths))
        for row in table:
            print(fmt(row))
        for r in rows:
            # `summary` / `contains` / `unlocks` are NOT covered by the pack signature, so a hostile
            # mirror could use them to describe a config pack as a native-kernel pack. Print them
            # only with an explicit (unverified) marker, never flush against "signature: verified".
            print("\n  %s %s" % (r["pack_id"], r["version"]))
            if r["summary"]:
                print("    (unverified description)  %s" % r["summary"])
            if r["contains"]:
                print("    (unverified)  claims to contain: %s" % ", ".join(r["contains"]))
            if not r["signature_ok"]:
                print("    REFUSED   %s" % r["reason"])
        print("\n[pack] %s" % _pack_reality_note())
        print("[pack] install with:  veizik pack install <pack_id>")
        return 0

    # --- pack install ---------------------------------------------------------------------------
    if act == "install":
        _banner()
        print("\n[pack] licence: %s" % label)
        try:
            results = _pack_install_results(vp, getattr(args, "pack_id", None), tier,
                                            getattr(args, "dry_run", False))
        except vp.PackError as e:
            print("\n[pack] %s" % e)
            return 3
        refused = 0
        for res in results:
            pid = res["pack_id"]
            action = res["action"]
            if action == "installed":
                print("\n[pack] installed %s %s (tier %s, %d file(s))"
                      % (pid, res["version"], res["tier"], res["files"]))
                print("       -> %s" % res["path"])
            elif action == "already-current":
                print("\n[pack] %s %s is already installed and current." % (pid, res["version"]))
            elif action == "would-install":
                print("\n[pack] --dry-run: would install %s %s (tier %s); signature already verified."
                      % (pid, res["version"], res["tier"]))
            else:
                refused += 1
                print("\n[pack] refused %s:\n  %s" % (pid, res["reason"]))
        if not results:
            print("\n[pack] nothing to install.")
        # Say what installing actually did — nothing is "unlocked" until the render path consults
        # these packs, and today it does not. Claiming otherwise contradicts `veizik feature`.
        unlocked = vp.unlocked_features()
        if unlocked:
            print("\n[pack] installed data now available to the runtime: %s"
                  % ", ".join("%s (%s)" % (f, p) for f, p in sorted(unlocked.items())))
            print("[pack] note: these are execution profiles and capsule definitions. The render"
                  "\n       path does not consume them yet — that lands with the native engine pack.")
        print("\n[pack] %s" % _pack_reality_note())
        # A refusal is a real, actionable answer (wrong tier, bad signature, downgrade attempt),
        # not a crash — distinct exit code so scripts can tell it from a transport failure.
        return 3 if refused else 0

    # --- pack verify ----------------------------------------------------------------------------
    if act == "verify":
        _banner()
        rows = vp.verify_installed()
        if not rows:
            print("\n[pack] no packs installed — nothing to verify.")
            return 0
        print("\n[pack] re-verifying %d installed pack(s): file contents + detached signature\n"
              % len(rows))
        bad = 0
        for r in rows:
            if r["ok"]:
                print("  OK       %-22s %-8s tier=%-8s %d file(s), signature re-checked"
                      % (r["pack_id"], r["version"], r["tier"], r.get("files", 0)))
            else:
                bad += 1
                print("  FAILED   %-22s %-8s %s" % (r["pack_id"], r["version"] or "?", r["reason"]))
        if bad:
            print("\n[pack] %d pack(s) FAILED verification. They are on disk but must not be trusted;"
                  % bad)
            print("       reinstall with:  veizik pack install <pack_id>")
            return 4
        print("\n[pack] every installed pack matches what veizik signed.")
        return 0

    # --- pack status ----------------------------------------------------------------------------
    if act == "status":
        _banner()
        inst = vp.installed()
        print("\n[pack] licence          %s" % label)
        print("[pack] manifest         %s" % vp.manifest_url())
        print("[pack] signature check  %s"
              % ("Ed25519, embedded public key" if vp.crypto_available()
                 else "UNAVAILABLE (`cryptography` missing) — installs are refused"))
        print("[pack] install root     %s" % vp.PACKS_DIR)
        if not inst:
            print("\n[pack] no packs installed. This seat runs the public path only:")
            print("       the limml_universal autotuner derives a plan from your hardware")
            print("       on every render, with no stable profile applied.")
            print("\n[pack] see what you can install:  veizik pack list")
            print("\n[pack] %s" % _pack_reality_note())
            return 0
        print("\n[pack] installed:")
        for pid, rec in sorted(inst.items()):
            print("\n  %s %s   (tier %s, installed %s)"
                  % (pid, rec.get("version"), rec.get("tier"), rec.get("installed_at")))
            if rec.get("summary"):
                print("    %s" % rec["summary"])
            print("    path      %s" % rec.get("path"))
            print("    files     %d" % len(rec.get("files") or {}))
            print("    sha256    %s" % (rec.get("sha256") or "")[:32])
        unlocked = vp.unlocked_features()
        print("\n[pack] features unlocked by installed packs:")
        if unlocked:
            for feat, pid in sorted(unlocked.items()):
                print("    %-14s <- %s" % (feat, pid))
        else:
            print("    (none)")
        prof = vp.load_profiles()
        if prof:
            print("\n[pack] active profile source: %s (%d stable profile(s))"
                  % (prof.get("_pack_id"), len(prof.get("profiles") or {})))
        print("\n[pack] verify these against the signature at any time:  veizik pack verify")
        print("\n[pack] %s" % _pack_reality_note())
        return 0

    print("[pack] unknown action: %s" % act)
    return 2


# ---------------------------------------------------------------------------------------------------
#   veizik setup — first-run onboarding.
#
#   One command that walks the whole first five minutes: what this machine can run, the TWO-SCREEN
#   consent (§5), how to get a key if you don't have one, and the next command to type. Re-running it
#   after setup is a status summary rather than a wizard, so it is always safe to type.
# ---------------------------------------------------------------------------------------------------
def _hw_summary_lines(hw, tier, verdict):
    """Compact, provenance-honest hardware lines shared by setup and benchmark."""
    lines = []
    gpus = hw.get("gpus") or []
    if gpus:
        for i, g in enumerate(gpus):
            vram = ("%.1f GB" % g["vram_total_gb"]) if g.get("vram_total_gb") is not None else "unknown"
            free = ("%.1f GB free" % g["vram_free_gb"]) if g.get("vram_free_gb") is not None else "free unknown"
            lines.append("  GPU %d           %s | %s | %s | cc %s | driver %s"
                         % (i, g.get("name") or "unknown", vram, free,
                            g.get("compute_cap") or "?", g.get("driver") or "?"))
    else:
        lines.append("  GPU             none detected (nvidia-smi absent or no NVIDIA device)")
    lines.append("  accelerator     %s" % (hw.get("accel") or "none"))
    lines.append("  torch           %s" % (hw.get("torch") or "not installed — the render path needs it"))
    if hw.get("cuda_version"):
        lines.append("  CUDA            %s" % hw["cuda_version"])
    lines.append("  OS / arch       %s / %s   (python %s)" % (hw.get("os"), hw.get("arch"), hw.get("python")))
    lines.append("  host RAM        %s" % (("%.0f GB" % hw["ram_gb"]) if hw.get("ram_gb") else "unknown"))
    lines.append("  support tier    %s — %s" % (tier, verdict))
    return lines


def cmd_setup(args):
    _banner()
    st = _usage_load()
    configured = False
    try:
        configured = bool(_tel().consent_asked())
    except Exception:
        pass

    ve = _ent()
    try:
        ent = ve.resolve(quiet=True)
    except Exception:
        ent = None
    has_key = bool(ent is not None and ent.source != "free")

    # ---- already set up: summarise instead of re-running the wizard --------------------------------
    if configured and not args.force:
        print("\nThis machine is already set up.\n")
        print("  licence         %s" % (ve.status_line(ent) if ent is not None else "unknown"))
        try:
            vt = _tel()
            print("  telemetry       %s (consent %s)"
                  % ("enabled" if vt.enabled() else "disabled",
                     (vt.consent_state() or {}).get("consent_version") or "-"))
            print("  install id      %s   (pseudonymous)" % vt.installation_id())
        except Exception as e:
            print("  telemetry       unavailable (%s)" % type(e).__name__)
        print("  first render    %s" % (st.get("first_success_at") or "not yet"))
        print("  renders         %d successful on this machine" % int(st.get("render_successes") or 0))
        print("  app version     %s" % _app_version()[0])
        print("\nRe-run the consent screens with:  veizik setup --force")
        print("Hardware detail:                 veizik doctor")
        _usage_note()
        return 0

    # ---- 1. what this machine is -----------------------------------------------------------------
    # NOTE: no "step 1 of 3" numbering here. _consent_flow() prints its own mandatory "Step 1 of 2 /
    # Step 2 of 2" screens (§5) and two nested counters read as a broken wizard.
    print("\n== This machine " + "=" * 61 + "\n")
    hw = _probe_hw_honest()
    tier, verdict = _tier_verdict(hw)
    for ln in _hw_summary_lines(hw, tier, verdict):
        print(ln)
    print("\n  Full per-family table:  veizik doctor")
    print("  %s" % _measurement_note())

    # ---- 2. consent, two separate screens (§5) ----------------------------------------------------
    print("\n== What veizik processes " + "=" * 52 + "")
    _consent_flow(force=args.force)

    # ---- 3. licence ------------------------------------------------------------------------------
    print("\n== Licence " + "=" * 66 + "\n")
    if has_key:
        print("  %s" % ve.status_line(ent))
        print("  Already activated on this machine. To move the seat elsewhere: veizik deactivate")
    else:
        print("  No key is active on this machine, so you are on the free path.")
        print("")
        print("  Get a free Starter Preview key:")
        print("      1. open https://veizik.com")
        print("      2. sign up with your email — the key is shown on screen and emailed to you")
        print("      3. veizik activate vzk_live_...")
        print("")
        print("  Starter Preview: free for 7 days, 1 registered device, 1 concurrent node,")
        print("  doctor + limited sample/capsule runs, experimental universal path, non-commercial.")
        print("  The 7 days start at your FIRST SUCCESSFUL RENDER, not at activation.")
        print("  Sharing performance data adds 3 days (see `veizik telemetry status`).")

    _usage_save(dict(st, setup_at=_iso_now()))

    print("\n" + "-" * 78)
    print(" Next")
    print("-" * 78)
    if not has_key:
        print("  veizik activate <YOUR_KEY>   activate this machine")
    print("  veizik doctor                full hardware + per-family support tier table")
    print("  veizik benchmark             record this machine's execution profile")
    print("  veizik plans                 what each plan includes (billed by concurrent nodes)")
    print("  veizik feature list          honest lifecycle status of every feature")
    print("\nWhat runs in this download today: doctor, activate/status/logout, free entitlement,")
    print("telemetry, plans, feature, upgrade, verify, pack — plus an EXPERIMENTAL universal")
    print("t2v/t2i path (Linux + NVIDIA, your own torch/diffusers). ComfyUI run/serve, TimeMachine,")
    print("the native CUDA engine, Queue/batch, Recover and the API bridge are NOT in it yet.")
    _usage_note()
    return 0


# ---------------------------------------------------------------------------------------------------
#   veizik benchmark — this machine's execution profile.
#
#   HONESTY BOUNDARY, and it is the whole design of this command: the native engine is not in the
#   public download, so there is no render to time. Inventing a "12.4 s/frame" here would be the
#   easiest lie in the product and it is the one thing this command must never do. What it CAN
#   measure it measures for real (hardware facts, and — only when torch is present — a GEMM and a
#   memory-bandwidth micro-benchmark). Everything else is written out as "measurement in progress".
# ---------------------------------------------------------------------------------------------------
def _bench_time(fn, sync, warmup, iters):
    """Timed loop with a real warmup. Without the warmup the first call carries context creation,
    kernel autotuning and allocator growth, and the number comes out 2-10x too slow."""
    for _ in range(warmup):
        fn()
    sync()
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    sync()
    return (time.perf_counter() - t0) / iters


def _microbench(device=None):
    """GEMM throughput + memory bandwidth. Returns a dict where every entry is either a measured
    number or an explicit reason it was not measured. Never raises, never estimates."""
    out = {"device": None, "dtype": None, "gemm_tflops": None, "gemm_shape": None,
           "device_copy_gb_s": None, "host_to_device_gb_s": None, "notes": []}
    try:
        import torch
    except Exception as e:
        out["notes"].append("torch is not installed (%s) — no micro-benchmark is possible. "
                            "%s for GEMM and bandwidth." % (type(e).__name__, _UNMEASURED))
        return out

    forced = bool(device)
    if device:
        dev = device
    elif torch.cuda.is_available():
        dev = "cuda"
    elif getattr(getattr(torch, "backends", None), "mps", None) is not None \
            and torch.backends.mps.is_available():
        dev = "mps"
    else:
        dev = "cpu"
    out["device"] = dev
    out["device_forced"] = forced
    if dev == "cpu":
        out["notes"].append(("--device cpu was requested" if forced
                             else "no GPU available to torch")
                            + " — the numbers below are CPU numbers and say nothing about render "
                              "performance.")
    elif dev == "mps":
        out["notes"].append("Apple MPS: measured for the record, but MPS is not a supported veizik "
                            "render target.")

    def _sync():
        try:
            if dev == "cuda":
                torch.cuda.synchronize()
            elif dev == "mps":
                torch.mps.synchronize()
        except Exception:
            pass

    # --- GEMM ------------------------------------------------------------------------------------
    n = 4096 if dev == "cuda" else (2048 if dev == "mps" else 1024)
    dtype = torch.float16 if dev in ("cuda", "mps") else torch.float32
    out["dtype"] = str(dtype).replace("torch.", "")
    out["gemm_shape"] = "%dx%dx%d" % (n, n, n)
    try:
        a = torch.randn(n, n, device=dev, dtype=dtype)
        b = torch.randn(n, n, device=dev, dtype=dtype)
        iters = 20 if dev == "cuda" else (10 if dev == "mps" else 3)
        sec = _bench_time(lambda: torch.matmul(a, b), _sync, warmup=3, iters=iters)
        out["gemm_tflops"] = round(2.0 * n ** 3 / sec / 1e12, 2)
        out["gemm_ms"] = round(sec * 1e3, 3)
        del a, b
    except Exception as e:
        out["notes"].append("GEMM micro-benchmark failed (%s: %s) — %s" % (type(e).__name__, e, _UNMEASURED))

    # --- on-device memory bandwidth (read + write) -------------------------------------------------
    try:
        elems = 64 * 1024 * 1024 if dev == "cuda" else 16 * 1024 * 1024
        src = torch.empty(elems, device=dev, dtype=torch.float32)
        dst = torch.empty_like(src)
        nbytes = src.numel() * src.element_size()
        sec = _bench_time(lambda: dst.copy_(src), _sync, warmup=2,
                          iters=10 if dev != "cpu" else 3)
        out["device_copy_gb_s"] = round(2.0 * nbytes / sec / 1e9, 1)   # 1 read + 1 write
        del src, dst
    except Exception as e:
        out["notes"].append("memory-bandwidth micro-benchmark failed (%s: %s) — %s"
                            % (type(e).__name__, e, _UNMEASURED))

    # --- host->device transfer (the offload path's actual cost) ------------------------------------
    if dev == "cuda":
        try:
            elems = 32 * 1024 * 1024
            host = torch.empty(elems, dtype=torch.float32).pin_memory()
            devt = torch.empty(elems, device=dev, dtype=torch.float32)
            nbytes = host.numel() * host.element_size()
            sec = _bench_time(lambda: devt.copy_(host, non_blocking=True), _sync, warmup=2, iters=10)
            out["host_to_device_gb_s"] = round(nbytes / sec / 1e9, 1)
            del host, devt
        except Exception as e:
            out["notes"].append("H2D micro-benchmark failed (%s: %s) — %s"
                                % (type(e).__name__, e, _UNMEASURED))
    else:
        out["notes"].append("host->device transfer is CUDA-only; not measured on %s." % dev)

    try:
        if dev == "cuda":
            torch.cuda.empty_cache()
    except Exception:
        pass
    return out


def _ram_bucket(gb):
    """Coarse RAM bucket. Telemetry gets a bucket, not the exact byte count, because the exact
    figure is closer to a machine fingerprint than to a compatibility signal."""
    if not gb:
        return None
    for edge in (8, 16, 32, 64, 128, 256, 512):
        if gb <= edge * 1.05:
            return edge
    return 1024


def _bench_run_report(hw, micro, started, ended, wall_s):
    """Assemble a veizik-run-report-v1 for this benchmark and put it through the SAME scrubber the
    render path uses. The micro-benchmark numbers are deliberately NOT in here: the run-report
    allowlist has no field for them, and widening an allowlist to fit a payload is how privacy
    promises rot. They stay in the local file only."""
    vt = _tel()
    gpus = hw.get("gpus") or []
    g0 = gpus[0] if gpus else {}
    cc = g0.get("compute_cap")
    try:
        ent = _ent().resolve(quiet=True)
        tier_id = {"free": "starter_preview", "personal": "starter_preview", "creator": "creator",
                   "pro": "pro", "studio": "studio"}.get(ent.tier, "starter_preview")
    except Exception:
        tier_id = "starter_preview"
    report = vt.build_report(
        license_tier=tier_id,
        runtime={"veizik_version": _app_version()[0],
                 "execution_backend": hw.get("accel") or "none",
                 "profile_id": "benchmark-microbench-v1"},
        hardware={"gpu_vendor": hw.get("gpu_vendor") or "none",
                  "gpu_model": g0.get("name"),
                  "gpu_vram_gb": g0.get("vram_total_gb"),
                  "gpu_count": hw.get("gpu_count") or 0,
                  "gpu_arch": ("sm_%s" % cc.replace(".", "")) if cc else None,
                  "driver_version": g0.get("driver"),
                  "cuda_version": hw.get("cuda_version"),
                  "os_major": hw.get("os_major"),
                  "cpu_class": hw.get("arch"),
                  "ram_bucket_gb": _ram_bucket(hw.get("ram_gb"))},
        workload={"model_public_id": "benchmark:microbench",
                  "precision": micro.get("dtype") or "n/a",
                  "batch": 1, "start_kind": "cold"},
        result={"started_at": started, "ended_at": ended, "wall_s": round(wall_s, 3),
                "status": "ok"},
    )
    return vt._scrub(report)


def cmd_benchmark(args):
    _banner()
    t_start = time.time()
    started = _iso_now()
    print("\n[benchmark] execution profile for this machine")
    print("[benchmark] %s" % _measurement_note())

    # ---- 1. hardware ------------------------------------------------------------------------------
    hw = _probe_hw_honest()
    tier, verdict = _tier_verdict(hw)
    print("\n[hardware]")
    for ln in _hw_summary_lines(hw, tier, verdict):
        print(ln)
    print("  probe sources   %s" % ", ".join("%s=%s" % (k, v) for k, v in sorted(hw.get("sources", {}).items())))

    # ---- 2. per-family support tier, but ONLY from measured hardware -------------------------------
    fam_rows = []
    if hw.get("accel") == "cuda" and (hw.get("gpus") or []):
        try:
            import copy
            lu = _lu()
            g0 = hw["gpus"][0]
            probe = lu.HwProfile()
            probe.n_gpus = hw.get("gpu_count") or 1
            probe.gpu_name = g0.get("name") or probe.gpu_name
            if g0.get("vram_total_gb"):
                probe.gpu_total_gb = g0["vram_total_gb"]
                probe.gpu_free_gb = g0.get("vram_free_gb") or g0["vram_total_gb"]
            cc = (g0.get("compute_cap") or "").split(".")
            if len(cc) == 2 and cc[0].isdigit() and cc[1].isdigit():
                probe.sm = int(cc[0]) * 10 + int(cc[1])
            probe.fp8_tc = probe.sm >= 89
            probe.int8_tc = probe.sm >= 75
            if hw.get("ram_gb"):
                probe.host_ram_gb = hw["ram_gb"]
            for fam in _FAMILY_ORDER:
                tmpl = lu.FAMILY_TEMPLATES.get(fam)
                if tmpl is None:
                    continue
                card = copy.deepcopy(tmpl)
                card.confidence = 0.99
                plan = lu.autotune(card, probe, args.target)
                fam_rows.append((fam, plan.support_tier, plan.dtype, plan.attention, plan.offload))
        except Exception as e:
            print("\n[families] planner unavailable (%s: %s)" % (type(e).__name__, e))
    if fam_rows:
        print("\n[families]  planned from the MEASURED GPU above (not from defaults)")
        print("  %-14s %-5s %-6s %-12s %s" % ("family", "tier", "dtype", "attention", "offload"))
        for r in fam_rows:
            print("  %-14s %-5s %-6s %-12s %s" % r)
        print("  render time     %s for every row — the native engine is not in this download" % _UNMEASURED)
    else:
        print("\n[families] not planned: no measured CUDA GPU on this host. `veizik doctor` will still")
        print("           print the table, but from limml_universal's DEFAULT profile, not from"
              " measurement.")

    # ---- 3. micro-benchmarks ----------------------------------------------------------------------
    micro = {"skipped": True, "notes": ["--no-micro was passed"]} if args.no_micro \
        else _microbench(args.device or None)
    print("\n[microbench]")
    if args.no_micro:
        print("  skipped (--no-micro)")
    else:
        print("  device          %s" % (micro.get("device") or "none"))
        print("  GEMM %-10s %s" % (micro.get("gemm_shape") or "",
                                   ("%.2f TFLOP/s (%s, %.3f ms)"
                                    % (micro["gemm_tflops"], micro.get("dtype"), micro.get("gemm_ms", 0.0)))
                                   if micro.get("gemm_tflops") else _UNMEASURED))
        print("  device copy     %s" % (("%.1f GB/s (read+write)" % micro["device_copy_gb_s"])
                                        if micro.get("device_copy_gb_s") else _UNMEASURED))
        print("  host -> device  %s" % (("%.1f GB/s (pinned)" % micro["host_to_device_gb_s"])
                                        if micro.get("host_to_device_gb_s") else _UNMEASURED))
        for note in micro.get("notes") or []:
            print("  note            %s" % note)

    # ---- 4. what is NOT measured, said plainly ----------------------------------------------------
    print("\n[not measured]")
    print("  render time / frames-per-second       %s" % _UNMEASURED)
    print("  peak VRAM for a real render           %s" % _UNMEASURED)
    print("  native T1 engine throughput           %s (engine not distributed)" % _UNMEASURED)
    print("  The single measured figure veizik publishes today is LTX-13B peak VRAM 9.55 GB")
    print("  (internal) plus block/engine-level rel_L2 accuracy.")

    # ---- 5. run-report + telemetry ----------------------------------------------------------------
    ended = _iso_now()
    wall = time.time() - t_start
    report, queued, clean = None, None, None
    try:
        vt = _tel()
        report = _bench_run_report(hw, micro, started, ended, wall)
        clean = vt.report_is_clean(report)
        if vt.is_enabled():
            # Deliberately spool directly instead of calling record_run(): that helper bumps
            # cumulative_successes and can set first_success_at, i.e. it would let a benchmark
            # masquerade as a render. §16 counts "first successful render" as a real metric and the
            # Starter trial clock starts there, so a benchmark must never touch either.
            queued = vt._spool(report)
    except Exception as e:
        print("\n[report] could not build the run report (%s: %s)" % (type(e).__name__, e))

    out_path = args.out or os.path.join(_VEIZIK_HOME,
                                        "benchmark-%s.json" % time.strftime("%Y%m%dT%H%M%SZ",
                                                                            time.gmtime()))
    doc = {"veizik_benchmark": "v1",
           "generated_at": ended,
           "app_version": _app_version()[0],
           "hardware_probe": hw,
           "support_tier": {"tier": tier, "verdict": verdict, "families": [
               {"family": f, "tier": t, "dtype": d, "attention": a, "offload": o}
               for (f, t, d, a, o) in fam_rows]},
           "microbench": micro,
           "unmeasured": {"render_time_s": _UNMEASURED, "peak_vram_gb": _UNMEASURED,
                          "native_engine_throughput": _UNMEASURED},
           # schema-compatible: this is exactly the object telemetry would transmit
           "run_report": report,
           "run_report_schema": getattr(_tel_safe(), "SCHEMA_VERSION", "veizik-run-report-v1"),
           "run_report_passes_scrubber": clean}
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(doc, f, indent=2, default=str)
        print("\n[report] written -> %s" % os.path.abspath(out_path))
        print("[report] run_report section conforms to %s%s"
              % (doc["run_report_schema"],
                 "" if clean is not False else "  (WARNING: it did NOT pass the scrubber)"))
        print("[report] the micro-benchmark numbers stay in this file: the run-report allowlist has")
        print("         no field for them, and we do not widen an allowlist to fit a payload.")
    except OSError as e:
        print("\n[report] could not write %s (%s)" % (out_path, e))

    if queued:
        print("\n[telemetry] queued for the next batch (you consented to performance data).")
        print("[telemetry] see the exact bytes:   veizik telemetry show-last")
        print("[telemetry] send them now:         veizik telemetry send --force")
        print("\n[contributor] Telemetry Contributor — what sharing gets you")
        print("  your hardware        %s" % ((hw.get("gpus") or [{}])[0].get("name") or "no GPU detected"))
        print("  community median     %s (needs contributed reports before a median exists)" % _UNMEASURED)
        print("  yours vs median      %s" % _UNMEASURED)
        print("  stability            %s" % _UNMEASURED)
        print("  recommended profile  %s — stable profiles ship in the Creator pack" % _UNMEASURED)
        print("  also included        automatic profile correction, priority compatibility analysis,")
        print("                       an externally-verified Badge, early Adapter access,")
        print("                       +3 days of Starter Preview, optional public benchmark")
        print("                       contributor credit, and a vote on which hardware we support next.")
    else:
        print("\n[telemetry] not queued — optional performance sharing is off, which locks nothing.")
        print("[telemetry] sharing would add: a benchmark report for your machine, automatic profile")
        print("            correction, priority compatibility analysis, an external Badge, early")
        print("            Adapter access, +3 days of Starter Preview, and a hardware-support vote.")
        print("[telemetry] turn it on:  veizik telemetry enable")

    _usage_note()
    return 0


def _tel_safe():
    """_tel() but returns a stub object on import failure — used only for a version string."""
    try:
        return _tel()
    except Exception:
        return type("_Stub", (), {"SCHEMA_VERSION": "veizik-run-report-v1"})()


# ---------------------------------------------------------------------------------------------------
#   veizik deactivate — release THIS machine's registration.
#
#   The seat lives on the server, so the release has to happen there; wiping the local session alone
#   would leave the user's device count silently consumed. We therefore call the server FIRST and
#   only then drop local state, and we say plainly what happened when the server is unreachable.
# ---------------------------------------------------------------------------------------------------
_DEVICE_RELEASE_LIMIT = 3          # device changes per year; the server is the authority, not this


def _release_device(api_key, device_id):
    """POST /api/device/release. Returns (ok, payload_or_error_string)."""
    import urllib.error
    ve = _ent()
    try:
        return True, ve._http("POST", "/api/device/release",
                              {"api_key": api_key, "device_id": device_id})
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {}
        if e.code == 404:
            return False, ("the server has no /api/device/release route yet (HTTP 404)")
        return False, (body.get("error") or "server refused the release (HTTP %d)" % e.code)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def cmd_deactivate(args):
    _banner()
    ve = _ent()
    _ls = getattr(ve, "load_session", None) or getattr(ve, "_load_session", None)
    sess = None
    if _ls:
        try:
            sess = _ls()
        except Exception:
            sess = None
    api_key = (sess or {}).get("api_key")

    try:
        import veizik_session as vs
        device_id = vs.device_id()
    except Exception as e:
        device_id = None
        print("[deactivate] could not read this machine's device id (%s)" % e)

    if not api_key:
        print("\n[deactivate] no licence is active on this machine — nothing to release.")
        print("[deactivate] `veizik logout` clears a local session; `veizik activate <KEY>` adds one.")
        return 0

    print("\n  licence         %s" % ve.status_line(ve.resolve(quiet=True)))
    print("  device id       %s   (pseudonymous, local)" % (device_id or "unknown"))
    print("\nThis releases THIS machine's registration so you can activate another one.")
    print("Your licence, your plan and your renders are untouched, and you can re-activate here")
    print("at any time with the same key.")

    if not args.yes:
        try:
            ans = input("\nRelease this machine? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("[deactivate] cancelled — nothing was changed.")
            return 0

    # 1. server side first: the seat is the thing that actually matters.
    ok, res = (False, "skipped (--local-only)") if args.local_only \
        else _release_device(api_key, device_id)
    if ok and isinstance(res, dict) and not res.get("error"):
        print("\n[deactivate] server released this device.")
        used = res.get("releases_used")
        remaining = res.get("releases_remaining")
        limit = res.get("releases_limit") or _DEVICE_RELEASE_LIMIT
        if remaining is None and used is not None:
            try:
                remaining = max(0, int(limit) - int(used))
            except (TypeError, ValueError):
                remaining = None
        if remaining is not None:
            print("[deactivate] device changes: %s of %s used this year, %s remaining."
                  % (used if used is not None else "?", limit, remaining))
            if int(remaining) == 0:
                print("[deactivate] that was the last one for this licence year. Further moves need")
                print("             support: https://veizik.com  (we do move them, it is just manual).")
        else:
            print("[deactivate] device changes: the server did not report a count. The published")
            print("             limit is %d per licence year." % _DEVICE_RELEASE_LIMIT)
        if res.get("registered_devices") is not None:
            print("[deactivate] devices still registered: %s" % res["registered_devices"])
    else:
        print("\n[deactivate] server release did NOT happen: %s" % res)
        print("[deactivate] the seat is still held server-side, so it still counts against your")
        if args.local_only:
            print("             registered devices. Free it with:  veizik deactivate   (no --local-only)")
        else:
            print("             registered devices. Re-run this command once you are online, or ask")
            print("             https://veizik.com to clear it.")
        print("[deactivate] published limit: %d device changes per licence year." % _DEVICE_RELEASE_LIMIT)

    # 2. local state: what the user asked for, regardless of what the server said.
    dropped = ve.logout()
    print("\n[deactivate] local session %s" % ("removed" if dropped else "was not present"))
    if args.forget_device:
        try:
            import veizik_session as vs
            os.remove(vs.DEVICE_FILE)
            print("[deactivate] device id forgotten — this machine will register as a NEW device.")
            print("             (That consumes another device slot if you re-activate here.)")
        except OSError as e:
            print("[deactivate] device id not removed (%s)" % e)
    else:
        print("[deactivate] device id kept, so re-activating here reuses the same slot.")
        print("             Use --forget-device only if you are retiring this machine.")
    print("\n[deactivate] this machine is now on the free path:  veizik status")
    return 0


# ---------------------------------------------------------------------------------------------------
#   veizik update — update the installation.
#
#   The installer (web/install.sh) is the single update mechanism: it pins a git tag, installs the
#   locked deps and checksums the engine. Re-running it IS the update, so this command's job is to
#   report where you are, hand you that exact line, and refuse to run it behind your back unless you
#   asked with --yes. After any update the signed packs should be re-verified — a partial update
#   leaves them intact-but-stale, which `pack verify` is designed to catch.
# ---------------------------------------------------------------------------------------------------
def _latest_release_tag():
    """Newest release tag in the distribution repo, or (None, reason). Network, best-effort."""
    if not _run_out(["git", "--version"], timeout=5):
        return None, "git is not installed, so the tag list cannot be read"
    out = _run_out(["git", "ls-remote", "--tags", "--refs", _VZ_REPO], timeout=25)
    if out is None:
        return None, "could not reach %s" % _VZ_REPO
    tags = []
    for line in out.splitlines():
        parts = line.split("refs/tags/")
        if len(parts) == 2 and parts[1].strip():
            tags.append(parts[1].strip())
    if not tags:
        return None, "the repository has no release tags"

    def _key(t):
        nums = []
        for chunk in t.lstrip("v").replace("-", ".").split("."):
            nums.append(int(chunk) if chunk.isdigit() else -1)
        return nums
    return sorted(tags, key=_key)[-1], None


def cmd_update(args):
    _banner()
    version, source = _app_version()
    print("\n[update] installed version   %s" % version)
    print("[update] version source      %s" % source)
    print("[update] install root        %s%s"
          % (_VZ_HOME, "" if os.path.isdir(_VZ_HOME) else "   (not present — running from a checkout?)"))
    print("[update] running from        %s" % _HERE)
    print("[update] python              %s" % sys.executable)

    if args.check or args.yes:
        latest, why = _latest_release_tag()
        if latest:
            print("[update] latest release tag  %s" % latest)
            if version in ("unknown",):
                print("[update] cannot compare: this install does not report a version.")
            elif latest.lstrip("v") == version.lstrip("v"):
                print("[update] you are on the latest release.")
            else:
                print("[update] an update is available: %s -> %s" % (version, latest))
        else:
            print("[update] latest release tag  unknown (%s)" % why)

    cmd = "curl -fsSL %s | sh" % _INSTALL_URL
    print("\n[update] the installer IS the updater — it re-pins the tag, reinstalls the locked")
    print("         dependencies and re-checksums the engine:")
    print("\n    %s\n" % cmd)
    # env goes on the RIGHT-hand sh: that is the process the installer actually runs in.
    print("[update] pin a specific version instead:   curl -fsSL %s | VZ_VERSION=v0.2.0 sh"
          % _INSTALL_URL)

    if not args.yes:
        print("\n[update] not run. veizik does not execute a network install script on your behalf")
        print("         unless you ask:   veizik update --yes")
    else:
        if not _run_out(["curl", "--version"], timeout=5):
            print("\n[update] curl is not installed — run the line above with your own downloader.")
            return 1
        print("\n[update] running the installer (--yes given)...")
        sys.stdout.flush()      # the child writes straight to the tty; flush so the order survives a pipe
        try:
            rc = subprocess.call(["sh", "-c", cmd])
        except Exception as e:
            print("[update] installer could not be launched (%s)" % e)
            return 1
        if rc != 0:
            print("\n[update] installer exited rc=%d — your existing install is untouched." % rc)
            return rc
        print("\n[update] installer finished.")

    print("\n[update] after ANY update, re-verify the signed packs: an interrupted or partial update")
    print("         leaves them on disk looking fine while no longer matching what veizik signed.")
    print("\n    veizik pack verify\n")
    if args.yes:
        print("[update] running it now...")
        try:
            return cmd_pack(argparse.Namespace(pack_action="verify"))
        except Exception as e:
            print("[update] pack verify could not run (%s: %s) — run it yourself." % (type(e).__name__, e))
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

    # setup — first-run onboarding: hardware summary -> two-screen consent -> key -> next command.
    p_setup = sub.add_parser("setup", help="first-run onboarding: check this machine, consent "
                             "screens, how to get a key, what to type next")
    p_setup.add_argument("--force", action="store_true",
                         help="run the wizard again and re-ask the consent screens")
    p_setup.set_defaults(func=cmd_setup)

    # benchmark — measure what is measurable on this box; never invent a render time.
    p_bench = sub.add_parser("benchmark", help="this machine's execution profile: hardware probe, "
                             "support tier, GEMM/bandwidth micro-benchmark, run-report JSON")
    p_bench.add_argument("--out", default="", help="report path (default ~/.veizik/benchmark-<ts>.json)")
    p_bench.add_argument("--no-micro", action="store_true", dest="no_micro",
                         help="skip the GEMM/bandwidth micro-benchmark (hardware probe only)")
    p_bench.add_argument("--device", default="", choices=["", "cuda", "mps", "cpu"],
                         help="force the micro-benchmark device (default: best available)")
    p_bench.set_defaults(func=cmd_benchmark)

    # deactivate — release this machine's registration (server-side seat + local session).
    p_deact = sub.add_parser("deactivate", help="release THIS machine's registration so another "
                             "machine can be activated")
    p_deact.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p_deact.add_argument("--local-only", action="store_true", dest="local_only",
                         help="clear local state only; do NOT ask the server to free the seat")
    p_deact.add_argument("--forget-device", action="store_true", dest="forget_device",
                         help="also discard the device id (this machine re-registers as new — only "
                              "for a machine you are retiring)")
    p_deact.set_defaults(func=cmd_deactivate)

    # update — report the installed version and hand over the installer line.
    p_upd = sub.add_parser("update", help="show the installed version and how to update "
                           "(runs the installer only with --yes)")
    p_upd.add_argument("--check", action="store_true", help="also look up the latest release tag")
    p_upd.add_argument("--yes", action="store_true",
                       help="actually run the installer, then re-verify the signed packs")
    p_upd.set_defaults(func=cmd_update)

    # license: activate / inspect / clear an API key (unlocks your tier; free without one)
    p_login = sub.add_parser("login", help="activate a veizik API key (unlocks your paid tier)")
    p_login.add_argument("api_key", help="your veizik API key (vzk_live_...) from veizik.com")
    p_login.set_defaults(func=cmd_login)
    p_status = sub.add_parser("status", help="show current license tier + entitlement")
    p_status.set_defaults(func=cmd_status)
    p_logout = sub.add_parser("logout", help="remove the stored license session (revert to Free)")
    p_logout.set_defaults(func=cmd_logout)

    p_verify = sub.add_parser("verify", help="prove you own the license email (binds payments to this key)")
    p_verify.add_argument("--email", help="override the email (defaults to your logged-in license email)")
    p_verify.set_defaults(func=cmd_verify)
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

    # §14 Pro Preview commands — registered so typing one gets an honest status answer instead of
    # "invalid choice". They intentionally do NOT run anything; see cmd_pro_preview.
    for _pv in _PRO_PREVIEW_CMDS:
        _p = sub.add_parser(_pv, help="Pro Preview: %s — NOT in the public download today"
                            % _FEATURES.get(_pv, ("", [], _pv, ""))[2])
        _p.add_argument("rest", nargs=argparse.REMAINDER,
                        help="accepted and ignored; the command does not execute in this build")
        _p.set_defaults(func=cmd_pro_preview, preview_feature=_pv)

    # pack — signed runtime packs: one public binary + entitlement + per-tier pack.
    # Every install path runs through an Ed25519 signature check against the public key embedded
    # in this client; there is deliberately no --skip-verify / --force / --insecure flag to add.
    p_pack = sub.add_parser("pack", help="signed runtime packs: list/install/verify/status "
                            "(what your tier unlocks, and proof it is what we signed)")
    psub = p_pack.add_subparsers(dest="pack_action")
    psub.add_parser("list", help="packs in the manifest, their signature state, and your access")
    p_pi = psub.add_parser("install", help="install the packs your tier entitles you to (verified)")
    p_pi.add_argument("pack_id", nargs="?", default=None,
                      help="pack to install; omit to install every pack your tier allows")
    p_pi.add_argument("--dry-run", action="store_true", dest="dry_run",
                      help="verify signature + entitlement, then stop before downloading")
    psub.add_parser("verify", help="re-verify every installed pack: file contents + signature")
    psub.add_parser("status", help="installed packs and the features they unlock")
    p_pack.set_defaults(func=cmd_pack, pack_action=None)

    # telemetry — the OPTIONAL performance/compatibility channel only
    p_tel = sub.add_parser("telemetry", help="optional performance data: status/benefits/contributor/"
                           "consent/enable/disable/show-last/queue/send/export/delete")
    tsub = p_tel.add_subparsers(dest="telemetry_action")
    tsub.add_parser("status", help="consent version/time, pending count, exactly what is and is not collected")
    tsub.add_parser("benefits", help="what sharing performance data gets you (§6) — benefits, never unlocks")
    tsub.add_parser("contributor", help="your Telemetry Contributor report: this machine's hardware, "
                                        "runs and stability")
    tsub.add_parser("consent", help="re-show the two-step consent screens and change your answer")
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
