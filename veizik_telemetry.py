#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
veizik_telemetry.py — Veizik telemetry client (pure stdlib).

Product context
---------------
Veizik ships ONE public bootstrap CLI. Paid tiers unlock signed entitlements +
private runtime packs; there are no per-tier binaries. This module is part of the
PUBLIC binary surface (CLI parser, updater, doctor, license client, telemetry
client, pack loader, signature verifier, public Adapter interface).

Two strictly separated data classes — NEVER merge them
-----------------------------------------------------
A. License operational data (no consent required; strictly necessary to provide
   the purchased service): license id/hash, pseudonymous device identifier, plan,
   app/protocol version, activation state, verification time, run lease/expiry,
   subscription state.
   ->  handled by veizik_entitlement.py against api.veizik.com/v1/license/*.
       NOT this module.

B. Optional performance & compatibility data (separate, explicit, opt-in
   consent): hardware, workload settings, result timings, product signals.
   ->  this module, against the telemetry service.

Different hosts, different code paths, different stores, on purpose. A telemetry
outage must never block a render, a license check, or the CLI.

Terminology
-----------
We say "pseudonymous", never "anonymous". installation_id is a salted hash, but
it is linkable to a license and therefore to an email; under Korean PIPA and
GDPR that is pseudonymised personal data, not anonymous data. Do not write
"anonymous" in UI, docs, or comments.

Consent (GDPR Art. 4(11) / Art. 7)
----------------------------------
Consent must be freely given, specific, informed, and an unambiguous affirmative
action, separated per purpose. Declining optional telemetry MUST NOT lock any
purchased core feature. This module therefore has exactly one failure mode:
it does nothing.

Component status: Shipped (client spool + batch send).
Server aggregation dashboard: Development.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import shutil
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "CONSENT_VERSION",
    "SCHEMA_VERSION",
    "installation_id",
    "build_report",
    "record_run",
    "status",
    "enable",
    "disable",
    "show_last",
    "queue",
    "send",
    "maybe_send_async",
    "export",
    "delete",
    "consent_screen_text",
    "retention_note",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SCHEMA_VERSION = "veizik-run-report-v1"
CONSENT_VERSION = "telemetry-v1.1"

# Telemetry endpoint is intentionally NOT the license endpoint.
DEFAULT_TELEMETRY_BASE = "https://veizik.com/v1/events"
# Reference only — this module never calls it.
LICENSE_BASE_HINT = "https://api.veizik.com/v1/license"

HTTP_TIMEOUT_S = 8.0
BATCH_INTERVAL_S = 24 * 60 * 60          # 24h batch cadence
SPOOL_MAX_ITEMS = 100
SPOOL_MAX_BYTES = 10 * 1024 * 1024       # 10 MB
SEND_MAX_BATCH = 50

VEIZIK_HOME = os.path.expanduser(os.environ.get("VEIZIK_HOME", "~/.veizik"))
CONSENT_PATH = os.path.join(VEIZIK_HOME, "telemetry.json")
SALT_PATH = os.path.join(VEIZIK_HOME, "install_salt")
QUEUE_DIR = os.path.join(VEIZIK_HOME, "telemetry_queue")
LAST_PATH = os.path.join(VEIZIK_HOME, "telemetry_last.json")
STATE_PATH = os.path.join(VEIZIK_HOME, "telemetry_state.json")

_UA = "veizik-telemetry/1.1 (+https://veizik.com)"


def telemetry_base() -> str:
    """Events endpoint. VEIZIK_TELEMETRY_BASE wins; VEIZIK_TELEMETRY_API is
    accepted as a legacy alias so a split deployment cannot silently post to the
    wrong host."""
    base = os.environ.get("VEIZIK_TELEMETRY_BASE")
    if not base:
        legacy = os.environ.get("VEIZIK_TELEMETRY_API")
        base = (legacy.rstrip("/") + "/events") if legacy else DEFAULT_TELEMETRY_BASE
    return base.rstrip("/")


def retention_note() -> str:
    return (
        "Raw reports are kept 30-90 days, then only aggregates remain. Public "
        "figures are published only above a minimum sample size. Erasure by "
        "installation id removes raw rows."
    )


# --------------------------------------------------------------------------- #
# Allowlist — the ONLY fields that may leave this machine
# --------------------------------------------------------------------------- #

RUNTIME_FIELDS = {
    "veizik_version",
    "adapter_id",
    "capsule_id",
    "execution_backend",
    "profile_id",
}

HARDWARE_FIELDS = {
    "gpu_vendor",
    "gpu_model",
    "gpu_vram_gb",
    "gpu_count",
    "gpu_arch",
    "driver_version",
    "cuda_version",
    "os_major",
    "cpu_class",
    "ram_bucket_gb",
    "storage_class",
    "power_limit_w",
    "form_factor",
}

WORKLOAD_FIELDS = {
    "model_public_id",
    "model_hash",
    "precision",
    "quantization",
    "attention_backend",
    "width",
    "height",
    "resolution",
    "frames",
    "steps",
    "batch",
    "tile",
    "offload",
    "concurrent_jobs",
    "start_kind",          # cold | warm
}

RESULT_FIELDS = {
    "started_at",
    "ended_at",
    "wall_s",
    "model_load_s",
    "denoise_s",
    "vae_s",
    "post_s",
    "peak_vram_gb",
    "peak_ram_gb",
    "gpu_util_mean",
    "power_w",
    "status",              # ok | failed
    "error_code",
    "interrupted_stage",
    "oom",
    "recovered",
    "user_abort",
}

SIGNAL_FIELDS = {
    "first_success",
    "cumulative_successes",
    "active_days",
    "feature_uses",
    "preview_requests",
    "pro_interest",
}

TOP_FIELDS = {
    "schema_version",
    "event_id",
    "installation_id",
    "license_tier",
    "telemetry_consent_version",
    "runtime",
    "hardware",
    "workload",
    "result",
    "signals",
    "privacy",
}

SECTION_ALLOWLIST: Dict[str, set] = {
    "runtime": RUNTIME_FIELDS,
    "hardware": HARDWARE_FIELDS,
    "workload": WORKLOAD_FIELDS,
    "result": RESULT_FIELDS,
    "signals": SIGNAL_FIELDS,
}

PRIVACY_FLAGS = (
    "contains_prompt",
    "contains_input",
    "contains_output",
    "contains_local_path",
)

# --------------------------------------------------------------------------- #
# Deny patterns — never collected without a separate, distinct consent.
#
# When one of these survives into a report we do NOT silently drop it and call
# the report clean: we strip the value AND raise the matching privacy flag, so
# the server rejects the whole event. Fail loud, fail safe. Losing a metric is
# cheaper than leaking a prompt.
# --------------------------------------------------------------------------- #

DENY_PATTERNS: Tuple[Tuple[str, str], ...] = (
    # (substring matched against the lowercased key, privacy flag to raise)
    ("prompt", "contains_prompt"),
    ("negative", "contains_prompt"),
    ("caption", "contains_prompt"),
    ("text_input", "contains_prompt"),
    ("terminal", "contains_prompt"),
    ("stdin", "contains_prompt"),
    ("input_image", "contains_input"),
    ("input_video", "contains_input"),
    ("init_image", "contains_input"),
    ("ref_image", "contains_input"),
    ("source_media", "contains_input"),
    ("output_image", "contains_output"),
    ("output_video", "contains_output"),
    ("result_media", "contains_output"),
    ("thumbnail", "contains_output"),
    ("latents", "contains_output"),
    ("filename", "contains_local_path"),
    ("file_name", "contains_local_path"),
    ("filepath", "contains_local_path"),
    ("file_path", "contains_local_path"),
    ("path", "contains_local_path"),
    ("dir", "contains_local_path"),
    ("folder", "contains_local_path"),
    ("cwd", "contains_local_path"),
    ("home", "contains_local_path"),
    ("username", "contains_local_path"),
    ("user_name", "contains_local_path"),
    ("hostname", "contains_local_path"),
    ("computer", "contains_local_path"),
    ("machine_name", "contains_local_path"),
    ("ip_addr", "contains_local_path"),
    ("ip_address", "contains_local_path"),
    ("client_ip", "contains_local_path"),
    ("env", "contains_local_path"),
    ("environ", "contains_local_path"),
    ("api_key", "contains_local_path"),
    ("apikey", "contains_local_path"),
    ("secret", "contains_local_path"),
    ("token", "contains_local_path"),
    ("password", "contains_local_path"),
    ("credential", "contains_local_path"),
    ("crash_dump", "contains_local_path"),
    ("core_dump", "contains_local_path"),
    ("memory_dump", "contains_local_path"),
)

# NOTE: allowlisted keys are matched FIRST and are never deny-scanned, so
# legitimate names that happen to contain a deny substring cannot be tripped.

_PATH_MARKERS = ("/users/", "/home/", "/var/folders/", "c:\\", "\\users\\", "/private/")


def _deny_flag_for_key(key: str) -> Optional[str]:
    k = key.lower()
    for needle, flag in DENY_PATTERNS:
        if needle in k:
            return flag
    return None


def _looks_like_local_path(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    v = value.lower()
    return any(m in v for m in _PATH_MARKERS)


def _looks_like_free_text(value: Any) -> bool:
    """Allowlisted fields are short identifiers or numbers. A long multi-word
    string in a report is almost certainly a prompt leak through a legal key."""
    if not isinstance(value, str):
        return False
    return len(value) > 200 or value.count(" ") >= 8


# --------------------------------------------------------------------------- #
# Small fs helpers (never raise to the caller)
# --------------------------------------------------------------------------- #


def _ensure_dirs() -> None:
    for d in (VEIZIK_HOME, QUEUE_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    try:
        os.chmod(VEIZIK_HOME, 0o700)
    except OSError:
        pass


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: str, obj: dict, mode: int = 0o600) -> bool:
    _ensure_dirs()
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        parent = os.path.dirname(tmp)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# Pseudonymous installation id
# --------------------------------------------------------------------------- #


def _salt() -> str:
    """Random per-installation salt, generated once, stored 0600.

    Without the salt a machine fingerprint would be a stable global identifier
    correlatable across vendors. With it, the id is meaningless outside this
    machine's ~/.veizik.
    """
    _ensure_dirs()
    try:
        with open(SALT_PATH, "r", encoding="utf-8") as fh:
            s = fh.read().strip()
        if len(s) >= 32:
            return s
    except OSError:
        pass
    s = uuid.uuid4().hex + uuid.uuid4().hex
    try:
        with open(SALT_PATH, "w", encoding="utf-8") as fh:
            fh.write(s)
        os.chmod(SALT_PATH, 0o600)
    except OSError:
        pass
    return s


def _machine_fingerprint() -> str:
    """Composite host fingerprint.

    Deliberately NOT GPU-only: users swap and add GPUs, and multi-GPU boxes would
    collide or churn. The raw components never leave the machine — only the
    salted hash does.
    """
    parts = [
        platform.system(),
        platform.machine(),
        platform.release().split("-")[0],
        str(uuid.getnode()),                     # MAC-derived node id
        os.path.expanduser("~"),                 # home path shape (hashed only)
    ]
    return "|".join(p or "" for p in parts)


def installation_id() -> str:
    """Pseudonymous, stable-per-installation identifier."""
    digest = hashlib.sha256(
        ("veizik-install-v1|" + _salt() + "|" + _machine_fingerprint()).encode("utf-8")
    ).hexdigest()
    return "ins_" + digest[:32]


# --------------------------------------------------------------------------- #
# Consent store
# --------------------------------------------------------------------------- #


def _consent() -> dict:
    data = _read_json(CONSENT_PATH) or {}
    return {
        "enabled": bool(data.get("enabled", False)),
        "consent_version": data.get("consent_version") or CONSENT_VERSION,
        "consented_at": data.get("consented_at"),
        "installation_id": data.get("installation_id") or installation_id(),
        "asked": bool(data),
    }


def consent_asked() -> bool:
    """False means the user has never seen the consent screen."""
    return _consent()["asked"]


def _save_consent(enabled: bool) -> dict:
    rec = {
        "enabled": bool(enabled),
        "consent_version": CONSENT_VERSION,
        "consented_at": _iso_now() if enabled else None,
        "installation_id": installation_id(),
    }
    _write_json(CONSENT_PATH, rec)
    return rec


def is_enabled() -> bool:
    """True only when consent is present AND matches the current consent version.

    A consent-version bump invalidates prior consent: the purpose description
    changed, so the user must be asked again.
    """
    c = _consent()
    return bool(c["enabled"]) and c["consent_version"] == CONSENT_VERSION


# --------------------------------------------------------------------------- #
# Report construction
# --------------------------------------------------------------------------- #


def build_report(**fields: Any) -> Dict[str, Any]:
    """Build a veizik-run-report-v1 dict.

    Accepts nested sections (runtime={...}, hardware={...}, ...) and/or flat
    keyword fields, which are routed into their section by the allowlist.

    privacy flags are ALWAYS emitted false here. _scrub() is the only thing
    allowed to flip one, and a flipped flag is a rejection signal, not metadata.
    """
    sections: Dict[str, Dict[str, Any]] = {
        "runtime": {},
        "hardware": {},
        "workload": {},
        "result": {},
        "signals": {},
    }
    extras: Dict[str, Any] = {}

    for key, value in fields.items():
        if key in sections and isinstance(value, dict):
            sections[key].update(value)
            continue
        placed = False
        for sect, allowed in SECTION_ALLOWLIST.items():
            if key in allowed:
                sections[sect][key] = value
                placed = True
                break
        if not placed and key not in TOP_FIELDS:
            # Kept so _scrub() can see, flag and strip it. Silently swallowing
            # unknown fields here would defeat the whole point of the check.
            extras[key] = value

    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": "evt_" + uuid.uuid4().hex,
        "installation_id": fields.get("installation_id") or installation_id(),
        "license_tier": fields.get("license_tier") or "starter_preview",
        "telemetry_consent_version": CONSENT_VERSION,
        "runtime": sections["runtime"],
        "hardware": sections["hardware"],
        "workload": sections["workload"],
        "result": sections["result"],
        "signals": sections["signals"],
        "privacy": {flag: False for flag in PRIVACY_FLAGS},
    }
    report.update(extras)
    return report


# --------------------------------------------------------------------------- #
# Scrubber
# --------------------------------------------------------------------------- #


def _scrub(report: Dict[str, Any]) -> Dict[str, Any]:
    """Allowlist-enforce a report.

    Rules:
      1. Only allowlisted top-level keys, and allowlisted per-section keys,
         survive.
      2. Any removed key matching a deny pattern raises its privacy flag.
      3. Any surviving VALUE that looks like a local path or free text is
         removed and raises a flag too — catches a legal key stuffed with
         illegal content.
      4. A raised flag means the server MUST reject the event. Intended.
    """
    privacy = {flag: False for flag in PRIVACY_FLAGS}
    removed: List[str] = []

    def raise_flag(flag: Optional[str]) -> None:
        if flag:
            privacy[flag] = True

    def inspect_value(value: Any, _depth: int = 0) -> bool:
        """Returns True if the value (or anything nested inside it) is disqualifying.

        MUST recurse into containers. Inspecting only ``str`` was a real leak: a prompt
        nested inside a dict/list under an allowlisted field passed untouched, every
        privacy flag stayed false, and the server — which trusts those flags — accepted
        it. Banned *keys* inside nested containers raise their flag too.
        """
        dirty = False
        if _looks_like_local_path(value):
            privacy["contains_local_path"] = True
            dirty = True
        if _looks_like_free_text(value):
            privacy["contains_prompt"] = True
            dirty = True
        if _depth < 8:
            if isinstance(value, dict):
                for k, v in list(value.items())[:64]:
                    flag = _deny_flag_for_key(str(k))
                    if flag:
                        raise_flag(flag)
                        dirty = True
                    if inspect_value(v, _depth + 1):
                        dirty = True
            elif isinstance(value, (list, tuple)):
                for v in list(value)[:64]:
                    if inspect_value(v, _depth + 1):
                        dirty = True
        return dirty

    clean: Dict[str, Any] = {}

    for key, value in (report or {}).items():
        if key == "privacy":
            continue

        if key not in TOP_FIELDS:
            removed.append(key)
            raise_flag(_deny_flag_for_key(key))
            inspect_value(value)
            continue

        if key in SECTION_ALLOWLIST:
            allowed = SECTION_ALLOWLIST[key]
            section_in = value if isinstance(value, dict) else {}
            section_out: Dict[str, Any] = {}
            for sub_key, sub_val in section_in.items():
                if sub_key not in allowed:
                    removed.append("%s.%s" % (key, sub_key))
                    raise_flag(_deny_flag_for_key(sub_key))
                    inspect_value(sub_val)
                    continue
                if inspect_value(sub_val):
                    removed.append("%s.%s" % (key, sub_key))
                    continue
                section_out[sub_key] = _scalarize(sub_val)
            clean[key] = section_out
        else:
            if inspect_value(value):
                removed.append(key)
                continue
            clean[key] = _scalarize(value)

    clean.setdefault("schema_version", SCHEMA_VERSION)
    clean.setdefault("event_id", "evt_" + uuid.uuid4().hex)
    clean.setdefault("installation_id", installation_id())
    clean.setdefault("license_tier", "starter_preview")
    clean.setdefault("telemetry_consent_version", CONSENT_VERSION)
    for sect in SECTION_ALLOWLIST:
        clean.setdefault(sect, {})
    clean["privacy"] = privacy
    if removed:
        # Count only. The stripped NAMES are not transmitted either — a key name
        # can itself be sensitive (e.g. a path used as a dict key).
        clean["scrubbed_field_count"] = len(removed)
    return clean


def _scalarize(value: Any) -> Any:
    """Only JSON scalars, and flat containers of scalars, are transmittable."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, (list, tuple)):
        return [_scalarize(v) for v in list(value)[:32]]
    if isinstance(value, dict):
        return {str(k)[:64]: _scalarize(v) for k, v in list(value.items())[:32]}
    return str(value)[:200]


def report_is_clean(report: Dict[str, Any]) -> bool:
    """Mirror of the server-side gate: every privacy flag must be exactly false.

    scrubbed_field_count is tolerated (it carries no user content) but a report
    that tripped a flag is never sent.
    """
    priv = (report or {}).get("privacy") or {}
    return all(priv.get(flag) is False for flag in PRIVACY_FLAGS)


# --------------------------------------------------------------------------- #
# Spool
# --------------------------------------------------------------------------- #


def _queue_files() -> List[str]:
    try:
        names = [n for n in os.listdir(QUEUE_DIR) if n.endswith(".json")]
    except OSError:
        return []
    paths = [os.path.join(QUEUE_DIR, n) for n in names]
    paths.sort(key=lambda p: (_safe_mtime(p), p))
    return paths


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _enforce_spool_caps() -> int:
    """Drop oldest-first until under both caps. Returns the number dropped."""
    dropped = 0
    paths = _queue_files()
    total = sum(_safe_size(p) for p in paths)
    while paths and (len(paths) > SPOOL_MAX_ITEMS or total > SPOOL_MAX_BYTES):
        victim = paths.pop(0)
        total -= _safe_size(victim)
        try:
            os.unlink(victim)
            dropped += 1
        except OSError:
            pass
    return dropped


def _spool(report: Dict[str, Any]) -> Optional[str]:
    _ensure_dirs()
    event_id = str(report.get("event_id") or uuid.uuid4().hex)
    name = "%d-%s.json" % (int(time.time() * 1000), event_id[-12:])
    path = os.path.join(QUEUE_DIR, name)
    if not _write_json(path, report):
        return None
    _write_json(LAST_PATH, report)
    _enforce_spool_caps()
    return path


# --------------------------------------------------------------------------- #
# Local product-signal state (leaves the machine only via the allowlist)
# --------------------------------------------------------------------------- #


def _state() -> dict:
    return _read_json(STATE_PATH) or {}


def _bump_state(success: bool) -> dict:
    st = _state()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    days = set(st.get("active_days_list") or [])
    days.add(today)
    if success:
        st["cumulative_successes"] = int(st.get("cumulative_successes") or 0) + 1
        if not st.get("first_success_at"):
            # Starter Preview's 7-day trial starts at the FIRST SUCCESSFUL
            # RENDER, not at activation — driver/install trouble must not burn
            # the trial.
            st["first_success_at"] = _iso_now()
    st["active_days_list"] = sorted(days)[-400:]
    st["active_days"] = len(days)
    _write_json(STATE_PATH, st)
    return st


def first_success_at() -> Optional[str]:
    """Trial clock origin. Local; the license client owns the actual decision."""
    return _state().get("first_success_at")


# --------------------------------------------------------------------------- #
# Public API — record
# --------------------------------------------------------------------------- #


def record_run(**fields: Any) -> Optional[str]:
    """Build -> scrub -> spool one run report.

    Returns the spool path, or None if nothing was recorded. Without consent it
    returns None and writes nothing beyond the local, never-transmitted signal
    counters (which the trial clock needs regardless of telemetry).
    """
    try:
        res = fields.get("result") or {}
        success = bool(
            res.get("status", "ok") == "ok"
            if "status" in res
            else fields.get("success", True)
        )
        _bump_state(success)

        if not is_enabled():
            return None

        st = _state()
        signals = dict(fields.get("signals") or {})
        signals.setdefault("cumulative_successes", st.get("cumulative_successes", 0))
        signals.setdefault("active_days", st.get("active_days", 0))
        signals.setdefault(
            "first_success", bool(success and st.get("cumulative_successes") == 1)
        )
        fields["signals"] = signals

        report = _scrub(build_report(**fields))
        return _spool(report)
    except Exception:
        # Telemetry must never surface an exception into the render path.
        return None


# --------------------------------------------------------------------------- #
# Public API — transport
# --------------------------------------------------------------------------- #


def due_for_send() -> bool:
    last = float(_state().get("last_send_ts") or 0.0)
    return (time.time() - last) >= BATCH_INTERVAL_S


def send(max_batch: int = SEND_MAX_BATCH, force: bool = False) -> bool:
    """Gzip-batch POST the spool. True only on a clean 2xx flush.

    Swallows every failure. A dead endpoint, a captive portal, an offline
    laptop — none of it is allowed to matter to the user's render.
    """
    try:
        if not is_enabled():
            return False
        if not force and not due_for_send():
            return False

        paths = _queue_files()[:max_batch]
        if not paths:
            return False

        events: List[dict] = []
        used: List[str] = []
        for p in paths:
            rec = _read_json(p)
            if rec is None or not report_is_clean(rec):
                # Unreadable, or would be rejected server-side anyway. Drop it
                # locally rather than retry forever.
                try:
                    os.unlink(p)
                except OSError:
                    pass
                continue
            events.append(rec)
            used.append(p)

        if not events:
            return False

        payload = {
            "schema_version": SCHEMA_VERSION,
            "installation_id": installation_id(),
            "telemetry_consent_version": CONSENT_VERSION,
            "sent_at": _iso_now(),
            "events": events,
        }
        body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        req = urllib.request.Request(
            telemetry_base(),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "User-Agent": _UA,
                "X-Veizik-Schema": SCHEMA_VERSION,
                "X-Veizik-Consent": CONSENT_VERSION,
            },
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            code = int(getattr(resp, "status", None) or resp.getcode())
            resp.read()
        if not (200 <= code < 300):
            return False

        for p in used:
            try:
                os.unlink(p)
            except OSError:
                pass

        st = _state()
        st["last_send_ts"] = time.time()
        st["last_send_count"] = len(events)
        _write_json(STATE_PATH, st)
        return True

    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False
    except Exception:
        return False


def maybe_send_async() -> None:
    """Post-render hook: fire a batch upload only when the 24h window is open,
    on a daemon thread, so no part of the render path ever waits on a socket."""
    try:
        if not is_enabled() or not due_for_send() or not _queue_files():
            return
        import threading

        threading.Thread(target=lambda: send(force=False), daemon=True).start()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Public API — CLI surface
# --------------------------------------------------------------------------- #


def status() -> Dict[str, Any]:
    """Data for `veizik telemetry status`.

    Enumerates BOTH what is collected and what is never collected. A privacy
    claim the user cannot inspect is not a privacy claim.
    """
    c = _consent()
    paths = _queue_files()
    st = _state()
    return {
        "enabled": is_enabled(),
        "asked": c["asked"],
        "consent_version": c["consent_version"],
        "consent_version_current": CONSENT_VERSION,
        "reconsent_required": bool(
            c["enabled"] and c["consent_version"] != CONSENT_VERSION
        ),
        "consented_at": c["consented_at"],
        "installation_id": c["installation_id"],
        "installation_id_kind": "pseudonymous (salted hash; linkable to a license)",
        "endpoint": telemetry_base(),
        "license_endpoint_separate": LICENSE_BASE_HINT,
        "queued": len(paths),
        "queued_bytes": sum(_safe_size(p) for p in paths),
        "spool_limits": {"max_items": SPOOL_MAX_ITEMS, "max_bytes": SPOOL_MAX_BYTES},
        "last_send_ts": st.get("last_send_ts"),
        "batch_interval_s": BATCH_INTERVAL_S,
        "collected": COLLECTED_DESCRIPTION,
        "never_collected": NEVER_COLLECTED_DESCRIPTION,
        "retention": retention_note(),
        "note": (
            "Optional performance data only. License operation data is handled "
            "separately by the license client and is required to provide the "
            "service. Declining this restricts no purchased feature."
        ),
    }


def status_text() -> str:
    """Human-readable `veizik telemetry status` rendering, with the ✓ / ✗ lists."""
    s = status()
    lines = [
        "veizik telemetry — optional performance data",
        "  state             : %s" % ("ENABLED" if s["enabled"] else "disabled"),
        "  consent version   : %s%s"
        % (
            s["consent_version"],
            "  (re-consent required)" if s["reconsent_required"] else "",
        ),
        "  consented at      : %s" % (s["consented_at"] or "-"),
        "  installation id   : %s  [%s]" % (s["installation_id"], s["installation_id_kind"]),
        "  endpoint          : %s" % s["endpoint"],
        "  queued reports    : %d  (%d bytes; cap %d / %d bytes)"
        % (
            s["queued"],
            s["queued_bytes"],
            s["spool_limits"]["max_items"],
            s["spool_limits"]["max_bytes"],
        ),
        "",
        "  Collected:",
    ]
    lines += ["    ✓ %s" % item for item in s["collected"]]
    lines += ["", "  Never collected:"]
    lines += ["    ✗ %s" % item for item in s["never_collected"]]
    lines += ["", "  " + s["retention"], "  " + s["note"]]
    return "\n".join(lines)


COLLECTED_DESCRIPTION: List[str] = [
    "Hardware: GPU vendor/model/VRAM/count/architecture, driver, CUDA, OS major, CPU class, RAM bucket, storage class, power limit, form factor",
    "Run settings: veizik version, capsule_id, adapter_id, public model id + weight hash, precision, quantization, attention backend, resolution, frames, steps, batch, tile, offload, concurrent jobs, cold/warm start",
    "Results: start/end, wall time, model load / denoise / VAE / post-process time, peak VRAM, peak RAM, mean GPU utilisation, power draw (optional), success or failure, error code, stage of interruption, OOM, Recover triggered, user abort",
    "Product signals: first successful render, cumulative successes, days used, per-feature use counts, Preview requests, Pro interest",
    "Identifiers: pseudonymous installation id, license tier, consent version",
]

NEVER_COLLECTED_DESCRIPTION: List[str] = [
    "Prompt text and negative prompt",
    "Input images or video",
    "Generated output (images, video, latents, thumbnails)",
    "File names and full local paths",
    "User name, computer name",
    "IP address stored as behavioural data",
    "Full environment variables",
    "API keys and external tokens",
    "Personal model directories",
    "Terminal input",
    "Raw crash-dump memory",
]


def enable() -> Dict[str, Any]:
    """Record an affirmative opt-in for the CURRENT consent version."""
    _save_consent(True)
    return status()


def disable(purge_queue: Optional[bool] = None) -> Dict[str, Any]:
    """Stop all future transmission immediately.

    purge_queue=None means the CLI still has to ask the user whether to delete
    the local spool; it should prompt and re-call with an explicit True/False.
    Transmission is already stopped either way — the open question is only the
    fate of the local queue.
    """
    _save_consent(False)
    purged = 0
    if purge_queue is True:
        purged = _purge_queue()
    out = status()
    out["queue_purged"] = purged
    out["queue_decision_pending"] = purge_queue is None
    out["queue_prompt"] = (
        "Delete the %d report(s) still queued on this machine? "
        "Nothing further will be sent either way." % out["queued"]
    )
    return out


# --------------------------------------------------------------------------- CLI compatibility API
# veizik_cli.py's `telemetry` command was written against these names. They are thin adapters over
# the canonical functions above — keep both in sync rather than duplicating logic.
# telemetry_base() is the full events URL (…/v1/events); the CLI appends "/events" to TELEMETRY_API,
# so expose the parent here to avoid printing a doubled path.
TELEMETRY_API = telemetry_base().rsplit("/events", 1)[0]
COLLECTED = [(d.split(":", 1)[0], d.split(":", 1)[1].strip() if ":" in d else d)
             for d in COLLECTED_DESCRIPTION]
NEVER_COLLECTED = list(NEVER_COLLECTED_DESCRIPTION)


def enabled() -> bool:
    """Alias of is_enabled() (CLI-facing name)."""
    return is_enabled()


def consent_state() -> Dict[str, Any]:
    """Consent record in the shape the CLI prints: {consent_version, decided_at, enabled}."""
    c = _consent() or {}
    decided = c.get("consented_at") or c.get("decided_at")
    if isinstance(decided, str):                       # stored ISO -> epoch for strftime
        try:
            decided = int(time.mktime(time.strptime(decided[:19], "%Y-%m-%dT%H:%M:%S")))
        except Exception:
            decided = None
    return {"consent_version": c.get("consent_version"), "decided_at": decided,
            "enabled": bool(c.get("enabled"))}


def set_consent(on: bool) -> Dict[str, Any]:
    return enable() if on else disable(purge_queue=False)


def queue_count() -> int:
    return len(_queue_files())


def queue_bytes() -> int:
    return sum(_safe_size(p) for p in _queue_files())


def clear_queue() -> int:
    return _purge_queue()


def last_report() -> Optional[Dict[str, Any]]:
    return show_last()


def _spool_read() -> List[Dict[str, Any]]:
    return queue()


def delete_request(**kw) -> Dict[str, Any]:
    return delete(**kw)


def _purge_queue() -> int:
    n = 0
    for p in _queue_files():
        try:
            os.unlink(p)
            n += 1
        except OSError:
            pass
    try:
        os.unlink(LAST_PATH)
    except OSError:
        pass
    return n


def show_last() -> Optional[Dict[str, Any]]:
    """The exact payload most recently queued. Nothing is hidden from the user."""
    return _read_json(LAST_PATH)


def queue() -> List[Dict[str, Any]]:
    """Every pending report, in send order."""
    out: List[Dict[str, Any]] = []
    for p in _queue_files():
        rec = _read_json(p)
        if rec is not None:
            out.append({"file": os.path.basename(p), "report": rec})
    return out


def export(path: str) -> Optional[str]:
    """GDPR Art. 15/20 — write everything this machine holds to one JSON file."""
    try:
        bundle = {
            "exported_at": _iso_now(),
            "consent": _consent(),
            "status": status(),
            "local_state": _state(),
            "queued_reports": [item["report"] for item in queue()],
            "last_report": show_last(),
        }
        target = os.path.abspath(os.path.expanduser(path))
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target
    except OSError:
        return None


def delete(request_server_deletion: bool = True) -> Dict[str, Any]:
    """GDPR Art. 17 — erase locally, and ask the server to erase raw events.

    Server-side deletion is keyed on installation_id, so the id must be captured
    and sent BEFORE the local salt is destroyed; otherwise the raw rows become
    unreachable.
    """
    inst = installation_id()
    result: Dict[str, Any] = {
        "installation_id": inst,
        "local_purged": 0,
        "server_requested": False,
        "server_ok": False,
        "message": "",
    }

    if request_server_deletion:
        result["server_requested"] = True
        try:
            body = json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "installation_id": inst,
                    "requested_at": _iso_now(),
                    "action": "delete_raw_events",
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                telemetry_base().rsplit("/", 1)[0] + "/deletion-requests",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json", "User-Agent": _UA},
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                code = int(getattr(resp, "status", None) or resp.getcode())
                resp.read()
            result["server_ok"] = 200 <= code < 300
        except Exception:
            result["server_ok"] = False

    result["local_purged"] = _purge_queue()
    for p in (STATE_PATH, LAST_PATH):
        try:
            os.unlink(p)
        except OSError:
            pass
    shutil.rmtree(QUEUE_DIR, ignore_errors=True)
    _save_consent(False)

    result["message"] = (
        "Local data erased; server erasure requested for %s." % inst
        if result["server_ok"]
        else "Local data erased. Server erasure NOT confirmed — re-run "
        "`veizik telemetry delete` while online (id %s)." % inst
    )
    return result


# --------------------------------------------------------------------------- #
# Consent screen copy (2 steps, per spec)
# --------------------------------------------------------------------------- #


def consent_screen_text() -> Dict[str, Any]:
    """Copy for the two-step consent flow.

    Step 1 is a NOTICE (Continue only) — license operation data is necessary to
    provide the purchased service, so it is not consent-based and must not be
    dressed up as a choice.
    Step 2 is CONSENT (Yes/No, both proceed) — optional performance data.
    """
    return {
        "step_1": {
            "kind": "notice",
            "title": "License operation data",
            "body": [
                "To run Veizik under your license we process: license id/hash, a "
                "pseudonymous device identifier, your plan, app and protocol "
                "version, activation state, verification time, run lease and "
                "expiry, and subscription state.",
                "This is the minimum required to operate the license — including "
                "your registered-device and concurrent-node limits — and is not "
                "optional.",
                "It is handled by the license service, separately from anything "
                "on the next screen.",
            ],
            "actions": ["Continue"],
        },
        "step_2": {
            "kind": "consent",
            "title": "Optional performance and compatibility data",
            "body": [
                "May we collect hardware, run settings and timing results, so "
                "Veizik gets faster and more compatible on machines like yours?",
                "Collected: " + "; ".join(COLLECTED_DESCRIPTION),
                "Never collected: " + "; ".join(NEVER_COLLECTED_DESCRIPTION),
                retention_note(),
                "Change it any time with `veizik telemetry disable`, inspect what "
                "would be sent with `veizik telemetry show-last`, and erase it "
                "with `veizik telemetry delete`.",
                "Saying No changes nothing about the features you have paid for.",
            ],
            "actions": ["Yes, share performance data", "No thanks"],
            "consent_version": CONSENT_VERSION,
        },
    }


# --------------------------------------------------------------------------- #
# CLI entry / self-check
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        print(status_text())
    elif cmd == "enable":
        enable()
        print(status_text())
    elif cmd == "disable":
        purge = True if "--purge" in sys.argv else (False if "--keep" in sys.argv else None)
        print(json.dumps(disable(purge), ensure_ascii=False, indent=2))
    elif cmd == "show-last":
        print(json.dumps(show_last(), ensure_ascii=False, indent=2))
    elif cmd == "queue":
        print(json.dumps(queue(), ensure_ascii=False, indent=2))
    elif cmd == "send":
        print(json.dumps({"sent": send(force=True)}, ensure_ascii=False))
    elif cmd == "export":
        print(export(sys.argv[2] if len(sys.argv) > 2 else "./veizik_telemetry_export.json"))
    elif cmd == "delete":
        print(json.dumps(delete(), ensure_ascii=False, indent=2))
    elif cmd == "consent":
        print(json.dumps(consent_screen_text(), ensure_ascii=False, indent=2))
    elif cmd == "selftest":
        dirty = _scrub(
            build_report(
                license_tier="founding_creator",
                runtime={"veizik_version": "0.1.0", "execution_backend": "experimental"},
                hardware={"gpu_model": "RTX 3090", "gpu_vram_gb": 24},
                workload={"steps": 30, "precision": "fp16"},
                result={"status": "ok", "wall_s": 138.0, "peak_vram_gb": 9.55},
                prompt="a cat riding a bicycle through neon Seoul at night, cinematic",
                output_path="/Users/dongkoo/out.mp4",
            )
        )
        assert "prompt" not in dirty and "output_path" not in dirty
        assert dirty["privacy"]["contains_prompt"] is True
        assert dirty["privacy"]["contains_local_path"] is True
        assert report_is_clean(dirty) is False, "dirty report must fail the server gate"

        clean = _scrub(
            build_report(
                runtime={"veizik_version": "0.1.0"},
                hardware={"gpu_model": "RTX 3090"},
                result={"status": "ok", "wall_s": 1.0},
            )
        )
        assert report_is_clean(clean) is True
        assert clean["runtime"]["veizik_version"] == "0.1.0"

        # A legal key stuffed with an illegal value must still be caught.
        stuffed = _scrub(build_report(workload={"model_public_id": "/Users/x/models/a.safetensors"}))
        assert report_is_clean(stuffed) is False
        assert "model_public_id" not in stuffed["workload"]

        # No consent => record_run writes nothing.
        if not is_enabled():
            assert record_run(result={"status": "ok"}) is None

        print("selftest OK")
    else:
        print(
            "usage: veizik_telemetry.py "
            "status|enable|disable|show-last|queue|send|export|delete|consent|selftest"
        )
