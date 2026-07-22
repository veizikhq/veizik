#!/usr/bin/env python3
"""veizik_entitlement — client-side license gate for the veizik CLI.

This is the paywall layer: it fetches a server-signed entitlement from veizik.com and gates
features/resolution/watermark by tier. It holds NO signing secret — only the Cloudflare Worker holds
the Ed25519 private key. The client PINS the server's Ed25519 public key and VERIFIES the signature on
every entitlement it loads (fresh or cached). A forged or hand-edited session (e.g. tier set to
"studio") fails verification and falls back to Free — editing ~/.veizik/session.json no longer grants
a paid tier. TLS protects the fetch; the pinned signature protects the cache.

Because this is open-source Python, a determined user can still rebuild the binary to skip this call
entirely; that is expected. The signature check closes the trivial file-edit bypass, not a full
recompile. Cryptographic enforcement (kernel entanglement in the native engine) is the deeper layer
that makes a skipped check yield a non-functional engine. Do not oversell this as uncrackable.

Flow:
    veizik login <api_key>   -> POST /api/entitlement, cache session (~/.veizik/session.json, 0600)
    veizik status            -> show current tier + entitlement
    veizik logout            -> drop the session
    (render commands)        -> resolve() gates features, clamps free-tier res/frames, stamps output
"""
import os, sys, json, time, base64, hmac as _hmac, urllib.request, urllib.error

API_BASE = os.environ.get("VEIZIK_API_BASE", "https://veizik.com").rstrip("/")
STATUS_BASE = os.environ.get("VEIZIK_STATUS_BASE", "https://veizik-status.deno.dev").rstrip("/")
SESSION = os.path.expanduser(os.environ.get("VEIZIK_SESSION", "~/.veizik/session.json"))
SHORT_GRACE_S   = 24 * 3600          # offline / outage-UNCONFIRMED -> bounds a deliberately-offline user
OUTAGE_GRACE_S  = 14 * 24 * 3600     # oracle-CONFIRMED global primary outage -> keep honest users working
_MAX_SKEW_S     = 300                # clock-skew tolerance for nbf / anti-rollback high-water-mark
_OUTAGE_MAX_AGE = 300                # oracle attestation must be < 5 min old (anti-replay)
# Liveness oracle public key (key #2, low-value: can ONLY sign outage attestations, never a tier/exp).
# Empty until the oracle is stood up -> the oracle branch is skipped and grace stays SHORT (safe default).
_LIVE_PUB_B64   = os.environ.get("VEIZIK_LIVE_PUB", "")
_HWM = SESSION + ".hwm"              # tamper-evident anti-rollback anchor sidecar

# Free-tier / entry caps (paid creator+ = uncapped). Mirrors the pricing plan (Free = 720p/~5s).
CAPS = {
    "free":     {"vmax": (1280, 720),  "fmax": 121, "imax": (1280, 1280)},
    "personal": {"vmax": (1920, 1080), "fmax": 241, "imax": (2048, 2048)},
}
# Human labels for the tiers (display only).
TIER_LABEL = {"free": "Free", "personal": "Personal Lite", "creator": "Creator",
              "pro": "Pro", "studio": "Studio"}

# Free fallback when there is no (valid) session. Matches the Worker's free tier.
_FREE = {"tier": "free", "features": ["render", "resume"], "timemachine": False,
         "commercial": False, "watermark": "forced", "exp": 0}



# --- G1 (client-side signature verification) — pinned server Ed25519 public key + stdlib verify -----
# The server signs entitlements with Ed25519; the client verifies with THIS pinned public key. A forged
# or edited session (e.g. hand-set tier=studio) fails verification and is rejected -> Free. Closes the
# "edit ~/.veizik/session.json -> Studio" bypass. Pure-stdlib RFC 8032 verify (no extra dependency).
_ENT_PUB_B64 = "IRmlypKFDLsG2V9w45h8BxZ3Xx8eh8ERPIPzNs1Zq9c="   # entitlement_ed25519.pub (raw, 32 bytes)

import hashlib as _hl
_p = 2**255 - 19
_d = (-121665 * pow(121666, _p-2, _p)) % _p
_I = pow(2, (_p-1)//4, _p)
def _inv(x): return pow(x, _p-2, _p)
def _xrecover(y):
    xx = (y*y-1) * _inv(_d*y*y+1)
    x = pow(xx, (_p+3)//8, _p)
    if (x*x - xx) % _p != 0: x = (x*_I) % _p
    if x % 2 != 0: x = _p-x
    return x
_By = (4 * _inv(5)) % _p; _Bx = _xrecover(_By); _B = [_Bx % _p, _By % _p]
def _edwards(P, Q):
    x1,y1=P; x2,y2=Q
    x3=(x1*y2+x2*y1)*_inv(1+_d*x1*x2*y1*y2)
    y3=(y1*y2+x1*x2)*_inv(1-_d*x1*x2*y1*y2)
    return [x3%_p, y3%_p]
def _scalarmult(P, e):
    if e==0: return [0,1]
    Q=_scalarmult(P, e//2); Q=_edwards(Q,Q)
    if e&1: Q=_edwards(Q,P)
    return Q
def _decodepoint(s):
    y=int.from_bytes(s,"little") & ((1<<255)-1)
    x=_xrecover(y)
    if x & 1 != (s[31]>>7)&1: x=_p-x
    P=[x,y]
    if (-P[0]*P[0]+P[1]*P[1]-1-_d*P[0]*P[0]*P[1]*P[1]) % _p != 0: raise ValueError("bad point")
    return P
def _ed25519_verify(pubkey, msg, sig):
    if len(sig)!=64 or len(pubkey)!=32: return False
    try:
        A=_decodepoint(pubkey); R=_decodepoint(sig[:32])
        S=int.from_bytes(sig[32:],"little")
        h=int.from_bytes(_hl.sha512(sig[:32]+pubkey+msg).digest(),"little")
        return _scalarmult(_B,S)==_edwards(R,_scalarmult(A,h))
    except Exception: return False

def _verify_token(token, pub_b64=_ENT_PUB_B64):
    """Return the signed payload ONLY if its Ed25519 signature verifies against `pub_b64`.
    Also enforces `nbf` (not-before): a token dated in the future (or a rolled-back local clock)
    is rejected. `nbf` is nbf-tolerant — absent => 0 => always valid — so the client can ship
    before the server guarantees the field."""
    try:
        body, sig = token.split(".", 1)
        pub = base64.urlsafe_b64decode(pub_b64)
        s = sig + "=" * (-len(sig) % 4)
        if not _ed25519_verify(pub, body.encode(), base64.urlsafe_b64decode(s)): return None
        b = body + "=" * (-len(body) % 4)
        p = json.loads(base64.urlsafe_b64decode(b).decode())
        nbf = int(p.get("nbf", 0) or 0)
        if nbf and int(time.time()) + _MAX_SKEW_S < nbf: return None   # future-dated / rolled-back clock
        return p
    except Exception:
        return None

def _verify_ed25519_token(token):
    """Backward-compatible alias (verifies against the pinned ENTITLEMENT key)."""
    return _verify_token(token, _ENT_PUB_B64)


# --- device binding (closes cross-device token theft/replay) -----------------------------------------
def device_fingerprint():
    """Stable HWID (soft signals; prod prefers a TPM/Secure-Enclave attested key)."""
    import platform, uuid
    sig = "|".join([platform.node(), platform.machine(), platform.system(),
                    hex(uuid.getnode()), os.environ.get("VZ_GPU_UUID", "")])
    return _hl.sha256(sig.encode()).digest()

def _dev_hex():
    return device_fingerprint().hex()

def _device_ok(payload):
    """True if the token is NOT device-bound (rollout-tolerant) OR is bound to THIS device.
    A captured token replayed on another machine carries someone else's `device` -> rejected -> Free."""
    d = payload.get("device")
    return (not d) or (d == _dev_hex())

# ----------------------------------------------------------------------------- transport
# A real, non-default User-Agent is REQUIRED: Cloudflare's managed rules 403 the stock
# "Python-urllib/*" UA before the request ever reaches the Worker.
_UA = "veizik-cli/0.1 (+https://veizik.com)"


def _http(method, path, body=None, timeout=20):
    url = API_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _load_session():
    try:
        with open(SESSION) as f:
            return json.load(f)
    except Exception:
        return None


def _save_session(sess):
    os.makedirs(os.path.dirname(SESSION), exist_ok=True)
    tmp = SESSION + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sess, f, indent=2)
    os.replace(tmp, SESSION)
    try:
        os.chmod(SESSION, 0o600)
    except OSError:
        pass


# --- anti-rollback anchor + liveness oracle (backup + intentional-offline defense) ------------------
def _hwm_key(sess):                                   # keyed off the server-signed token; rotates per login
    return _hl.sha256(("vzk-hwm|" + (sess.get("token") or "")).encode()).digest()

def _load_hwm(sess):
    try:
        d = json.load(open(_HWM))
        body = json.dumps({k: d[k] for k in ("hwm", "grace_used", "grace_src")}, sort_keys=True).encode()
        if _hmac.compare_digest(d.get("mac", ""), _hmac.new(_hwm_key(sess), body, _hl.sha256).hexdigest()):
            return d
    except Exception:
        pass
    return {"hwm": 0, "grace_used": False, "grace_src": None}

def _save_hwm(sess, d):
    body = json.dumps({k: d[k] for k in ("hwm", "grace_used", "grace_src")}, sort_keys=True).encode()
    d["mac"] = _hmac.new(_hwm_key(sess), body, _hl.sha256).hexdigest()
    try:
        os.makedirs(os.path.dirname(_HWM), exist_ok=True)
        tmp = _HWM + ".tmp"
        with open(tmp, "w") as f: f.write(json.dumps(d))
        os.replace(tmp, _HWM); os.chmod(_HWM, 0o600)
    except OSError:
        pass

def _oracle_confirms_outage(timeout=6):
    """True ONLY if the independent oracle is reachable, its attestation verifies against the pinned
    liveness key, is fresh (<5min), and reports the primary genuinely down. A user who merely blocks
    veizik.com locally cannot trigger this: the oracle probes the primary SERVER-SIDE."""
    if not _LIVE_PUB_B64 or not STATUS_BASE:
        return (False, 0)                                    # oracle not configured yet -> SHORT grace only
    try:
        req = urllib.request.Request(STATUS_BASE + "/live", headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            att = json.loads(r.read().decode()).get("attestation", "")
        p = _verify_token(att, _LIVE_PUB_B64)
        if not p:
            return (False, 0)
        ca = int(p.get("checked_at", 0) or 0)
        if int(time.time()) - ca > _OUTAGE_MAX_AGE:          # stale attestation -> reject (anti-replay)
            return (False, ca)
        return (bool(p.get("primary_down")), ca)
    except Exception:
        return (False, 0)


# ----------------------------------------------------------------------------- entitlement model
class Entitlement:
    def __init__(self, payload, source):
        self.tier = payload.get("tier", "free")
        self.features = payload.get("features") or []
        self.timemachine = payload.get("timemachine", False)
        self.commercial = bool(payload.get("commercial", False))
        self.watermark = payload.get("watermark", "forced")
        self.key_id = payload.get("key_id")
        self.exp = int(payload.get("exp", 0) or 0)
        self.source = source            # 'live' | 'cached' | 'grace' | 'free'

    def allows(self, feature):
        return feature in self.features

    def label(self):
        return TIER_LABEL.get(self.tier, self.tier)

    def clamp(self, w, h, frames, is_video):
        """Clamp requested dims to the tier cap. Returns (w, h, frames, note-or-None)."""
        cap = CAPS.get(self.tier)
        if not cap:
            return w, h, frames, None       # creator/pro/studio: uncapped
        note = []
        if is_video:
            mw, mh = cap["vmax"]
            if w and h and (w > mw or h > mh):
                w, h = min(w or mw, mw), min(h or mh, mh); note.append("%dx%d" % (mw, mh))
            if frames and frames > cap["fmax"]:
                frames = cap["fmax"]; note.append("%df" % cap["fmax"])
        else:
            mw, mh = cap["imax"]
            if w and h and (w > mw or h > mh):
                w, h = min(w or mw, mw), min(h or mh, mh); note.append("%dx%d" % (mw, mh))
        return w, h, frames, (", ".join(note) if note else None)


# ----------------------------------------------------------------------------- public API
def login(api_key, ttl=86400):
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("empty api key")
    resp = _http("POST", "/api/entitlement",
                 {"api_key": api_key, "ttl": ttl, "device_fp": base64.b64encode(device_fingerprint()).decode()})
    if "entitlement" not in resp:
        raise RuntimeError(resp.get("error", "activation failed"))
    ed_token = resp.get("entitlement_ed25519")
    payload = _verify_ed25519_token(ed_token) if ed_token else None
    if not payload:                                        # missing/invalid signature -> refuse to trust
        sys.stderr.write("[veizik] entitlement signature did not verify — refusing (are you on the latest client?)\n")
        raise RuntimeError("entitlement signature verification failed")
    if not _device_ok(payload):                            # bound to a different device -> refuse
        sys.stderr.write("[veizik] entitlement is bound to a different device — refusing\n")
        raise RuntimeError("entitlement device mismatch")
    sess = {"api_key": api_key, "token": ed_token, "payload": payload, "fetched_at": int(time.time())}
    _save_session(sess)
    h = _load_hwm(sess)                                       # a fresh live token is the ONLY thing that
    h["hwm"] = max(int(h.get("hwm", 0)), int(payload.get("iat", 0) or 0), int(time.time()))
    h["grace_used"] = False; h["grace_src"] = None           # ...resets the one-shot SHORT_GRACE budget
    _save_hwm(sess, h)
    return Entitlement(payload, "live")


def logout():
    try:
        os.remove(SESSION)
        return True
    except OSError:
        return False


def resolve(quiet=False):
    """Active Entitlement: signed+unexpired -> cached; expired -> live refresh; refresh-fails ->
    graded grace (one-shot 24h when merely offline, up to 14d only during an ORACLE-CONFIRMED global
    outage); revoked -> Free immediately; rolled-back clock -> Free. NEVER raises, NEVER blocks render."""
    try:
        sess = _load_session()
        if not sess:
            return Entitlement(dict(_FREE), "free")
        payload = _verify_ed25519_token(sess.get("token") or "")
        if not payload or not _device_ok(payload):
            return Entitlement(dict(_FREE), "free")        # tampered/forged/future-dated/other-device -> Free
        now = int(time.time())
        exp = int(payload.get("exp", 0) or 0)
        iat = int(payload.get("iat", 0) or 0)
        h = _load_hwm(sess)
        hwm = max(int(h.get("hwm", 0)), iat)               # signed monotonic time floor
        rolled = now < hwm - _MAX_SKEW_S                   # casual clock rollback detected
        if not rolled and now > hwm:
            h["hwm"] = hwm = now; _save_hwm(sess, h)

        if not rolled and 0 < exp and now < exp:
            return Entitlement(payload, "cached")          # signed, unexpired -> offline OK up to 24h

        # expired OR rollback-forced -> attempt a live refresh against the single primary issuer
        try:
            return login(sess["api_key"])                  # writes fresh session, source='live'
        except urllib.error.HTTPError as e:
            if getattr(e, "code", None) in (401, 402, 403):    # server said NO (revoked/invalid)
                return Entitlement(dict(_FREE), "free")        #   -> Free NOW, zero grace
            # 5xx / 429 / 52x -> treat as an outage below
        except (urllib.error.URLError, OSError, TimeoutError, RuntimeError, ValueError):
            pass                                               # unreachable / bad response -> outage path

        if rolled:                                             # rolled-back clock earns NO grace
            return Entitlement(dict(_FREE), "free")
        confirmed, _ = _oracle_confirms_outage()
        if confirmed:
            if now < exp + OUTAGE_GRACE_S:                     # oracle-confirmed global outage -> long window
                if not quiet:
                    sys.stderr.write("[veizik] confirmed primary outage — extended grace (%s)\n"
                                     % payload.get("tier", "free"))
                return Entitlement(payload, "grace")
        else:
            if now < exp + SHORT_GRACE_S and not h.get("grace_used"):   # merely offline -> one-shot 24h
                h["grace_used"] = True; h["grace_src"] = "short"; _save_hwm(sess, h)
                if not quiet:
                    sys.stderr.write("[veizik] offline — one-time %dh grace (%s)\n"
                                     % (SHORT_GRACE_S // 3600, payload.get("tier", "free")))
                return Entitlement(payload, "grace")
        if not quiet:
            sys.stderr.write("[veizik] entitlement expired and could not refresh — falling back to Free\n")
        return Entitlement(dict(_FREE), "free")
    except Exception:
        return Entitlement(dict(_FREE), "free")            # HARD INVARIANT: resolve() never raises


# ----------------------------------------------------------------------------- watermarking
def _sidecar_marker(out_path, ent):
    """Always-written provenance marker (dependency-free). Metadata-level watermark."""
    try:
        with open(out_path + ".veizik.json", "w") as f:
            json.dump({"engine": "veizik", "tier": ent.tier, "watermark": ent.watermark,
                       "key_id": ent.key_id, "ts": int(time.time())}, f)
    except OSError:
        pass


def _which(x):
    from shutil import which
    return which(x)


def stamp(out_path, ent, is_video):
    """Apply the tier's watermark to a rendered file. forced/weak -> visible + metadata; none ->
    metadata provenance only. Best-effort: needs ffmpeg (video) / Pillow (image) for the VISIBLE mark;
    if absent, the metadata marker is still written and an honest warning is printed."""
    _sidecar_marker(out_path, ent)
    if ent.watermark == "none" or not os.path.exists(out_path):
        return
    text = "veizik.com" if ent.watermark == "weak" else "MADE WITH VEIZIK — FREE (veizik.com)"
    if ent.key_id and ent.watermark == "weak":
        text = "veizik.com · %s" % ent.key_id
    try:
        if is_video:
            _stamp_video(out_path, text)
        else:
            _stamp_image(out_path, text)
    except Exception as e:
        sys.stderr.write("[veizik] visible watermark not applied (%s); metadata marker written. "
                         "Install ffmpeg (video) / Pillow (image) for the visible mark.\n"
                         % type(e).__name__)


def _stamp_video(out_path, text):
    ff = _which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg not found")
    tmp = out_path + ".wm.mp4"
    safe = text.replace(":", "\\:").replace("'", "")
    vf = ("drawtext=text='%s':x=w-tw-12:y=h-th-12:fontsize=h/24:fontcolor=white@0.85:"
          "box=1:boxcolor=black@0.4:boxborderw=6" % safe)
    import subprocess
    r = subprocess.run([ff, "-y", "-i", out_path, "-vf", vf, "-codec:a", "copy",
                        "-metadata", "comment=Made with veizik (veizik.com)", tmp],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError("ffmpeg rc=%d" % r.returncode)
    os.replace(tmp, out_path)


def _stamp_image(out_path, text):
    from PIL import Image, ImageDraw, ImageFont     # optional dep
    im = Image.open(out_path).convert("RGBA")
    W, H = im.size
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    fs = max(14, H // 28)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", fs)
    except Exception:
        font = ImageFont.load_default()
    tw = d.textlength(text, font=font)
    x, y = W - tw - 12, H - fs - 12
    d.rectangle([x - 6, y - 4, x + tw + 6, y + fs + 6], fill=(0, 0, 0, 110))
    d.text((x, y), text, fill=(255, 255, 255, 220), font=font)
    Image.alpha_composite(im, layer).convert("RGB").save(out_path)


# ----------------------------------------------------------------------------- CLI-facing helpers
def status_line(ent):
    src = {"live": "active", "cached": "active", "grace": "offline-grace", "free": "free"}.get(ent.source, ent.source)
    wm = {"forced": "forced watermark", "weak": "light watermark", "none": "no watermark"}.get(ent.watermark, ent.watermark)
    tm = {False: "no", "beta": "beta", True: "yes"}.get(ent.timemachine, str(ent.timemachine))
    exp = ("exp in %dh" % max(0, (ent.exp - int(time.time())) // 3600)) if ent.exp else "-"
    return ("tier=%s (%s) | %s | commercial=%s | timemachine=%s | %s"
            % (ent.label(), src, wm, "yes" if ent.commercial else "no", tm, exp))
