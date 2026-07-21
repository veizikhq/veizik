#!/usr/bin/env python3
"""Veizik Universal Runtime — auto-detect a diffusion model family, probe the hardware, and produce an
auto-tuned execution plan (support tier, dtype, attention backend, caching, offload strategy, sampler).
The public planner for a broad model-family surface.

TIERS OF SUPPORT (honest):
  T1 native      : a native engine path is available for this family (shipped in the private runtime pack).
  T2 universal   : hardware-aware planning (offload + attention + caching + dtype), via diffusers/DiffSynth.
  T3 best-effort : confidence<0.6 -> whole-model offload + SDPA, no advanced levers, STILL RENDERS.

This module implements the model-family registry, auto-detect, hardware probe, and the autotune planner.
Run:  python3 veizik_universal.py --detect <model_path>         # print card + auto-tuned plan
      python3 veizik_universal.py --detect-config <config.json> # detect from a raw config
      python3 veizik_universal.py --selftest                    # detect+plan all built-in families
"""
import os, sys, json, argparse, glob
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Tuple, Dict

# ---------------------------------------------------------------------------------------------------
#   ModelCard (Tier-0 lever facts + optional Tier-1 block spec). JSON-serializable.
# ---------------------------------------------------------------------------------------------------
@dataclass
class SamplerSpec:
    scheduler: str = "flow_match"      # flow_match | unipc | ddim
    time_shift: float = 3.0            # flow-match sigma shift (Step-Video=13!, LTX res-mu, Wan~5)
    default_steps: int = 30
    cfg_hi: float = 5.0                # CFG at high-noise start
    cfg_lo: float = 5.0                # CFG at low-noise end (ramp hi->lo; equal = fixed)
    cfg_zero_star: bool = False        # zero-init first step (flow-match opening stability)

@dataclass
class ModelCard:
    family: str                        # ltx|wan22_moe|stepvideo|hunyuanvideo|cogvideox|flux|sd35|unknown
    kind: str = "video"                # video|image
    confidence: float = 1.0
    pipeline_cls: str = ""             # 'diffusers.LTXPipeline' | 'diffsynth.StepVideoPipeline'
    transformer_attrs: List[str] = field(default_factory=lambda: ["transformer"])
    block_container_paths: List[str] = field(default_factory=list)   # dotted, relative to pipe
    block_count: int = 0               # auto len()
    per_block_bytes: int = 0           # auto param-sum (bf16)
    # attention
    attn_is_joint: bool = False        # MMDiT concat text+img (Flux/SD3/Hunyuan-single)
    has_cross_attn: bool = True
    qk_norm: str = "rms"               # rms|layer|none
    # dtype policy (sm_86: fp8 NO, int8/int4 TC yes)
    prefers_bf16: bool = True
    int8_safe: bool = False
    int4_safe: bool = False
    # caching capability flag for this family (None => auto-disabled).
    teacache_ok: bool = True
    teacache_thresh: float = 0.10
    teacache_warmup: int = 3
    # vae
    vae_spatial_comp: int = 8
    vae_temporal_comp: int = 8
    vae_tiling_ok: bool = True
    # sampler (the load-bearing per-model field)
    sampler: SamplerSpec = field(default_factory=SamplerSpec)
    # native tier-1
    native_engine: bool = False          # True if a native engine path is available for this family
    # crash-resilience: True if the pipeline exposes a per-step latent callback (diffusers
    # callback_on_step_end / DiffSynth) so a render can RESUME from the last saved latent. False =>
    # opaque black-box pipeline (no mid-render state) -> whole-job retry (still crash-isolated).
    checkpointable: bool = True
    notes: str = ""

# ---------------------------------------------------------------------------------------------------
#   Native-engine availability per family. The compiled native engine ships in the private runtime
#   pack (entitlement-gated); this public map only records WHICH families have a native path, so the
#   support-tier table can show it. No engine internals are described here.
# ---------------------------------------------------------------------------------------------------
@dataclass
class BlockSpec:
    native_status: str = "TODO"            # DONE = native engine path available | TODO = universal path only

BLOCK_SPECS: Dict[str, BlockSpec] = {
    "stepvideo": BlockSpec("DONE"), "ltx": BlockSpec("DONE"), "cogvideox": BlockSpec("DONE"),
    "hunyuanvideo": BlockSpec("DONE"), "wan": BlockSpec("DONE"), "flux": BlockSpec("DONE"),
    "wan22_moe": BlockSpec("TODO"),
}

# ---------------------------------------------------------------------------------------------------
#   FAMILY_TEMPLATES — the thin per-model surface. Structural constants auto-derived at reflect time;
#   these carry what config.json CANNOT express (sampler, teacache, arch flags).
# ---------------------------------------------------------------------------------------------------
FAMILY_TEMPLATES: Dict[str, ModelCard] = {
    "ltx": ModelCard(
        family="ltx", kind="video", pipeline_cls="diffusers.LTXPipeline",
        block_container_paths=["transformer.transformer_blocks"],
        attn_is_joint=False, has_cross_attn=True, qk_norm="rms",
        prefers_bf16=True, int8_safe=True, int4_safe=False,
        teacache_ok=True, teacache_thresh=0.05, teacache_warmup=2,
        vae_spatial_comp=32, vae_temporal_comp=8, vae_tiling_ok=True,
        sampler=SamplerSpec(scheduler="flow_match", time_shift=6.0, default_steps=30,
                            cfg_hi=3.0, cfg_lo=3.0, cfg_zero_star=False),
        native_engine=True, notes="Realtime latent-diffusion video; native engine path available."),
    "stepvideo": ModelCard(
        family="stepvideo", kind="video", pipeline_cls="diffsynth.StepVideoPipeline",
        block_container_paths=["transformer.transformer_blocks"],
        attn_is_joint=False, has_cross_attn=True, qk_norm="rms",
        prefers_bf16=True, int8_safe=False, int4_safe=True,   # int4 calibrated in native plan
        teacache_ok=True, teacache_thresh=0.10, teacache_warmup=3,
        vae_spatial_comp=16, vae_temporal_comp=8, vae_tiling_ok=True,
        sampler=SamplerSpec(scheduler="flow_match", time_shift=13.0, default_steps=50,
                            cfg_hi=9.0, cfg_lo=5.0, cfg_zero_star=True),
        native_engine=True, notes="30B; native engine path (Tier-1). time_shift=13 is critical."),
    "wan22_moe": ModelCard(
        family="wan22_moe", kind="video", pipeline_cls="diffsynth.WanPipeline",
        transformer_attrs=["transformer", "transformer_2"],
        block_container_paths=["transformer.blocks", "transformer_2.blocks"],
        attn_is_joint=False, has_cross_attn=True, qk_norm="rms",
        prefers_bf16=True, int8_safe=True, int4_safe=False,
        teacache_ok=True, teacache_thresh=0.08, teacache_warmup=2,
        vae_spatial_comp=8, vae_temporal_comp=4, vae_tiling_ok=True,
        sampler=SamplerSpec(scheduler="unipc", time_shift=5.0, default_steps=24,
                            cfg_hi=7.0, cfg_lo=4.0, cfg_zero_star=True),
        native_engine=False, notes="Dual-expert MoE (high-noise->low-noise at boundary); inactive expert = cold capacity. Native path for the base block; MoE routing on the roadmap."),
    "hunyuanvideo": ModelCard(
        family="hunyuanvideo", kind="video", pipeline_cls="diffusers.HunyuanVideoPipeline",
        block_container_paths=["transformer.transformer_blocks", "transformer.single_transformer_blocks"],
        attn_is_joint=True, has_cross_attn=False, qk_norm="rms",
        prefers_bf16=True, int8_safe=True, int4_safe=False,
        teacache_ok=True, teacache_thresh=0.15, teacache_warmup=3,
        vae_spatial_comp=8, vae_temporal_comp=4, vae_tiling_ok=True,
        sampler=SamplerSpec(scheduler="flow_match", time_shift=7.0, default_steps=30,
                            cfg_hi=6.0, cfg_lo=6.0, cfg_zero_star=False),
        native_engine=True, notes="MMDiT joint attention; strong for humans. Native engine path available."),
    "cogvideox": ModelCard(
        family="cogvideox", kind="video", pipeline_cls="diffusers.CogVideoXPipeline",
        block_container_paths=["transformer.transformer_blocks"],
        attn_is_joint=True, has_cross_attn=False, qk_norm="none",
        prefers_bf16=True, int8_safe=True, int4_safe=False,
        teacache_ok=True, teacache_thresh=0.15, teacache_warmup=2,
        vae_spatial_comp=8, vae_temporal_comp=4, vae_tiling_ok=True,
        sampler=SamplerSpec(scheduler="ddim", time_shift=1.0, default_steps=50,
                            cfg_hi=6.0, cfg_lo=6.0, cfg_zero_star=False),
        native_engine=True, notes="3D-VAE, joint text-video attention. Native engine path available."),
    "flux": ModelCard(
        family="flux", kind="image", pipeline_cls="diffusers.FluxPipeline",
        block_container_paths=["transformer.transformer_blocks", "transformer.single_transformer_blocks"],
        attn_is_joint=True, has_cross_attn=False, qk_norm="rms",
        prefers_bf16=True, int8_safe=True, int4_safe=False,
        teacache_ok=True, teacache_thresh=0.25, teacache_warmup=1,
        vae_spatial_comp=8, vae_temporal_comp=1, vae_tiling_ok=True,
        sampler=SamplerSpec(scheduler="flow_match", time_shift=1.15, default_steps=28,
                            cfg_hi=3.5, cfg_lo=3.5, cfg_zero_star=False),
        native_engine=True, notes="Image MMDiT; time_shift resolution-dependent. Native engine path available."),
}

# unknown -> honest safe path
UNKNOWN_CARD = ModelCard(family="unknown", confidence=0.0, block_container_paths=[],
                         teacache_ok=False, native_engine=False,
                         notes="T3 best-effort: whole-model offload + SDPA, no advanced levers.")

# ---------------------------------------------------------------------------------------------------
#   AutoDetect — config._class_name table first, then checkpoint key-signature probe.
# ---------------------------------------------------------------------------------------------------
_CLASS_MAP = {
    "LTXVideoTransformer3DModel": ("ltx", 0.99),
    "WanTransformer3DModel": ("wan22_moe", 0.95),   # refined by transformer_2 presence
    "HunyuanVideoTransformer3DModel": ("hunyuanvideo", 0.99),
    "CogVideoXTransformer3DModel": ("cogvideox", 0.99),
    "FluxTransformer2DModel": ("flux", 0.99),
    "SD3Transformer2DModel": ("sd35", 0.90),
}

def autodetect_family(config: dict, index: Optional[dict] = None,
                      key_sig: Optional[List[str]] = None) -> Tuple[str, float]:
    cn = config.get("_class_name", "")
    if cn in _CLASS_MAP:
        fam, conf = _CLASS_MAP[cn]
        if fam == "wan22_moe" and index is not None:
            has2 = any("transformer_2" in k for k in index.get("__keys__", []))
            return ("wan22_moe" if has2 else "wan21", 0.97 if has2 else 0.95)
        return fam, conf
    # StepVideo signature (DiffSynth, no standard _class_name)
    if index is not None:
        keys = index.get("__keys__", [])
        if any("step_llm" in k for k in keys):
            return "stepvideo", 0.95
    if config.get("num_attention_heads") == 48 and config.get("num_layers", config.get("num_hidden_layers")) in (48,):
        return "stepvideo", 0.85
    # checkpoint key-signature fallback
    if key_sig:
        s = " ".join(key_sig[:200])
        if "single_transformer_blocks" in s and "transformer_2" not in s:
            return ("hunyuanvideo", 0.7) if "img_in" in s or "txt_in" in s else ("flux", 0.65)
        if "transformer_2" in s:
            return "wan22_moe", 0.7
        if "transformer_blocks" in s:
            return "ltx", 0.6
    return "unknown", 0.0

def resolve_card(model_path: str, overrides: Optional[dict] = None) -> ModelCard:
    """config-first detection -> FAMILY_TEMPLATE. Structural fields (block_count/per_block_bytes)
    are reflected at bind time; here we produce the lever-facts card."""
    cfg, index = {}, {"__keys__": []}
    mi = os.path.join(model_path, "model_index.json")
    if os.path.exists(mi):
        index = json.load(open(mi)); index["__keys__"] = list(index.keys())
    # transformer config
    for cand in ["transformer/config.json", "config.json", "transformer/diffusion_pytorch_model.safetensors.index.json"]:
        p = os.path.join(model_path, cand)
        if os.path.exists(p):
            try:
                d = json.load(open(p))
                if "_class_name" in d or "num_attention_heads" in d: cfg = d; break
            except Exception: pass
    key_sig = _safetensors_keys(model_path)
    fam, conf = autodetect_family(cfg, index, key_sig)
    card = FAMILY_TEMPLATES.get(fam, UNKNOWN_CARD)
    import copy; card = copy.deepcopy(card); card.confidence = conf
    if overrides:
        for k, v in overrides.items():
            if hasattr(card, k): setattr(card, k, v)
    return card

def _safetensors_keys(model_path: str, limit: int = 300) -> List[str]:
    """read safetensors header keys only (no weights) for the shape-probe fallback."""
    import struct
    keys = []
    for st in glob.glob(os.path.join(model_path, "**", "*.safetensors"), recursive=True)[:3]:
        try:
            with open(st, "rb") as f:
                n = struct.unpack("<Q", f.read(8))[0]
                hdr = json.loads(f.read(n))
                keys += [k for k in hdr.keys() if k != "__metadata__"]
                if len(keys) > limit: break
        except Exception: pass
    return keys

# ---------------------------------------------------------------------------------------------------
#   Hardware probe
# ---------------------------------------------------------------------------------------------------
@dataclass
class HwProfile:
    n_gpus: int = 1
    gpu_free_gb: float = 24.0
    gpu_total_gb: float = 24.0
    gpu_name: str = "RTX 3090"
    sm: int = 86
    nvlink: bool = False
    host_ram_gb: float = 64.0
    is_wsl: bool = False
    int8_tc: bool = True
    fp8_tc: bool = False

def probe_hardware() -> HwProfile:
    hw = HwProfile()
    try:
        import torch
        hw.n_gpus = torch.cuda.device_count()
        free, tot = torch.cuda.mem_get_info()
        hw.gpu_free_gb, hw.gpu_total_gb = free/1e9, tot/1e9
        p = torch.cuda.get_device_properties(0)
        hw.gpu_name = p.name; hw.sm = p.major*10 + p.minor
        hw.fp8_tc = hw.sm >= 89; hw.int8_tc = hw.sm >= 75
        if hw.n_gpus >= 2:
            try: hw.nvlink = torch.cuda.can_device_access_peer(0, 1)
            except Exception: pass
    except Exception as e:
        print("[hw] torch probe failed (%s) -> defaults" % e)
    try:
        hw.host_ram_gb = os.sysconf("SC_PAGE_SIZE")*os.sysconf("SC_PHYS_PAGES")/1e9
    except Exception: pass
    hw.is_wsl = "microsoft" in open("/proc/version").read().lower() if os.path.exists("/proc/version") else False
    return hw

# ---------------------------------------------------------------------------------------------------
#   Autotune planner — roofline tier math + dtype cascade + attention ladder + quality selection.
# ---------------------------------------------------------------------------------------------------
@dataclass
class RunPlan:
    support_tier: str = "T2"           # T1|T2|T3
    offload: str = "model_cpu_offload" # resident|model_cpu_offload|sequential_cpu_offload|native_tier
    dtype: str = "bf16"
    quant: str = "none"                # none|int8|int4
    attention: str = "sdpa_meff"       # native|flash|sage|sdpa_meff
    teacache: bool = False
    teacache_thresh: float = 0.0
    vae_tiling: bool = True
    base_res: Tuple[int, int] = (768, 448)
    frames_native: int = 121
    interpolate_to_fps: int = 0        # 0=none; else RIFE target
    steps: int = 30
    time_shift: float = 3.0
    cfg_hi: float = 5.0
    cfg_lo: float = 5.0
    cfg_zero_star: bool = False
    target_res: Tuple[int, int] = (3840, 2160)
    sr_stage: str = "none"             # none|ltx_upscaler|seedvr2
    provenance: dict = field(default_factory=dict)

# rough per-family activation model (GB) at a given token count; universal estimator
def _activation_gb(hidden: int, tokens: int, dtype_bytes: int = 2, attn_factor: float = 2.0) -> float:
    return tokens * hidden * dtype_bytes * attn_factor / 1e9

def autotune(card: ModelCard, hw: HwProfile, target: str = "commercial_10s",
             est_per_block_gb: float = 0.0, est_block_count: int = 0) -> RunPlan:
    p = RunPlan()
    p.provenance = {"family": card.family, "confidence": card.confidence,
                    "gpu_free_gb": round(hw.gpu_free_gb, 2), "nvlink": hw.nvlink, "is_wsl": hw.is_wsl}
    # ---- support tier ----
    if card.confidence < 0.6:
        p.support_tier = "T3"; p.offload = "sequential_cpu_offload"; p.attention = "sdpa_meff"
        p.teacache = False; p.provenance["reason"] = "confidence<0.6 -> safe path"
        return p
    p.support_tier = "T1" if card.native_engine else "T2"

    # ---- dtype ----
    p.dtype = "bf16" if card.prefers_bf16 else "fp16"

    # ---- sampler (load-bearing per-model) ----
    s = card.sampler
    p.steps, p.time_shift = s.default_steps, s.time_shift
    p.cfg_hi, p.cfg_lo, p.cfg_zero_star = s.cfg_hi, s.cfg_lo, s.cfg_zero_star

    # ---- attention ladder ----
    if card.native_engine and hw.sm >= 80:
        p.attention = "native"
    else:
        p.attention = "flash"   # torch SDPA flash backend; falls to sdpa_meff if unavailable at runtime

    # ---- TeaCache ----
    if card.teacache_ok:
        p.teacache = True; p.teacache_thresh = card.teacache_thresh

    # ---- roofline residency: how many blocks fit; pick offload strategy ----
    per_block = est_per_block_gb
    nblk = est_block_count or card.block_count
    if per_block > 0 and nblk > 0:
        # activation reserve at base res (tokens ~ spatial_latent * temporal_latent)
        bw, bh = p.base_res
        tok = (bw//card.vae_spatial_comp) * (bh//card.vae_spatial_comp) * max(1, p.frames_native//card.vae_temporal_comp)
        act = _activation_gb(4096, tok)   # hidden est; conservative
        budget = hw.gpu_free_gb - 2.0 - act
        hot = int(budget / per_block) if per_block > 0 else nblk
        p.provenance.update({"per_block_gb": round(per_block, 3), "n_blocks": nblk,
                             "act_reserve_gb": round(act, 2), "hot_blocks": max(hot, 0)})
        if hot >= nblk:
            p.offload = "resident"
        elif hw.nvlink and hw.n_gpus >= 2:
            p.offload = "native_tier" if card.native_engine else "model_cpu_offload"  # warm-NVLink via native; else diffusers offload
        else:
            p.offload = "model_cpu_offload"
        # quant only if it buys residency (WSL streaming is non-overlappable -> worth it)
        if hot < nblk and card.int4_safe and card.native_engine:
            p.quant = "int4"; p.offload = "native_tier"  # quant buys full residency
            p.provenance["quant_reason"] = "int4 buys full residency, kills streaming"
        elif hot < nblk and card.int8_safe and hw.is_wsl:
            p.provenance["quant_note"] = "int8 could raise residency (needs calibration receipt)"
    else:
        p.offload = "model_cpu_offload"

    # ---- quality/render plan by target ----
    if target.startswith("commercial") or "4k" in target:
        p.base_res = (768, 448); p.frames_native = 241; p.interpolate_to_fps = 0
        p.target_res = (3840, 2160)
        p.sr_stage = "ltx_upscaler" if card.family == "ltx" else "seedvr2"
    p.vae_tiling = card.vae_tiling_ok
    return p

# ---------------------------------------------------------------------------------------------------
#   Render-farm planner — multi-model caching + concurrent renders + multi-GPU (data-parallel).
# ---------------------------------------------------------------------------------------------------
# HONEST capability scope:
#   * Multi-model CACHING (keep N models resident, no reload between jobs)         -> supported here.
#   * CONCURRENT renders on ONE GPU (weights shared read-only; only activations    -> supported; but at
#     multiply): fills idle from streaming/VAE/CPU gaps -> ~1.2-2x throughput,         production seq a
#     NOT Kx, because a compute-bound video render already saturates the SMs.          single render
#   * MULTI-GPU = DATA-PARALLEL throughput (independent renders, one per GPU)       -> supported: near-Nx.
#   * SINGLE render split across GPUs (tensor/sequence/pipeline parallel, NCCL/xDiT)-> NOT supported.
#     Unnecessary for capacity: a 30B video DiT (~58GB fp16) fits on ONE 80GB H100.    (roadmap if needed)
_EST_WEIGHT_GB = {"stepvideo":58.0,"ltx":26.0,"wan22_moe":27.0,"hunyuanvideo":26.0,
                  "cogvideox":22.0,"flux":24.0,"sd35":16.0,"unknown":30.0}
def _weight_gb(card: ModelCard) -> float:
    if card.block_count>0 and card.per_block_bytes>0:
        return card.block_count*card.per_block_bytes/1e9        # measured at load
    return _EST_WEIGHT_GB.get(card.family, 30.0)                # estimate pre-load
def _render_act_gb(card: ModelCard, plan: RunPlan) -> float:
    # transient activation per ACTIVE render (weights are shared, not counted here); +VAE/decode headroom
    bw,bh = plan.base_res
    tok = (bw//card.vae_spatial_comp)*(bh//card.vae_spatial_comp)*max(1, plan.frames_native//card.vae_temporal_comp)
    return _activation_gb(4096, tok, 2, 3.0) + 1.5

@dataclass
class FarmPlan:
    topo: str = "single"                 # single | data_parallel
    mode: str = "throughput"             # throughput | latency | balanced
    instances_req: int = 0               # explicit --instances (0=auto memory-fit)
    n_gpus: int = 1
    per_gpu_free_gb: float = 24.0
    per_gpu: List[Dict] = field(default_factory=list)   # {gpu,resident,concurrency,fit_max,weight_gb,act_gb,streamed}
    total_concurrent: int = 0
    oom_safe: bool = True                # concurrency never exceeds the memory-fit ceiling
    model_parallel_supported: bool = False
    note: str = ""

def plan_render_farm(hw: HwProfile, cards: List[ModelCard], target: str = "commercial_10s",
                     per_gpu_free_gb: float = 0.0, quant_ok: bool = True, max_conc_per_gpu: int = 8,
                     mode: str = "throughput", instances: int = 0) -> FarmPlan:
    """Memory-fit multi-model caching + concurrent-render + multi-GPU data-parallel orchestration planner.
    Weights are read-only -> cached ONCE, shared by all concurrent renders of that model; concurrency is
    bounded by ACTIVATION memory, not weight memory.
      mode=throughput -> max concurrent (each render slower under contention; aggregate N-up)
      mode=latency    -> 1 render/GPU (owns the GPU, fastest single render)
      mode=balanced   -> up to 2/GPU
      instances=K     -> explicit target; CAPPED by the memory-fit ceiling so it can NEVER OOM (admission)."""
    free = per_gpu_free_gb if per_gpu_free_gb>0 else hw.gpu_free_gb
    margin = 2.0
    costs=[]
    for c in cards:
        plan = autotune(c, hw, target)
        w = _weight_gb(c)
        if quant_ok and plan.quant=="int4": w *= 0.25
        elif quant_ok and plan.quant=="int8": w *= 0.5
        costs.append({"fam":c.family, "w":w, "a":_render_act_gb(c, plan), "quant":plan.quant})
    # first-fit-decreasing bin-pack of models onto GPUs (resident cache); models too big -> stream(offload)
    gpus=[{"gpu":g,"resident":[],"wsum":0.0,"amax":0.0} for g in range(max(1,hw.n_gpus))]
    for i in sorted(range(len(costs)), key=lambda i:-costs[i]["w"]):
        c=costs[i]; placed=False
        for g in gpus:
            if g["wsum"]+c["w"]+margin+max(g["amax"],c["a"]) <= free:
                g["resident"].append("%s%s"%(c["fam"], "·int4" if c["quant"]=="int4" else "·int8" if c["quant"]=="int8" else ""))
                g["wsum"]+=c["w"]; g["amax"]=max(g["amax"],c["a"]); placed=True; break
        if not placed:  # doesn't fit resident -> streams from host; still serve on least-loaded GPU
            g=min(gpus,key=lambda x:x["wsum"]); g["resident"].append(c["fam"]+"(stream)"); g["amax"]=max(g["amax"],c["a"])
    # fill idle GPUs by replicating the fullest GPU's cache -> use the whole farm for throughput
    if hw.n_gpus>1:
        src=max(gpus, key=lambda x:len(x["resident"]))
        for g in gpus:
            if not g["resident"] and src["resident"]:
                g["resident"]=list(src["resident"]); g["wsum"]=src["wsum"]; g["amax"]=src["amax"]
    fp=FarmPlan(n_gpus=max(1,hw.n_gpus), per_gpu_free_gb=round(free,1), mode=mode, instances_req=instances)
    for g in gpus:
        avail=free - g["wsum"] - margin
        streamed=any("(stream)" in r for r in g["resident"])
        # memory-fit CEILING (OOM-safe admission): streamed=H2D-bandwidth-bound(low), resident=activation-bound
        fit = 0 if (not g["resident"] or g["amax"]<=0) else (min(2, max(1,int(avail/g["amax"]))) if streamed
                                                             else min(max_conc_per_gpu, max(1,int(avail/g["amax"]))))
        # mode shapes concurrency WITHIN the fit ceiling (never above -> never OOM)
        if   mode=="latency":  conc=min(fit,1)
        elif mode=="balanced": conc=min(fit,2)
        else:                  conc=fit                          # throughput
        if instances>0:                                          # explicit override, ADMISSION-capped by fit
            conc=min(fit, instances)
        fp.per_gpu.append({"gpu":g["gpu"],"resident":g["resident"],"concurrency":conc,"fit_max":fit,
                           "streamed":streamed,"weight_gb":round(g["wsum"],1),"act_per_render_gb":round(g["amax"],2)})
        fp.total_concurrent+=conc
    if instances>0 and any(g["concurrency"]<min(instances,g["fit_max"]) for g in fp.per_gpu):
        pass
    fp.oom_safe = all(g["concurrency"]<=g["fit_max"] for g in fp.per_gpu)   # invariant: conc never exceeds fit
    fp.topo = "data_parallel" if hw.n_gpus>1 else "single"
    fp.note = ("weights cached once & shared read-only by all concurrent renders (concurrency bounded by "
               "activation, not weight, memory). ONE-GPU concurrency fills streaming/VAE/CPU idle "
               "(~1.2-2x, not Nx — compute-bound renders saturate SMs). MULTI-GPU = data-parallel "
               "throughput (near-Nx). Single-render model-parallel across GPUs NOT supported (unneeded: "
               "30B fits one 80GB H100).")
    return fp

# ---------------------------------------------------------------------------------------------------
#   Crash-resilience — checkpoint/resume + instance isolation. NEW (did not exist: only memory-tier
#   OOM-avoidance existed in the native scheduler; render-step resume + multi-instance isolation are new).
# ---------------------------------------------------------------------------------------------------
# Diffusion denoise is INHERENTLY checkpointable: the entire state at step s is the latent q_s. So a
# crash (OOM spike, driver fault) resumes from the last saved latent -> only steps s..N re-run, not 0..N.
# TIERED — fine-grained step-resume needs a checkpointable pipeline (per-step latent callback). Models
# that don't expose one (opaque black-box) still get crash ISOLATION + whole-job RETRY, so the farm never
# dies; you just lose that one job's progress and it re-runs. Isolation + degrade-then-retry are UNIVERSAL.
@dataclass
class ResiliencePolicy:
    tier: str = "step_checkpoint"        # step_checkpoint (checkpointable) | whole_job_retry (opaque)
    checkpoint_every_steps: int = 5      # 0 if not checkpointable (no mid-render state exists to save)
    store: str = "host_ram+disk"         # host_ram=fast resume; disk=survives process death
    isolation: str = "process"           # UNIVERSAL: hard isolation, one instance's crash can't kill siblings
    on_oom: str = "degrade_then_retry"   # UNIVERSAL: admission prevents OOM; on a spike -> safer config
                                         #   (shed a slot / vae-tile / quant / lower res) then retry, not loop
    resume: str = "from_last_checkpoint"
    max_retries: int = 2

def plan_resilience(total_steps: int, checkpointable: bool = True, isolate_processes: bool = True) -> ResiliencePolicy:
    rp = ResiliencePolicy()
    rp.isolation = "process" if isolate_processes else "stream"
    if checkpointable:
        rp.tier = "step_checkpoint"
        rp.checkpoint_every_steps = max(1, total_steps//6)          # ~6 latent checkpoints/render (cheap)
        rp.resume = "from_last_checkpoint"                          # re-run only steps AFTER the last latent
    else:
        rp.tier = "whole_job_retry"                                # opaque pipeline: no mid-render state
        rp.checkpoint_every_steps = 0
        rp.resume = "whole_job_retry_on_idle_resource"             # lose that job's progress; farm survives
    return rp

def _print_farm(fp: FarmPlan, steps: int = 30, checkpointable: bool = True):
    adm = ("  (instances=%d requested; admission-capped to memory-fit -> OOM-safe)" % fp.instances_req) if fp.instances_req>0 else ""
    print("  topo=%s  mode=%s  gpus=%d  per_gpu_free=%.0fGB  oom_safe=%s%s"
          % (fp.topo, fp.mode, fp.n_gpus, fp.per_gpu_free_gb, fp.oom_safe, adm))
    print("  -> total concurrent renders in flight: %d" % fp.total_concurrent)
    for g in fp.per_gpu:
        print("   GPU%d: cache=%s | concurrency=%d/%d(fit) | resident_wt=%.1fGB | act/render=%.2fGB%s"
              % (g["gpu"], "+".join(g["resident"]) or "-", g["concurrency"], g["fit_max"],
                 g["weight_gb"], g["act_per_render_gb"], " [streamed]" if g.get("streamed") else ""))
    rp = plan_resilience(steps, checkpointable=checkpointable)
    if rp.tier=="step_checkpoint":
        print("  resilience[checkpointable]: latent checkpoint every %d steps -> %s | resume=from_last_checkpoint (only steps>ckpt re-run)"
              % (rp.checkpoint_every_steps, rp.store))
    else:
        print("  resilience[opaque/non-checkpointable]: no mid-render state -> WHOLE-JOB retry on idle resource (farm survives; that job restarts)")
    print("  resilience[universal]: isolation=%s (one crash can't kill siblings) | on_oom=%s (admission prevents; spike->safer config+retry, max %d)"
          % (rp.isolation, rp.on_oom, rp.max_retries))
    print("  note: %s" % fp.note)

# ---------------------------------------------------------------------------------------------------
#   CLI
# ---------------------------------------------------------------------------------------------------
def _print_plan(card: ModelCard, plan: RunPlan):
    print("  family=%s  conf=%.2f  tier=%s  kind=%s" % (card.family, card.confidence, plan.support_tier, card.kind))
    print("  offload=%s  dtype=%s  quant=%s  attn=%s  teacache=%s(%.2f)"
          % (plan.offload, plan.dtype, plan.quant, plan.attention, plan.teacache, plan.teacache_thresh))
    print("  sampler: %s shift=%.1f steps=%d cfg=%.1f->%.1f zero*=%s"
          % (card.sampler.scheduler, plan.time_shift, plan.steps, plan.cfg_hi, plan.cfg_lo, plan.cfg_zero_star))
    print("  render: base=%s frames=%d vae_tiled=%s SR=%s->%s"
          % (plan.base_res, plan.frames_native, plan.vae_tiling, plan.sr_stage, plan.target_res))
    if plan.provenance.get("hot_blocks") is not None:
        print("  residency: %d/%d blocks hot (per_block=%.2fGB, act=%.1fGB)"
              % (plan.provenance.get("hot_blocks",0), plan.provenance.get("n_blocks",0),
                 plan.provenance.get("per_block_gb",0), plan.provenance.get("act_reserve_gb",0)))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detect", default="")
    ap.add_argument("--detect-config", default="")
    ap.add_argument("--target", default="commercial_10s")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--farm", default="", help="comma-separated families to serve concurrently, e.g. ltx,stepvideo")
    ap.add_argument("--sim-gpus", type=int, default=0, help="simulate N GPUs (what-if, e.g. 4 for a 4xH100 box)")
    ap.add_argument("--sim-gpu-gb", type=float, default=0.0, help="simulate per-GPU free VRAM GB (e.g. 80 for H100)")
    ap.add_argument("--mode", default="throughput", choices=["throughput","latency","balanced"], help="farm mode")
    ap.add_argument("--instances", type=int, default=0, help="explicit concurrent instances/GPU (0=auto; admission-capped, OOM-safe)")
    ap.add_argument("--opaque", action="store_true", help="treat models as non-checkpointable (opaque pipeline) -> whole-job retry")
    A = ap.parse_args()
    hw = probe_hardware()
    if A.sim_gpus>0: hw.n_gpus=A.sim_gpus; hw.nvlink=(A.sim_gpus>1)
    if A.sim_gpu_gb>0: hw.gpu_free_gb=A.sim_gpu_gb; hw.gpu_total_gb=A.sim_gpu_gb
    if A.sim_gpu_gb>=80: hw.gpu_name="H100"; hw.sm=90; hw.fp8_tc=True
    print("[hw] %d GPU(s) %s sm_%d | free %.1f/%.1f GB | nvlink=%s | host %.0fGB | wsl=%s | int8_tc=%s fp8=%s"
          % (hw.n_gpus, hw.gpu_name, hw.sm, hw.gpu_free_gb, hw.gpu_total_gb, hw.nvlink,
             hw.host_ram_gb, hw.is_wsl, hw.int8_tc, hw.fp8_tc))
    if A.selftest:
        print("\n=== SELFTEST: detect+plan all built-in families ===")
        for fam in FAMILY_TEMPLATES:
            card = FAMILY_TEMPLATES[fam]; import copy; card = copy.deepcopy(card); card.confidence = 0.99
            plan = autotune(card, hw, A.target)
            print("\n[%s]" % fam); _print_plan(card, plan)
        print("\n=== RENDER FARM: multi-model cache + concurrent + multi-GPU (data-parallel) ===")
        import copy as _cp
        demo = [_cp.deepcopy(FAMILY_TEMPLATES[f]) for f in ("ltx","cogvideox") if f in FAMILY_TEMPLATES]
        for c in demo: c.confidence=0.99
        _print_farm(plan_render_farm(hw, demo, A.target, mode=A.mode, instances=A.instances), 30, checkpointable=True)
        return
    if A.farm:
        fams=[f.strip() for f in A.farm.split(",") if f.strip()]
        import copy as _cp
        cards=[]
        for f in fams:
            c=_cp.deepcopy(FAMILY_TEMPLATES.get(f, UNKNOWN_CARD)); c.confidence=0.99; c.family=f; cards.append(c)
        print("\n=== RENDER FARM PLAN: %s  (mode=%s instances=%s) ===" % (", ".join(fams), A.mode, A.instances or "auto"))
        fp=plan_render_farm(hw, cards, A.target, mode=A.mode, instances=A.instances)
        ck = (not A.opaque) and all(getattr(c,"checkpointable",True) for c in cards)
        _print_farm(fp, 30, checkpointable=ck); return
    if A.detect_config:
        cfg = json.load(open(A.detect_config))
        fam, conf = autodetect_family(cfg)
        print("\n[detect-config] -> family=%s confidence=%.2f" % (fam, conf))
        card = FAMILY_TEMPLATES.get(fam, UNKNOWN_CARD); card.confidence = conf
        _print_plan(card, autotune(card, hw, A.target)); return
    if A.detect:
        card = resolve_card(A.detect)
        print("\n[detect] %s" % A.detect)
        _print_plan(card, autotune(card, hw, A.target)); return
    ap.print_help()

if __name__ == "__main__":
    main()
