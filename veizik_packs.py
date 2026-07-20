#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
veizik_packs.py — Veizik runtime pack loader + signature verifier (public surface).

Product context
---------------
Veizik ships ONE public bootstrap CLI. There are NO per-tier binaries and no
per-tier installers. What a tier buys is a signed entitlement plus the right to
fetch private runtime packs.

    public binary   CLI parser, updater, doctor, license client, telemetry
                    client, PACK LOADER, SIGNATURE VERIFIER, public Adapter
                    interface.                      <- this module lives here
    private pack    LimML native runtime, advanced kernels, Model Adapters,
                    Capsules, GPU Oracle planner, Recover, TimeMachine, Queue,
                    API bridge.                      <- never in the public binary

Run-time sequence (the only supported order):

    1. verify the license token          (veizik_entitlement.resolve())
    2. read the feature manifest         (fetch_manifest())
    3. download the packs the tier needs (install() / ensure_for_tier())
    4. VERIFY THE PACK SIGNATURE         (verify_manifest_entry())
    5. only then unlock the features     (enabled_features())

Step 4 is not optional and has no bypass flag. A pack that does not verify is
not installed, is not loaded, and unlocks nothing.

Trust model
-----------
Ed25519. The private key is OFFLINE — not in this repo, not on the build worker,
not on the release CDN, not reachable from any network path. Only the 32-byte
PUBLIC key is embedded below, in the client, where an attacker who owns the
download server still cannot forge a pack.

This is a genuinely different trust anchor from veizik_entitlement.py, which
trusts TLS + the server. Here the server is explicitly NOT trusted: a fully
compromised veizik.com can serve any bytes it likes and every one of them will
be refused. That is the entire point of detached signatures.

What is signed
--------------
Not the manifest, and not the metadata alone — the tuple

    (schema tag, pack_id, version, tier, created, sha256-of-the-tar.gz)

serialised as canonical JSON. Binding the archive digest INTO the signed
metadata is what stops the "sign a harmless meta block, swap the payload"
attack; binding pack_id and tier is what stops a valid signature being replayed
into a different pack slot or a lower tier.

Threat model — every one of these is defended in code below
-----------------------------------------------------------
  1. Forged or unverified signature    -> verify_manifest_entry(); no unsigned
                                          path exists, and a missing crypto
                                          library is a REFUSAL, never a skip.
  2. Downgrade to an old vulnerable    -> monotonic version floor kept as a
     pack                                 high-water mark that survives an
                                          uninstall. Overridable only by an
                                          explicit --allow-downgrade.
  3. tar path traversal                -> safe_extract(): rejects absolute
     ("../", "/etc/…", symlink out)       paths, "..", drive letters, symlinks,
                                          hardlinks, devices, setuid bits, and
                                          re-checks the resolved realpath.
  4. pack_id / tier confusion          -> the signed tier is compared against
     (a Free seat installing a Pro pack)  the entitlement BEFORE download, and
                                          the signed pack_id must equal the slot
                                          it is being installed into.
  5. meta/bytes mismatch               -> sha256 of the downloaded bytes is
     (signed meta, swapped archive)       recomputed and compared to the SIGNED
                                          digest before anything is unpacked.
  6. Manifest tampering                -> the manifest itself is untrusted; it
     (server compromised)                 carries no authority. Each entry
                                          stands or falls on its own detached
                                          signature.

Honesty note (read before adding anything to a pack)
----------------------------------------------------
The native engine is NOT distributable yet. The only pack that exists today is
the Creator profile pack, and it contains exactly what its name says: stable
execution profiles (JSON), capsule definitions, and the profile-selection rules
— all real, and all executed by select_profile() below, so the pack is
behaviour rather than decoration. There is no .so, no .cu, and no native runtime
inside it, and this module must never print or imply otherwise. Every
render-duration field reads "measurement in progress"; do not replace those with
estimates, because the entire value of a stable profile is that its numbers came
from a measured run. If a pack claims content it does not carry, that is a bug
in the builder, not a marketing decision.

Component status: pack loader + verifier Shipped; hosted manifest and native
runtime packs Development.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "PACK_PUBKEYS",
    "PACK_PUBKEY_B64",
    "SIG_SCHEMA",
    "MANIFEST_SCHEMA",
    "PackError",
    "SignatureError",
    "crypto_available",
    "manifest_url",
    "fetch_manifest",
    "signing_payload",
    "verify_manifest_entry",
    "verify_sha256",
    "download",
    "safe_extract",
    "install",
    "uninstall",
    "installed",
    "verify_installed",
    "enabled_features",
    "missing_features",
    "ensure_for_tier",
    "list_packs",
    "select_profile",
    "load_profiles",
    "build_creator_pack",
    "tier_rank",
    "doctor",
]

# --------------------------------------------------------------------------- #
# Trust anchor
#
# Raw 32-byte Ed25519 public keys, base64. The matching private key lives only
# at ~/.limlink/secrets/veizik/pack_signing_ed25519.key on the offline signing
# host. Rotating these constants is a CLIENT RELEASE, on purpose: a key rotation
# the server could perform unilaterally would not be a trust anchor at all.
#
# PACK_PUBKEYS is a LIST so a key can be rotated without bricking clients that
# still hold packs signed by the previous key.
#
# Rotation procedure (offline; do not reorder these steps):
#   1. Generate the new keypair on the offline machine. The private half never
#      leaves ~/.limlink/secrets/veizik/ (0600).
#   2. PREPEND the new public key here and ship that CLI build, KEEPING the old
#      key in the list. The fleet must be able to verify BOTH before anything is
#      signed with the new key.
#   3. Wait out the updater's rollout window. Only once the fleet has the new
#      build do you begin signing with the new key.
#   4. Re-sign every still-served pack and publish the updated manifest.
#   5. Remove the old key in a LATER release.
#   A key is never removed in the same release that adds its replacement — that
#   sequence breaks every client that has not yet updated.
#
# key_id is the first 16 hex chars of sha256(raw public key). It is a routing
# hint for choosing which key to try first; it is NOT trusted, and a wrong or
# missing key_id costs only a few extra verification attempts.
# --------------------------------------------------------------------------- #

PACK_PUBKEY_B64 = "68MtVfmvVe9kMK0bqgu4/apYE5bPtqMEat1pRrvWhPw="

PACK_PUBKEYS: List[str] = [
    PACK_PUBKEY_B64,          # veizik pack signing key #1 (2026-07)
]

# Domain separation lives INSIDE the signed JSON object rather than as a byte
# prefix, so "the signature covers a canonical JSON document" stays literally
# true while a signature still cannot be replayed into another context (an
# entitlement token, say) that happens to hash the same field names.
SIG_SCHEMA = "veizik-pack-sig-v1"
MANIFEST_SCHEMA = "veizik-pack-manifest-v1"

DEFAULT_MANIFEST_URL = "https://veizik.com/v1/packs/manifest"
UPGRADE_URL = "https://veizik.com/#pricing"

VEIZIK_HOME = os.path.expanduser(os.environ.get("VEIZIK_HOME", "~/.veizik"))
PACKS_DIR = os.path.join(VEIZIK_HOME, "packs")
LEDGER_PATH = os.path.join(PACKS_DIR, "installed.json")
CACHE_DIR = os.path.join(PACKS_DIR, ".cache")

HTTP_TIMEOUT_S = 30.0
_UA = "veizik-packs/1.0 (+https://veizik.com)"

# Resource ceilings. A profile pack is a few KB; these exist so a hostile or
# corrupt archive cannot exhaust the disk before the digest check has even
# finished. A signature says "we made this", not "this is small", so the caps
# apply to signed content too. Sized for a future native pack, not today's JSON.
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 20000

# Tier lattice. Install is permitted when the seat's rank >= the pack's rank.
# 'starter' is the plan id used by `veizik plans`; 'free' is the entitlement
# tier string. They are the same seat and must rank identically.
TIER_RANK: Dict[str, int] = {
    "free": 0,
    "starter": 0,
    "personal": 1,
    "creator": 2,
    "pro": 3,
    "studio": 4,
}

TIER_LABEL = {
    "free": "Free", "starter": "Starter Preview", "personal": "Personal Lite",
    "creator": "Founding Creator", "pro": "Founding Pro", "studio": "Studio Node",
}

# Feature ids the entitlement layer issues today (veizik_entitlement._FREE and
# the Worker). A pack's `unlocks` MUST be drawn from this shared vocabulary:
# enabled_features() intersects the two, so a pack that invents an id nobody
# grants stays locked forever. That is the fail-closed direction, but it is also
# a silent no-op, which is why the vocabulary is written down here.
KNOWN_FEATURES: Tuple[str, ...] = ("render", "resume", "timemachine", "profiles", "capsule")

_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")
_PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class PackError(Exception):
    """Refusal to install or load a pack.

    Every message is written to be read by a customer at a terminal, not by a
    developer with the source open: it says what was refused and what to do.
    """


class SignatureError(PackError):
    """Signature missing, malformed, unverifiable, or wrong. Always fatal.

    A subclass rather than a flag so a caller can special-case "this is a trust
    failure, not a network hiccup" without string-matching the message.
    """


# --------------------------------------------------------------------------- #
# Small helpers (mirrors veizik_telemetry's fs discipline)
# --------------------------------------------------------------------------- #


def _ensure_dirs() -> None:
    for d in (VEIZIK_HOME, PACKS_DIR, CACHE_DIR):
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


def _shred(path: str) -> None:
    """Delete a rejected download immediately.

    A file that failed its hash or signature check must not survive on disk:
    left in the cache it becomes a resume target for the next run, an input to a
    support script, or simply a confusing artefact during an incident. Unlink is
    enough — the content is attacker-supplied, not secret, so overwriting first
    would buy nothing.
    """
    try:
        os.unlink(path)
    except OSError:
        pass


def tier_rank(tier: Optional[str]) -> int:
    """Unknown tiers rank -1, i.e. below Free.

    An unrecognised tier string on a PACK therefore fails the >= comparison and
    is refused. Failing closed on an unknown tier is the only safe reading: a
    future 'enterprise' pack must not become installable by everyone simply
    because this build has never heard of it.
    """
    return TIER_RANK.get((tier or "").strip().lower(), -1)


def _version_tuple(v: str) -> Tuple[int, ...]:
    """Strict dotted-numeric version, padded to 4 components. Anything else raises.

    Deliberately NOT a general semver parser. Version ordering is a SECURITY
    control here (the downgrade floor), and a version we cannot totally order is
    a version we must not accept: a loose parser that quietly maps "1.0.0-evil"
    onto 1.0.0 would hand an attacker a way to sit exactly at the floor and
    re-serve arbitrary content under a version that passes.

    Padding matters too — without it Python orders (1, 0) < (1, 0, 0), so "1.0"
    would read as older than "1.0.0" and could slip past the floor.
    """
    v = (v or "").strip()
    if not _VERSION_RE.match(v):
        raise PackError(
            "unusable version %r — pack versions must be dotted numbers "
            "(e.g. 1.0.0); anything else cannot be ordered and is refused" % v
        )
    parts = [int(p) for p in v.split(".")]
    return tuple((parts + [0, 0, 0, 0])[:4])


# --------------------------------------------------------------------------- #
# Signature verification (threats 1, 4, 5, 6)
#
# stdlib has no Ed25519, so this needs `cryptography`. When it is absent we
# REFUSE — we do not warn-and-continue. An unverifiable pack and a forged pack
# are indistinguishable from here, so they get the same answer.
#
# There is deliberately no override env var. A "skip verification" flag would
# become the first thing an attacker (or an impatient support script) reaches
# for, and it would silently demote this module to a plain downloader.
# --------------------------------------------------------------------------- #


def crypto_available() -> bool:
    """True when signatures can be checked. Used by doctor(); never used to skip."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
        return True
    except Exception:
        return False


def _ed25519_public_class():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        return Ed25519PublicKey
    except Exception as exc:                 # ImportError, but also broken installs
        raise SignatureError(
            "pack signatures cannot be verified on this machine: the `cryptography` "
            "package is not importable (%s).\n"
            "  Install it with:  python3 -m pip install cryptography\n"
            "  Nothing has been downloaded or installed. veizik refuses to install "
            "an unverified pack rather than trust it." % exc
        )


def _key_id(raw_pub: bytes) -> str:
    return hashlib.sha256(raw_pub).hexdigest()[:16]


def _load_pubkeys() -> List[Tuple[str, bytes]]:
    """Decode PACK_PUBKEYS to [(key_id, raw32)].

    A malformed entry is skipped rather than fatal: one bad constant must not
    disable a working key sitting next to it in the list.
    """
    out: List[Tuple[str, bytes]] = []
    for b64 in PACK_PUBKEYS:
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception:
            continue
        if len(raw) == 32:
            out.append((_key_id(raw), raw))
    return out


# Exactly these fields are signed, in exactly this set. Verification REBUILDS the
# object from parsed fields and re-serialises it, so an attacker cannot smuggle
# an extra key into the signed document and cannot drop one either: a missing
# field is a rejection, not a default.
SIGNED_FIELDS: Tuple[str, ...] = ("schema", "pack_id", "version", "tier", "created", "sha256")


def signing_payload(entry: Dict[str, Any]) -> bytes:
    """The exact canonical bytes the offline signer signs and the client re-derives.

    Canonical JSON: sorted keys, no insignificant whitespace, UTF-8. Both sides
    call THIS function, so the two can never drift — a reimplementation on either
    side is how signature schemes quietly break. Both sides must also build it
    from PARSED fields, never from the received manifest text, so that reordering
    keys, adding a field, or changing whitespace cannot alter what is verified.

    `url`, `size`, `summary` and `unlocks` are NOT signed: they are delivery and
    display metadata. The only thing binding the manifest to actual bytes is
    `sha256`, which is signed and is recomputed from the file on disk before
    anything is extracted (threat 5).
    """
    doc = {
        "schema": SIG_SCHEMA,
        "pack_id": _require_str(entry, "pack_id"),
        "version": _require_str(entry, "version"),
        "tier": _require_str(entry, "tier"),
        "created": _require_str(entry, "created"),
        "sha256": _require_str(entry, "sha256").lower(),
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_signing_bytes(pack_id: str, version: str, tier: str,
                            created: str, sha256: str) -> bytes:
    """Positional form of signing_payload(), kept for the signer and for tests."""
    return signing_payload({"pack_id": pack_id, "version": version, "tier": tier,
                            "created": created, "sha256": sha256})


def _require_str(entry: Dict[str, Any], key: str) -> str:
    val = (entry or {}).get(key)
    if not isinstance(val, str) or not val:
        raise PackError("manifest entry is missing required field %r" % key)
    return val


def _entry_signature(entry: Dict[str, Any]) -> str:
    """Read the detached signature.

    Both "sig" and "signature" are accepted so the manifest schema and the signer
    can be renamed independently without a flag day. Presence is mandatory in
    either spelling.
    """
    for key in ("sig", "signature"):
        val = (entry or {}).get(key)
        if isinstance(val, str) and val:
            return val
    raise SignatureError(
        "pack %s carries no signature. Unsigned packs are never installed, "
        "including from an official-looking URL." % (entry or {}).get("pack_id")
    )


def _validate_entry_shape(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Structural checks that run BEFORE any signature or network work.

    Cheap rejects first: a malformed pack_id or version would otherwise reach the
    filesystem path builder and the version comparator, both of which assume they
    are handling sane values. pack_id in particular becomes a directory name, so
    constraining it here is a second, independent guard against traversal through
    the id itself ("../../etc").
    """
    if not isinstance(entry, dict):
        raise PackError("manifest entry is not an object")

    pack_id = _require_str(entry, "pack_id")
    if not _PACK_ID_RE.match(pack_id):
        raise PackError(
            "malformed pack_id %r — must match %s (pack_id becomes a directory "
            "name, so slashes and dot-dot are refused)" % (pack_id, _PACK_ID_RE.pattern)
        )

    _version_tuple(_require_str(entry, "version"))     # ordering must be possible

    tier = _require_str(entry, "tier")
    if tier_rank(tier) < 0:
        raise PackError(
            "pack %s declares unknown tier %r — refused (this build cannot rank it, "
            "so it cannot decide whether you are entitled to it)" % (pack_id, tier)
        )

    if not _SHA256_RE.match(_require_str(entry, "sha256").lower()):
        raise PackError("pack %s: sha256 is not a 64-hex digest — refused" % pack_id)

    _require_str(entry, "created")
    _entry_signature(entry)                            # raises if absent

    size = entry.get("size")
    if size is not None and (not isinstance(size, int) or size < 0 or size > MAX_DOWNLOAD_BYTES):
        raise PackError("pack %s declares an out-of-range size" % pack_id)
    return entry


def verify_manifest_entry(entry: Dict[str, Any]) -> bool:
    """Verify one entry's detached Ed25519 signature over its canonical payload.

    Returns True only on a real cryptographic match. Every other outcome RAISES:
    a bare False would be far too easy to read as "probably offline, carry on".

    The signature covers pack_id, version, tier, created and the tar.gz sha256.
    Consequences worth stating outright:
      * tier is signed   -> the manifest host cannot relabel a Studio pack as
                            free to trick a lower seat into installing it (4).
      * sha256 is signed -> the host cannot keep a valid signature while swapping
                            the tarball underneath it (5).
      * version is signed-> a replayed OLD entry is still perfectly authentic,
                            which is exactly why downgrade needs its own separate
                            defence; see _check_not_downgrade() (2).

    Threat 6 in one sentence: the manifest is a delivery convenience, not an
    authority. It could be typed by an attacker and this function would still
    reject every entry in it.
    """
    _validate_entry_shape(entry)
    Ed25519PublicKey = _ed25519_public_class()         # raises: fail-closed

    payload = signing_payload(entry)
    try:
        sig = base64.b64decode(_entry_signature(entry), validate=True)
    except SignatureError:
        raise
    except Exception:
        raise SignatureError("pack %s: signature is not valid base64 — refused"
                             % entry.get("pack_id"))
    if len(sig) != 64:
        raise SignatureError(
            "pack %s: signature is %d bytes, expected 64 (Ed25519) — refused"
            % (entry.get("pack_id"), len(sig))
        )

    keys = _load_pubkeys()
    if not keys:
        raise SignatureError("no usable embedded pack signing key in this build")

    hinted = entry.get("key_id")
    if isinstance(hinted, str):                        # hint only, never a filter
        keys.sort(key=lambda kv: kv[0] != hinted)

    for _kid, raw_pub in keys:
        try:
            Ed25519PublicKey.from_public_bytes(raw_pub).verify(sig, payload)
            return True
        except Exception:
            continue

    raise SignatureError(
        "pack %s FAILED signature verification against %d embedded key(s).\n"
        "  The archive metadata was not signed by veizik's offline release key.\n"
        "  This is what a tampered manifest, a swapped archive, or a hostile "
        "mirror looks like. Nothing was installed."
        % (entry.get("pack_id"), len(keys))
    )


def verify_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """verify_manifest_entry() + the SIGNED fields, which are the only trustworthy
    view of an entry.

    Callers that go on to make a decision should use this rather than reading the
    raw entry: everything outside the returned dict (url, size, summary, unlocks)
    is unauthenticated transport metadata — fine for fetching and display, never
    for a decision.
    """
    verify_manifest_entry(entry)
    return {
        "pack_id": entry["pack_id"],
        "version": entry["version"],
        "tier": entry["tier"],
        "created": entry["created"],
        "sha256": entry["sha256"].lower(),
    }


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def manifest_url() -> str:
    """VEIZIK_PACK_MANIFEST overrides the default (a path or a URL).

    An override is safe precisely because the manifest has no authority: point
    the client at a hostile manifest and every entry still has to carry a
    signature from the offline key. That is what makes air-gapped and staging
    installs possible without ever adding a bypass flag.
    """
    return (os.environ.get("VEIZIK_PACK_MANIFEST") or DEFAULT_MANIFEST_URL).strip()


def _licence_key() -> Optional[str]:
    """The active licence key, if the user is logged in.

    The server decides entitlement: it only lists and only streams packs the key's tier covers.
    A signature proves a pack is genuine; it does not prove you are allowed to have it, so paid
    pack bytes are never a public download. Without a key the manifest simply comes back empty.
    """
    try:
        import veizik_entitlement as _ve
        sess = getattr(_ve, "_load_session", None)
        return (sess() or {}).get("api_key") if sess else None
    except Exception:
        return None


def _with_key(url: str) -> str:
    """Attach the licence key to a veizik.com pack URL (ignored for file:// and mirrors)."""
    key = _licence_key()
    if not key or not url.startswith("http") or "veizik.com" not in url:
        return url
    return url + ("&" if "?" in url else "?") + "key=" + urllib.parse.quote(key)


def _read_source(src: str, max_bytes: int) -> bytes:
    """Read a local path, file:// or http(s):// source with a hard size cap.

    Over-long input RAISES rather than truncating. Silently returning the first N
    bytes would hand the caller a corrupt archive that fails its digest check for
    a reason nobody can diagnose.
    """
    parsed = urllib.parse.urlparse(src)
    if parsed.scheme in ("", "file"):
        path = urllib.request.url2pathname(parsed.path) if parsed.scheme == "file" else src
        path = os.path.expanduser(path)
        if os.path.getsize(path) > max_bytes:
            raise PackError("%s is over the %d-byte cap — refused" % (src, max_bytes))
        with open(path, "rb") as fh:
            buf = fh.read(max_bytes + 1)
    elif parsed.scheme in ("http", "https"):
        req = urllib.request.Request(src, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            buf = resp.read(max_bytes + 1)
    else:
        raise PackError("unsupported source scheme %r — refused" % parsed.scheme)

    if len(buf) > max_bytes:
        raise PackError("%s exceeds the %d-byte cap — refused" % (src, max_bytes))
    return buf


def fetch_manifest(source: Optional[str] = None) -> Dict[str, Any]:
    """Fetch + parse the pack manifest.

    Parsing only — no trust is conferred. The returned document is untrusted
    input; every entry must still pass verify_manifest_entry() before it is acted
    on. Raises PackError on transport or parse failure so the CLI can print a
    real reason instead of silently reporting "no packs".
    """
    src = _with_key(source or manifest_url())
    try:
        raw = _read_source(src, MAX_MANIFEST_BYTES)
    except PackError:
        raise
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise PackError(
            "could not read the pack manifest at %s (%s).\n"
            "  Check your connection, or point at a local manifest with "
            "VEIZIK_PACK_MANIFEST=/path/to/manifest.json" % (src, type(exc).__name__)
        )
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise PackError("pack manifest at %s is not valid JSON — refused" % src)
    if not isinstance(doc, dict) or not isinstance(doc.get("packs"), list):
        raise PackError("pack manifest at %s has no 'packs' list — refused" % src)
    doc["_source"] = src
    return doc


def manifest_entries(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every entry that survives shape + signature checks.

    Bad entries are DROPPED rather than raised on: one revoked or malformed entry
    must not stop a user installing the other packs they paid for.
    """
    good: List[Dict[str, Any]] = []
    for entry in (doc or {}).get("packs") or []:
        try:
            if verify_manifest_entry(entry):
                good.append(entry)
        except PackError:
            continue
    return good


def _entry_url(entry: Dict[str, Any], manifest_src: str) -> str:
    """Resolve an entry's archive URL, allowing manifest-relative locations."""
    url = str(entry.get("url") or "")
    if not url:
        raise PackError("pack %s has no url in the manifest" % entry.get("pack_id"))
    if urllib.parse.urlparse(url).scheme:
        return _with_key(url)
    if not urllib.parse.urlparse(manifest_src).scheme:
        return os.path.join(os.path.dirname(os.path.expanduser(manifest_src)), url)
    return _with_key(urllib.parse.urljoin(manifest_src, url))


# --------------------------------------------------------------------------- #
# Ledger — installed state + the downgrade floor (threat 2)
#
# `floor` is a HIGH-WATER MARK, not the installed version: it is raised on every
# successful install and never lowered by uninstall, so "remove the pack, then
# reinstall the vulnerable 1.0.0" is not a bypass.
# --------------------------------------------------------------------------- #


def _ledger() -> Dict[str, Any]:
    doc = _read_json(LEDGER_PATH) or {}
    doc.setdefault("schema", "veizik-installed-v1")
    doc.setdefault("packs", {})
    doc.setdefault("floor", {})
    return doc


def _save_ledger(doc: Dict[str, Any]) -> None:
    _write_json(LEDGER_PATH, doc)


def _floor(pack_id: str) -> Optional[Tuple[int, ...]]:
    raw = _ledger()["floor"].get(pack_id)
    if not raw:
        return None
    try:
        return _version_tuple(str(raw))
    except PackError:
        return None


def _check_not_downgrade(pack_id: str, version: str, sha256: str,
                         allow_downgrade: bool = False) -> None:
    """Refuse to move a pack backwards.

    A downgrade is a valid signature REPLAY: the old entry genuinely was signed
    by us, so signature checking alone can never stop it. Two counters are
    enforced:

      * the currently installed version, and
      * `floor`, the per-pack high-water mark that survives uninstall.

    Same-version reinstall is allowed only when the sha256 matches the recorded
    one. Same version, different bytes is a content swap wearing a signature for
    a version the machine already trusts.

    allow_downgrade exists because rolling back a bad release is a real operator
    scenario — but it must be a deliberate, typed decision (`--allow-downgrade`),
    never something a manifest can trigger on its own.
    """
    if allow_downgrade:
        return

    want = _version_tuple(version)
    cur = _ledger()["packs"].get(pack_id) or {}
    cur_version = cur.get("version")
    if isinstance(cur_version, str):
        try:
            have = _version_tuple(cur_version)
        except PackError:
            have = None
        if have is not None:
            if want < have:
                raise PackError(
                    "downgrade refused: %s %s is older than the installed %s.\n"
                    "  Re-serving an old pack is how a fixed vulnerability comes back.\n"
                    "  If you really intend this, re-run with --allow-downgrade."
                    % (pack_id, version, cur_version)
                )
            if want == have:
                recorded = str(cur.get("sha256") or "")
                if recorded and recorded != sha256.lower():
                    raise PackError(
                        "refused: %s %s is already installed with a DIFFERENT sha256.\n"
                        "  Same version, different bytes means the artefact was replaced.\n"
                        "  Nothing has been changed." % (pack_id, version)
                    )

    floor = _floor(pack_id)
    if floor is not None and want < floor:
        raise PackError(
            "refusing to install %s %s: version %s or newer has already been "
            "installed on this machine (the floor survives uninstall on purpose).\n"
            "  Rolling back to an older pack is how a fixed vulnerability gets "
            "re-introduced, so veizik does not allow it — even with a valid "
            "signature. Use --allow-downgrade to override."
            % (pack_id, version, ".".join(str(p) for p in floor))
        )


def installed() -> Dict[str, Any]:
    """Installed packs, keyed by pack_id. Local state — still verify before use."""
    return dict(_ledger()["packs"])


def pack_path(pack_id: str) -> str:
    return os.path.join(PACKS_DIR, pack_id)


# --------------------------------------------------------------------------- #
# Archive extraction (threat 3)
#
# tarfile will happily write outside the destination if you let it. Python's own
# data_filter (3.12+) covers most of this, but it is not present on every
# interpreter we support, so the checks are explicit and the policy is ours:
# regular files and directories only, nothing else, ever.
#
# That is stricter than today's pack contents (plain JSON) require, deliberately.
# The moment a pack legitimately needs a symlink, someone has to come here and
# think about it, rather than a symlink quietly appearing in a tarball and being
# followed during extraction.
# --------------------------------------------------------------------------- #


def _reject_member(name: str, why: str) -> PackError:
    return PackError(
        "pack archive rejected: member %r %s.\n"
        "  This is a path-traversal attempt or a corrupt archive. Nothing was "
        "written outside the pack directory." % (name, why)
    )


def _check_member(member: tarfile.TarInfo, dest_real: str) -> None:
    """Raise unless this member is provably safe to extract into dest_real."""
    name = member.name or ""

    # Type policy: regular files and directories only. Symlinks and hardlinks are
    # the classic escape (link to /etc/…, then write through it); devices and
    # fifos have no business in a pack at all.
    if member.issym() or member.islnk():
        raise _reject_member(name, "is a symlink/hardlink, which packs may not contain")
    if not (member.isfile() or member.isdir()):
        raise _reject_member(name, "is not a regular file or directory")

    # Name policy, applied to the RAW name before any join.
    if not name or name.startswith("/") or os.path.isabs(name):
        raise _reject_member(name, "is an absolute path")
    if "\\" in name or re.match(r"^[A-Za-z]:", name):
        raise _reject_member(name, "contains a Windows drive/backslash path")
    if "\x00" in name:
        raise _reject_member(name, "contains a NUL byte")
    if any(p == ".." for p in name.split("/") if p):
        raise _reject_member(name, "contains a '..' traversal component")

    # setuid/setgid inside a downloaded archive is never legitimate.
    if (member.mode or 0) & (stat.S_ISUID | stat.S_ISGID):
        raise _reject_member(name, "carries a setuid/setgid bit")

    # Belt and braces: resolve the final path and require containment. Catches
    # anything the name checks above did not anticipate. Note this compares
    # resolved paths, not string prefixes — "/opt/veizik-evil" starts with
    # "/opt/veizik", which is exactly how the naive version of this check fails.
    target = os.path.realpath(os.path.join(dest_real, name))
    if target != dest_real and not target.startswith(dest_real + os.sep):
        raise _reject_member(name, "resolves outside the pack directory")

    if (member.size or 0) < 0 or (member.size or 0) > MAX_UNCOMPRESSED_BYTES:
        raise _reject_member(name, "declares an implausible size")


def safe_extract(tar: tarfile.TarFile, dest: str) -> List[str]:
    """Extract `tar` into `dest`, refusing anything that could write outside it.

    Two passes, on purpose. The first inspects EVERY member and raises before a
    single byte is written, so an archive with one hostile member at the end
    cannot leave a half-extracted tree behind. The second writes.

    Permissions are set here rather than taken from the archive: directories
    0700, files 0600. A pack has no business shipping a world-writable file, and
    honouring archive modes is precisely how that would happen.
    """
    dest_real = os.path.realpath(os.path.abspath(os.path.expanduser(dest)))
    os.makedirs(dest_real, exist_ok=True)

    members = tar.getmembers()
    if len(members) > MAX_MEMBERS:
        raise PackError("pack archive has more than %d entries — refused" % MAX_MEMBERS)

    total = 0
    for member in members:
        _check_member(member, dest_real)
        total += max(0, int(member.size or 0))
        if total > MAX_UNCOMPRESSED_BYTES:
            raise PackError("pack archive expands past the %d-byte cap — refused"
                            % MAX_UNCOMPRESSED_BYTES)

    written: List[str] = []
    for member in members:
        target = os.path.normpath(os.path.join(dest_real, member.name))
        if member.isdir():
            os.makedirs(target, exist_ok=True)
            try:
                os.chmod(target, 0o700)
            except OSError:
                pass
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        src = tar.extractfile(member)
        if src is None:
            raise PackError("pack archive member %r could not be read" % member.name)
        with src, open(target, "wb") as out:
            shutil.copyfileobj(src, out, length=1024 * 1024)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        written.append(target)
    return written


def _file_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_digests(root: str) -> Dict[str, str]:
    """relpath -> sha256 for every file under root.

    Recorded at install time so verify_installed() can detect a file whose
    CONTENT was edited in place — a check a filename inventory would sail
    straight past, and the realistic post-install tampering case.
    """
    out: Dict[str, str] = {}
    for base, _dirs, names in os.walk(root):
        for n in names:
            full = os.path.join(base, n)
            out[os.path.relpath(full, root)] = _file_digest(full)
    return out


def verify_sha256(path: str, expected: str) -> bool:
    """Streaming sha256 comparison.

    compare_digest is used less for timing (the hash is public) than because it
    normalises the ragged cases — None, short strings, mixed case — that a plain
    == on hex gets subtly wrong.
    """
    import hmac as _hmac
    try:
        return _hmac.compare_digest(_file_digest(path), (expected or "").lower())
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


def download(entry: Dict[str, Any], dest: Optional[str] = None,
             manifest_src: Optional[str] = None) -> str:
    """Fetch a pack archive to `dest` (default: the pack cache). Returns the path.

    Streams with a hard byte cap so a hostile or broken host cannot fill the disk
    before the digest check ever runs. On ANY failure the partial file is removed
    — a half-written tarball that a later resumed download "completes" is exactly
    the confusion this avoids.
    """
    _validate_entry_shape(entry)
    url = _entry_url(entry, manifest_src or manifest_url())

    _ensure_dirs()
    if dest is None:
        dest = os.path.join(CACHE_DIR, "%s-%s.tar.gz" % (entry["pack_id"], entry["version"]))
    parent = os.path.dirname(os.path.abspath(dest))
    if parent:
        os.makedirs(parent, exist_ok=True)

    declared = entry.get("size")
    cap = min(int(declared), MAX_DOWNLOAD_BYTES) if isinstance(declared, int) and declared > 0 \
        else MAX_DOWNLOAD_BYTES

    try:
        blob = _read_source(url, cap)
        with open(dest, "wb") as fh:
            fh.write(blob)
    except PackError:
        _shred(dest)
        raise
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        _shred(dest)
        raise PackError("download of pack %s failed: %s" % (entry["pack_id"], exc))

    if isinstance(declared, int) and declared > 0 and len(blob) != declared:
        _shred(dest)
        raise PackError("pack %s: got %d bytes, manifest declared %d"
                        % (entry["pack_id"], len(blob), declared))
    return dest


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #


def install(
    entry: Dict[str, Any],
    tier: str = "free",
    allow_downgrade: bool = False,
    manifest_src: Optional[str] = None,
    keep_archive: bool = False,
) -> Dict[str, Any]:
    """Install ONE verified pack from a manifest entry. Returns the ledger record.

    Order of operations is the security property, so it is fixed:

      1. shape validation  — cheap rejects before any network or disk work
      2. signature         — before the download, so a bad manifest costs nothing
                             and the signed sha256 becomes the contract
      3. tier gate         — on the SIGNED tier; a Free seat asking for the Pro
                             pack must not even cause the download (threat 4)
      4. downgrade check   — a valid signature does not make a replay safe (2)
      5. download
      6. sha256 recompute  — from the bytes actually on disk (5); the manifest's
                             claim about them is not evidence about them
      7. safe extraction   — into a staging dir, so a rejected member never lands
                             in the live pack directory (3)
      8. atomic swap + ledger record

    Anything failing after step 5 shreds the download. The live pack directory is
    touched only once every check has passed, and the previous tree is kept until
    the swap succeeds so a failed activation cannot leave the user with nothing.
    """
    signed = verify_entry(entry)                       # steps 1-2
    eid = signed["pack_id"]

    seat = tier_rank(tier)
    need = tier_rank(signed["tier"])
    if seat < need:                                    # step 3
        raise PackError(
            "%s is a %s pack and your licence is %s.\n"
            "  Upgrade at %s, then run:  veizik activate <YOUR_KEY>"
            % (eid, TIER_LABEL.get(signed["tier"], signed["tier"]),
               TIER_LABEL.get((tier or "free").lower(), tier), UPGRADE_URL)
        )

    _check_not_downgrade(eid, signed["version"], signed["sha256"], allow_downgrade)   # step 4

    _ensure_dirs()
    archive = download(entry, manifest_src=manifest_src)                              # step 5

    if not verify_sha256(archive, signed["sha256"]):                                  # step 6
        _shred(archive)
        raise PackError(
            "%s: downloaded archive does not match its signed digest.\n"
            "  signed   %s\n"
            "  The signature covers the archive hash, so this means the archive was "
            "swapped or corrupted in transit. The download has been deleted and "
            "nothing was installed." % (eid, signed["sha256"])
        )

    staging = tempfile.mkdtemp(prefix=".stage-%s-" % eid, dir=PACKS_DIR)              # step 7
    try:
        with tarfile.open(archive, "r:gz") as tar:
            safe_extract(tar, staging)
        digests = _tree_digests(staging)
        unlocks = _unlocks_from_staging(staging, entry)
    except PackError:
        shutil.rmtree(staging, ignore_errors=True)
        _shred(archive)
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        _shred(archive)
        raise PackError("pack %s could not be extracted: %s" % (eid, exc))

    final = pack_path(eid)                                                            # step 8
    previous = final + ".previous"
    shutil.rmtree(previous, ignore_errors=True)
    try:
        if os.path.isdir(final):
            os.replace(final, previous)
        os.replace(staging, final)
    except OSError as exc:
        # Put the old tree back rather than leaving the user with nothing.
        if os.path.isdir(previous) and not os.path.isdir(final):
            try:
                os.replace(previous, final)
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        _shred(archive)
        raise PackError("pack %s could not be activated: %s" % (eid, exc))
    shutil.rmtree(previous, ignore_errors=True)

    if not keep_archive:
        _shred(archive)

    led = _ledger()
    rec = {
        "pack_id": eid,
        "version": signed["version"],
        "tier": signed["tier"],
        "created": signed["created"],
        "sha256": signed["sha256"],
        # Stored so verify_installed() can re-run the real signature check
        # offline, months later, without refetching the manifest.
        "signature": _entry_signature(entry),
        "key_id": entry.get("key_id"),
        "installed_at": _iso_now(),
        "path": final,
        "files": digests,
        "unlocks": unlocks,
        "summary": str(entry.get("summary") or ""),
    }
    led["packs"][eid] = rec

    prev, now = _floor(eid), _version_tuple(signed["version"])
    if prev is None or now > prev:
        led["floor"][eid] = signed["version"]
    _save_ledger(led)
    return rec


def _unlocks_from_staging(staging: str, entry: Dict[str, Any]) -> List[str]:
    """Read the feature ids a pack claims, from its own pack.json.

    Taken from INSIDE the verified tarball rather than from the manifest entry:
    the manifest's `unlocks` field is unsigned display metadata, while pack.json
    is covered by the signed sha256. When the two disagree, the tarball wins.
    """
    meta = _read_json(os.path.join(staging, "pack.json")) or {}
    if meta.get("pack_id") not in (None, entry["pack_id"]):
        raise PackError(
            "pack %s contains a pack.json declaring a different pack_id (%r) — refused"
            % (entry["pack_id"], meta.get("pack_id"))
        )
    feats = meta.get("unlocks")
    if not isinstance(feats, list):
        feats = entry.get("unlocks") if isinstance(entry.get("unlocks"), list) else []
    return sorted({str(f) for f in feats if isinstance(f, str)})


def uninstall(pack_id: str) -> bool:
    """Remove a pack's files and its ledger record.

    The `floor` entry is intentionally LEFT BEHIND. Clearing it here would turn
    uninstall into a one-command downgrade bypass (threat 2).
    """
    if not _PACK_ID_RE.match(pack_id or ""):
        raise PackError("illegal pack_id %r" % pack_id)
    led = _ledger()
    rec = led["packs"].pop(pack_id, None)
    shutil.rmtree(pack_path(pack_id), ignore_errors=True)
    _save_ledger(led)
    return rec is not None


def verify_installed() -> List[Dict[str, Any]]:
    """Re-verify every installed pack: file contents, then the signature.

    An install-time check is a check at one instant. This re-runs it on demand,
    so a customer can answer "is what I am about to run still what you signed?"
    for themselves, at any time, offline.

    Two independent checks, because they catch different attacks:
      * per-file sha256 against the digests recorded at install time — catches a
        file edited in place after installation, which a filename inventory would
        miss entirely;
      * the stored detached signature re-verified against the embedded public key
        — catches a LEDGER edited to relabel a pack's version or tier (to lower
        the downgrade floor, or to forge a Pro entitlement), since both fields sit
        inside the signed tuple.
    """
    out: List[Dict[str, Any]] = []
    for pack_id, rec in sorted(_ledger()["packs"].items()):
        row: Dict[str, Any] = {"pack_id": pack_id, "version": rec.get("version"),
                               "tier": rec.get("tier"), "ok": False, "reason": "",
                               "signature_checked": False}
        root = rec.get("path") or pack_path(pack_id)
        try:
            if not os.path.isdir(root):
                raise PackError("install directory is missing: %s" % root)

            expected = rec.get("files")
            if not isinstance(expected, dict):
                raise PackError("ledger entry predates content hashing — reinstall this pack")

            actual = _tree_digests(root)
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            if missing:
                raise PackError("%d file(s) missing from the installed pack (e.g. %s)"
                                % (len(missing), missing[0]))
            if extra:
                raise PackError("%d unexpected file(s) inside the installed pack (e.g. %s)"
                                % (len(extra), extra[0]))
            changed = sorted(f for f, d in expected.items() if actual.get(f) != d)
            if changed:
                raise PackError(
                    "%d file(s) modified since installation (e.g. %s) — the pack on disk "
                    "is no longer what was signed" % (len(changed), changed[0])
                )

            verify_manifest_entry({
                "pack_id": pack_id, "version": rec.get("version"), "tier": rec.get("tier"),
                "created": rec.get("created"), "sha256": rec.get("sha256"),
                "signature": rec.get("signature") or "", "key_id": rec.get("key_id"),
            })
            row["signature_checked"] = True
            row["files"] = len(expected)
            row["ok"] = True
        except PackError as exc:
            row["reason"] = str(exc).splitlines()[0]
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# Feature resolution
# --------------------------------------------------------------------------- #


def _entitlement_view(entitlement: Any) -> Dict[str, Any]:
    """Normalise a veizik_entitlement object OR a plain dict into {tier, features}.

    Accepting both keeps this module usable from the CLI (which holds the real
    Entitlement object) and from doctor/tests (which pass a dict) without either
    side having to import the other.
    """
    if entitlement is None:
        return {"tier": "free", "features": []}
    if isinstance(entitlement, dict):
        tier, feats = entitlement.get("tier") or "free", entitlement.get("features") or []
    else:
        tier = getattr(entitlement, "tier", None) or "free"
        feats = getattr(entitlement, "features", None) or []
    return {"tier": str(tier), "features": [str(f) for f in feats if isinstance(f, str)]}


def enabled_features(entitlement: Any) -> List[str]:
    """Features that are BOTH licensed AND physically installed.

    The intersection is the honest answer, and both halves are load-bearing:

      * entitlement without pack  -> paid for, not downloaded yet. The CLI should
        offer to fetch it, not pretend the feature already works.
      * pack without entitlement  -> the bytes are on disk (perhaps cached from a
        previous subscription period) but the licence does not grant them.
        Presence on disk is never permission.

    Also drops features from packs whose tier now outranks the current
    entitlement, which is what a lapsed or downgraded subscription looks like
    locally.
    """
    view = _entitlement_view(entitlement)
    seat = tier_rank(view["tier"])
    licensed = set(view["features"])

    have: set = set()
    for rec in installed().values():
        if tier_rank(rec.get("tier")) > seat:
            continue
        for feat in rec.get("unlocks") or []:
            have.add(str(feat))
    return sorted(have & licensed)


def missing_features(entitlement: Any) -> List[str]:
    """Licensed but not installed — i.e. what `veizik packs sync` would fetch."""
    view = _entitlement_view(entitlement)
    return sorted(set(view["features"]) - set(enabled_features(entitlement)))


def unlocked_features() -> Dict[str, str]:
    """feature -> the pack that provides it, for installed packs only.

    Inventory of what is ON DISK. It answers "is the verified code present?" and
    deliberately does NOT consult the licence — enabled_features() is the gate
    that does both, and it is the one the render path must ask.
    """
    out: Dict[str, str] = {}
    for pack_id, rec in installed().items():
        for feat in rec.get("unlocks") or []:
            out.setdefault(str(feat), pack_id)
    return out


def ensure_for_tier(tier: str, entitlement: Any = None, source: Optional[str] = None,
                    allow_downgrade: bool = False, dry_run: bool = False) -> Dict[str, Any]:
    """Install every manifest pack this tier is entitled to and does not yet have.

    Best-effort per pack: one failure is reported and the rest continue, because
    a single revoked signature must not block an otherwise healthy sync. The
    return value always lists what was skipped and why — a silent partial sync
    leaves the user guessing why a feature is still dark.
    """
    result: Dict[str, Any] = {"tier": tier, "manifest": source or manifest_url(),
                              "installed": [], "skipped": [], "failed": [],
                              "dry_run": bool(dry_run)}

    if not crypto_available():
        result["failed"].append({"pack_id": "*", "error": (
            "`cryptography` is not installed, so pack signatures cannot be verified. "
            "veizik installs no runtime pack in this state. "
            "Run: python3 -m pip install cryptography")})
        return result

    try:
        doc = fetch_manifest(source)
    except PackError as exc:
        result["failed"].append({"pack_id": "*", "error": str(exc)})
        return result

    src = doc.get("_source") or manifest_url()
    seat = tier_rank(tier)
    have = installed()

    # Keep only the newest signed entry per pack_id, so a manifest listing several
    # versions installs the latest rather than whichever came last in the array.
    best: Dict[str, Dict[str, Any]] = {}
    for entry in manifest_entries(doc):
        if tier_rank(entry["tier"]) > seat:
            result["skipped"].append({"pack_id": entry["pack_id"], "reason": "above tier",
                                      "tier": entry["tier"]})
            continue
        cur = best.get(entry["pack_id"])
        if cur is None or _version_tuple(entry["version"]) > _version_tuple(cur["version"]):
            best[entry["pack_id"]] = entry

    for pack_id, entry in sorted(best.items()):
        rec = have.get(pack_id)
        if rec and rec.get("version") == entry["version"] and rec.get("sha256") == entry["sha256"].lower():
            result["skipped"].append({"pack_id": pack_id, "reason": "already current",
                                      "version": rec.get("version")})
            continue
        if dry_run:
            result["installed"].append({"pack_id": pack_id, "version": entry["version"],
                                        "tier": entry["tier"], "planned": True})
            continue
        try:
            got = install(entry, tier=tier, allow_downgrade=allow_downgrade, manifest_src=src)
            result["installed"].append({"pack_id": pack_id, "version": got["version"],
                                        "unlocks": got["unlocks"]})
        except PackError as exc:
            result["failed"].append({"pack_id": pack_id, "error": str(exc)})

    if entitlement is not None:
        result["enabled_features"] = enabled_features(entitlement)
        result["missing_features"] = missing_features(entitlement)
    return result


def list_packs(entitlement_tier: str, source: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every manifest entry, annotated with signature state and eligibility.

    Signature failures are reported per row rather than raised: `pack list` is a
    diagnostic, and a user staring at a broken mirror deserves to SEE which entry
    is broken instead of one opaque error for the whole page. Nothing is
    installed here, so reporting rather than refusing is safe.
    """
    doc = fetch_manifest(source)
    seat = tier_rank(entitlement_tier)
    inst = installed()
    rows: List[Dict[str, Any]] = []
    for entry in doc.get("packs", []):
        if not isinstance(entry, dict):
            continue
        row: Dict[str, Any] = {
            "pack_id": str(entry.get("pack_id") or "?"),
            "version": str(entry.get("version") or "?"),
            "tier": str(entry.get("tier") or "?"),
            "summary": str(entry.get("summary") or ""),
            "unlocks": list(entry.get("unlocks") or []),
            "contains": list(entry.get("contains") or []),
            "signature_ok": False,
            "reason": "",
        }
        try:
            row.update(verify_entry(entry))
            row["signature_ok"] = True
        except PackError as exc:
            row["reason"] = str(exc).splitlines()[0]
        row["eligible"] = bool(row["signature_ok"] and seat >= tier_rank(row["tier"]))
        cur = inst.get(row["pack_id"])
        row["installed_version"] = cur.get("version") if cur else None
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Pack content — the Creator profile pack
#
# This is what a real pack contains TODAY. No native engine, no .so, no .cu:
# those are not distributable yet, and shipping an empty file named like one
# would be a lie told to our own future selves during debugging.
#
# What IS here is genuinely usable: stable execution profiles, capsule
# definitions, and the selection rules that pick a profile from detected
# hardware. select_profile() executes these rules, so the pack is behaviour.
#
# Every duration field reads "measurement in progress". Do not fill these in with
# estimates — the whole point of a stable profile is that its numbers came from a
# measured run.
# --------------------------------------------------------------------------- #

MEASUREMENT_PENDING = "measurement in progress"

CREATOR_PACK_ID = "veizik-creator-profiles"
CREATOR_PACK_TIER = "creator"

CREATOR_PACK_FILES: Dict[str, Any] = {
    "pack.json": {
        "pack_id": CREATOR_PACK_ID,
        "tier": CREATOR_PACK_TIER,
        "kind": "profiles",
        "unlocks": ["profiles", "capsule"],
        "contains": [
            "execution profiles (JSON)",
            "capsule definitions (JSON)",
            "profile selection rules (JSON, evaluated by the public loader)",
        ],
        "does_not_contain": [
            "native LimML runtime",
            "compiled kernels",
            "model weights",
        ],
        "note": ("Configuration and capsule definitions only. The native runtime is "
                 "not distributed in this pack."),
    },
    "profiles.json": {
        "schema": "veizik-profiles-v1",
        "profiles": [
            {
                "id": "safe-12gb",
                "label": "Safe / 12 GB class",
                "min_vram_gb": 10,
                "backend": "diffusers",
                "settings": {"precision": "fp16", "attention": "sdpa",
                             "offload": "sequential", "tile_vae": True, "batch": 1},
                "max_render": {"width": 1280, "height": 720, "frames": 121},
                "expected_wall_s": MEASUREMENT_PENDING,
                "status": "stable",
            },
            {
                "id": "balanced-24gb",
                "label": "Balanced / 24 GB class",
                "min_vram_gb": 22,
                "backend": "diffusers",
                "settings": {"precision": "fp16", "attention": "sdpa",
                             "offload": "model", "tile_vae": False, "batch": 1},
                "max_render": {"width": 1280, "height": 720, "frames": 241},
                "expected_wall_s": MEASUREMENT_PENDING,
                "status": "stable",
            },
            {
                "id": "apple-mps",
                "label": "Apple silicon (MPS)",
                "min_vram_gb": 0,
                "requires": {"vendor": "apple"},
                "backend": "diffusers",
                "settings": {"precision": "fp16", "attention": "sdpa",
                             "offload": "none", "tile_vae": True, "batch": 1},
                "max_render": {"width": 1024, "height": 576, "frames": 97},
                "expected_wall_s": MEASUREMENT_PENDING,
                "status": "stable",
                "note": "Unified memory; VRAM thresholds do not apply the same way.",
            },
            {
                "id": "cpu-fallback",
                "label": "CPU fallback",
                "min_vram_gb": 0,
                "backend": "diffusers",
                "settings": {"precision": "fp32", "attention": "math",
                             "offload": "none", "tile_vae": True, "batch": 1},
                "max_render": {"width": 768, "height": 448, "frames": 49},
                "expected_wall_s": MEASUREMENT_PENDING,
                "status": "last resort",
                "note": "Correctness path, not a performance path.",
            },
        ],
    },
    "capsules.json": {
        "schema": "veizik-capsule-v1",
        "description": ("A capsule pins every input that changes a result, so the same "
                        "capsule reproduces the same render. The fields listed here are "
                        "REQUIRED to be recorded; anything not listed is free to vary."),
        "pinned_fields": [
            "veizik_version", "profile_id", "adapter_id",
            "model_public_id", "model_hash",
            "precision", "quantization", "attention_backend",
            "width", "height", "frames", "steps", "guidance", "scheduler", "seed",
        ],
        "capsules": [
            {
                "id": "t2v-preview-720p",
                "label": "Text to video preview (720p)",
                "status": "experimental",
                "profile_hint": "balanced-24gb",
                "pins": {"width": 1280, "height": 720, "frames": 121, "steps": 30,
                         "scheduler": "default", "guidance": None, "seed": None},
                "note": "Universal t2v path. Timing: " + MEASUREMENT_PENDING,
            },
            {
                "id": "t2i-preview",
                "label": "Text to image preview",
                "status": "experimental",
                "profile_hint": "safe-12gb",
                "pins": {"width": 1024, "height": 1024, "frames": 1, "steps": 30,
                         "scheduler": "default", "guidance": None, "seed": None},
                "note": "Universal t2i path. Timing: " + MEASUREMENT_PENDING,
            },
        ],
    },
    "rules.json": {
        "schema": "veizik-profile-rules-v1",
        "description": ("Ordered rules; first match wins. Predicates are a fixed "
                        "vocabulary evaluated by the public loader — there is no "
                        "expression language and nothing is eval()'d, so a pack cannot "
                        "execute code through its rules."),
        "rules": [
            {"when": {"vendor_is": "apple"}, "profile": "apple-mps"},
            {"when": {"has_gpu": False}, "profile": "cpu-fallback"},
            {"when": {"vram_gb_at_least": 22}, "profile": "balanced-24gb"},
            {"when": {"vram_gb_at_least": 10}, "profile": "safe-12gb"},
        ],
        "default": "cpu-fallback",
    },
}


def _match_rule(when: Dict[str, Any], hw: Dict[str, Any]) -> bool:
    """Evaluate one rule clause against detected hardware.

    Closed vocabulary by design. An UNKNOWN predicate returns False rather than
    being ignored: ignoring it would let an unrecognised, more-restrictive
    condition silently pass, which is the wrong direction to fail.
    """
    for key, want in (when or {}).items():
        if key == "vendor_is":
            if str(hw.get("gpu_vendor") or "").lower() != str(want).lower():
                return False
        elif key == "has_gpu":
            if bool(hw.get("has_gpu", False)) is not bool(want):
                return False
        elif key == "vram_gb_at_least":
            try:
                if float(hw.get("gpu_vram_gb") or 0) < float(want):
                    return False
            except (TypeError, ValueError):
                return False
        elif key == "os_is":
            if str(hw.get("os") or "").lower() != str(want).lower():
                return False
        else:
            return False
    return True


def select_profile(hardware: Dict[str, Any], pack_dir: Optional[str] = None) -> Dict[str, Any]:
    """Pick an execution profile for this machine from an installed pack.

    Reads rules.json/profiles.json out of the INSTALLED pack directory (so the
    content arrived through signature verification), falling back to the
    in-module Creator definitions when no pack is installed. Returns the profile
    plus WHICH rule chose it, because "why did it pick that one" is the first
    question every support ticket asks.
    """
    base = pack_dir or pack_path(CREATOR_PACK_ID)
    rules_doc = _read_json(os.path.join(base, "rules.json")) or CREATOR_PACK_FILES["rules.json"]
    prof_doc = _read_json(os.path.join(base, "profiles.json")) or CREATOR_PACK_FILES["profiles.json"]

    by_id = {p["id"]: p for p in (prof_doc.get("profiles") or [])
             if isinstance(p, dict) and p.get("id")}
    for idx, rule in enumerate(rules_doc.get("rules") or []):
        if _match_rule(rule.get("when") or {}, hardware or {}):
            prof = by_id.get(rule.get("profile"))
            if prof:
                return {"profile": prof, "matched_rule": idx, "source": base}
    return {"profile": by_id.get(rules_doc.get("default")), "matched_rule": None, "source": base}


def load_profiles() -> Optional[Dict[str, Any]]:
    """Read profiles.json out of any installed pack that provides one.

    Returns None when no profile pack is installed — the caller then falls back
    to the public autotuner in limml_universal, which is what a Free seat runs.
    """
    for pack_id in sorted(installed()):
        doc = _read_json(os.path.join(pack_path(pack_id), "profiles.json"))
        if doc:
            doc["_pack_id"] = pack_id
            return doc
    return None


def build_creator_pack(outdir: str, version: str) -> Dict[str, Any]:
    """Materialise the Creator pack as a real, signable tar.gz.

    Writes CREATOR_PACK_FILES to a DETERMINISTIC archive and returns the manifest
    entry skeleton (everything but the signature) for the offline signer.
    Deterministic means fixed mtime, uid/gid 0, sorted names, and mtime=0 in the
    gzip header — so identical inputs produce an identical sha256 on any machine,
    and a rebuild whose hash changed means the CONTENT changed. Without
    reproducibility, "is this the artefact we signed?" is unanswerable.
    """
    outdir = os.path.abspath(os.path.expanduser(outdir))
    os.makedirs(outdir, exist_ok=True)
    archive = os.path.join(outdir, "%s-%s.tar.gz" % (CREATOR_PACK_ID, version))

    import gzip as _gzip

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name in sorted(CREATOR_PACK_FILES):
            body = json.dumps(CREATOR_PACK_FILES[name], ensure_ascii=False,
                              indent=2, sort_keys=True).encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mtime = 0
            info.mode = 0o600
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(body))

    with open(archive, "wb") as fh:
        fh.write(_gzip.compress(raw.getvalue(), compresslevel=9, mtime=0))

    entry = {
        "pack_id": CREATOR_PACK_ID,
        "version": version,
        "tier": CREATOR_PACK_TIER,
        "created": _iso_now(),
        "sha256": _file_digest(archive),
        "url": "https://veizik.com/v1/packs/%s-%s.tar.gz" % (CREATOR_PACK_ID, version),
        "size": os.path.getsize(archive),
        "unlocks": CREATOR_PACK_FILES["pack.json"]["unlocks"],
        "contains": CREATOR_PACK_FILES["pack.json"]["contains"],
        "summary": "Stable execution profiles, capsule definitions and selection rules.",
    }
    return {"archive": archive, "entry": entry,
            "signing_payload": signing_payload(entry).decode("utf-8")}


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #


def doctor() -> Dict[str, Any]:
    """Diagnostics for `veizik doctor`. Never raises.

    Reports the verification posture honestly, including the case where nothing
    can be installed at all — a doctor that hides a fail-closed state is worse
    than no doctor.
    """
    keys = _load_pubkeys()
    ok = crypto_available()
    return {
        "manifest_url": manifest_url(),
        "packs_dir": PACKS_DIR,
        "signature_backend": "cryptography Ed25519" if ok else "MISSING",
        "signature_verification": "available" if ok else "UNAVAILABLE (installs refused)",
        "embedded_keys": [{"key_id": kid, "b64": base64.b64encode(raw).decode()}
                          for kid, raw in keys],
        "installed_packs": [
            {"pack_id": k, "version": v.get("version"), "tier": v.get("tier"),
             "unlocks": v.get("unlocks"), "sha256": v.get("sha256")}
            for k, v in sorted(installed().items())
        ],
        "version_floor": _ledger().get("floor") or {},
        "feature_vocabulary": list(KNOWN_FEATURES),
        "notes": [
            "Packs are verified with an offline Ed25519 key; there is no bypass flag.",
            "Downgrades are refused against both the installed version and a "
            "persistent high-water floor that survives uninstall.",
            "Render-time figures in pack content read '%s' until measured." % MEASUREMENT_PENDING,
        ],
    }


def status_text() -> str:
    d = doctor()
    lines = [
        "veizik packs",
        "  manifest          : %s" % d["manifest_url"],
        "  packs dir         : %s" % d["packs_dir"],
        "  signature verify  : %s" % d["signature_verification"],
        "  trusted keys      : %s" % (", ".join(k["key_id"] for k in d["embedded_keys"]) or "-"),
        "",
        "  Installed packs:",
    ]
    if d["installed_packs"]:
        for p in d["installed_packs"]:
            lines.append("    - %s %s  [tier=%s]  unlocks: %s"
                         % (p["pack_id"], p["version"], p["tier"],
                            ", ".join(p["unlocks"] or []) or "-"))
    else:
        lines.append("    (none)")
    lines += ["", "  " + "\n  ".join(d["notes"])]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI entry / self-check
#
# `sign` is an OPERATOR-ONLY tool and lives inside __main__ rather than at module
# scope: the shipped CLI imports this module and must not expose a signing entry
# point at all. It reads the private key from the offline secrets path and fails
# loudly if it is absent. That path is referenced nowhere else in this file.
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys

    PRIVKEY_PATH = os.path.expanduser("~/.limlink/secrets/veizik/pack_signing_ed25519.key")

    def _sign_entry_offline(entry: Dict[str, Any]) -> Dict[str, Any]:
        """Sign a manifest entry with the OFFLINE private key. Signing host only."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if not os.path.exists(PRIVKEY_PATH):
            raise SystemExit(
                "no private signing key at %s.\n"
                "This command only runs on the offline signing machine. The key must "
                "never be copied into a repo, a worker, CI, or a build image."
                % PRIVKEY_PATH
            )
        with open(PRIVKEY_PATH, "r", encoding="utf-8") as fh:
            raw = base64.b64decode(fh.read().strip(), validate=True)
        if len(raw) != 32:
            raise SystemExit("private key is not a raw 32-byte Ed25519 seed")
        priv = Ed25519PrivateKey.from_private_bytes(raw)
        pub = priv.public_key().public_bytes_raw()

        signed = dict(entry)
        signed["sig"] = base64.b64encode(priv.sign(signing_payload(entry))).decode()
        signed["key_id"] = _key_id(pub)
        if base64.b64encode(pub).decode() not in PACK_PUBKEYS:
            print("WARNING: this key's public half is NOT in PACK_PUBKEYS; clients "
                  "will reject the result.", file=sys.stderr)
        return signed

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        print(status_text())

    elif cmd == "doctor":
        print(json.dumps(doctor(), indent=2, ensure_ascii=False))

    elif cmd == "list":
        print(json.dumps(list_packs(sys.argv[2] if len(sys.argv) > 2 else "free"),
                         indent=2, ensure_ascii=False))

    elif cmd == "installed":
        print(json.dumps(installed(), indent=2, ensure_ascii=False))

    elif cmd == "verify":
        print(json.dumps(verify_installed(), indent=2, ensure_ascii=False))

    elif cmd == "build":
        print(json.dumps(build_creator_pack(sys.argv[2] if len(sys.argv) > 2 else "./dist",
                                            sys.argv[3] if len(sys.argv) > 3 else "1.0.0"),
                         indent=2, ensure_ascii=False))

    elif cmd == "sign":
        # usage: veizik_packs.py sign <entry.json>   (offline signing machine only)
        with open(sys.argv[2], "r", encoding="utf-8") as fh:
            print(json.dumps(_sign_entry_offline(json.load(fh)), indent=2, ensure_ascii=False))

    elif cmd == "selftest":
        # --- canonical bytes are stable and field-order independent ------------
        a = canonical_signing_bytes("p", "1.0.0", "creator", "2026-01-01T00:00:00Z", "ab" * 32)
        assert b'"schema":"veizik-pack-sig-v1"' in a
        assert a == signing_payload({"sha256": "AB" * 32, "created": "2026-01-01T00:00:00Z",
                                     "tier": "creator", "version": "1.0.0", "pack_id": "p",
                                     "extra": "ignored"}), "key order/extras must not matter"
        assert b"ignored" not in a, "unsigned fields must not enter the payload"

        # --- version ordering is total, or it is refused (threat 2 foundation) --
        assert _version_tuple("1.2.3") == (1, 2, 3, 0)
        assert _version_tuple("1.0") == _version_tuple("1.0.0")
        assert _version_tuple("1.2.10") > _version_tuple("1.2.9")
        for bad in ("1.0.0-rc1", "v1.0", "", "1.0.0+evil", "latest"):
            try:
                _version_tuple(bad)
                raise AssertionError("accepted unorderable version %r" % bad)
            except PackError:
                pass

        # --- tier lattice is fail-closed (threat 4) ----------------------------
        assert tier_rank("enterprise") == -1 and tier_rank(None) == -1
        assert tier_rank("free") == tier_rank("starter") < tier_rank("creator") < tier_rank("pro")

        # --- threat 1: unsigned and forged entries are both refused ------------
        base_entry = {"pack_id": "x", "version": "1.0.0", "tier": "creator",
                      "created": "2026-01-01T00:00:00Z", "sha256": "ab" * 32}
        try:
            verify_manifest_entry(dict(base_entry))
            raise AssertionError("unsigned entry accepted")
        except SignatureError as e:
            assert "no signature" in str(e)
        try:
            verify_manifest_entry(dict(base_entry, sig=base64.b64encode(b"\x00" * 64).decode()))
            raise AssertionError("forged signature accepted")
        except SignatureError as e:
            assert "FAILED signature verification" in str(e)
        # A malformed pack_id must be refused before any crypto runs.
        try:
            verify_manifest_entry(dict(base_entry, pack_id="../../etc/passwd", sig="AA=="))
            raise AssertionError("traversal pack_id accepted")
        except PackError:
            pass

        # --- threat 3: path traversal, in every shape --------------------------
        dest = os.path.realpath(tempfile.mkdtemp(prefix="veizik-safe-"))
        for bad in ("../evil", "/etc/passwd", "a/../../evil", "..\\evil", "C:\\evil"):
            info = tarfile.TarInfo(bad)
            info.size = 0
            try:
                _check_member(info, dest)
                raise AssertionError("accepted unsafe member %r" % bad)
            except PackError:
                pass
        for bad_type, label in ((tarfile.SYMTYPE, "symlink"), (tarfile.LNKTYPE, "hardlink"),
                                (tarfile.CHRTYPE, "device"), (tarfile.FIFOTYPE, "fifo")):
            info = tarfile.TarInfo("x")
            info.type = bad_type
            info.linkname = "/etc/passwd"
            try:
                _check_member(info, dest)
                raise AssertionError("accepted %s member" % label)
            except PackError:
                pass
        setuid = tarfile.TarInfo("s")
        setuid.mode = 0o4755
        try:
            _check_member(setuid, dest)
            raise AssertionError("accepted setuid member")
        except PackError:
            pass
        ok_member = tarfile.TarInfo("sub/pack.json")
        ok_member.size = 10
        _check_member(ok_member, dest)                 # must not raise

        # --- the pack is real: builds reproducibly, extracts, drives a decision -
        tmp = tempfile.mkdtemp(prefix="veizik-pack-")
        built = build_creator_pack(tmp, "1.0.0")
        assert os.path.getsize(built["archive"]) > 0
        assert build_creator_pack(tmp, "1.0.0")["entry"]["sha256"] == built["entry"]["sha256"], \
            "build must be reproducible or signatures cannot be audited"
        assert verify_sha256(built["archive"], built["entry"]["sha256"])
        assert not verify_sha256(built["archive"], "b" * 64)

        stage = tempfile.mkdtemp(prefix="veizik-extract-")
        with tarfile.open(built["archive"], "r:gz") as t:
            files = safe_extract(t, stage)
        assert os.path.exists(os.path.join(stage, "pack.json"))
        assert all(os.path.getsize(f) > 0 for f in files)
        assert "profiles" in (_read_json(os.path.join(stage, "pack.json")) or {})["unlocks"]

        assert select_profile({"has_gpu": True, "gpu_vendor": "nvidia", "gpu_vram_gb": 24},
                              stage)["profile"]["id"] == "balanced-24gb"
        assert select_profile({"has_gpu": True, "gpu_vendor": "nvidia", "gpu_vram_gb": 12},
                              stage)["profile"]["id"] == "safe-12gb"
        assert select_profile({"has_gpu": False}, stage)["profile"]["id"] == "cpu-fallback"
        assert select_profile({"has_gpu": True, "gpu_vendor": "apple", "gpu_vram_gb": 0},
                              stage)["profile"]["id"] == "apple-mps"

        # --- no vaporware in shipped pack content ------------------------------
        blob = json.dumps(CREATOR_PACK_FILES)
        assert blob.count(MEASUREMENT_PENDING) >= 5, "timings must stay unmeasured, not invented"
        assert ".so" not in blob and ".cu" not in blob, "packs must not claim native binaries"
        for feat in CREATOR_PACK_FILES["pack.json"]["unlocks"]:
            assert feat in KNOWN_FEATURES, "pack unlocks an id nothing can grant: %r" % feat

        # --- entitlement intersection is fail-closed on both sides -------------
        assert enabled_features(None) == []
        assert enabled_features({"tier": "creator", "features": []}) == []

        # --- live signing round trip, when the offline key is present ----------
        if os.path.exists(PRIVKEY_PATH) and crypto_available():
            signed = _sign_entry_offline(built["entry"])
            assert verify_manifest_entry(signed) is True
            for label, mutated in (("sha256 swap (threat 5)", dict(signed, sha256="b" * 64)),
                                   ("tier relabel (threat 4)", dict(signed, tier="free")),
                                   ("version replay (threat 2)", dict(signed, version="0.0.1"))):
                try:
                    verify_manifest_entry(mutated)
                    raise AssertionError("%s survived verification" % label)
                except SignatureError:
                    pass
            print("selftest OK (including live signing round trip)")
        else:
            print("selftest OK (signing key absent — round trip skipped)")

        for d in (tmp, stage, dest):
            shutil.rmtree(d, ignore_errors=True)

    else:
        print("usage: veizik_packs.py status|doctor|list|installed|verify|build|sign|selftest")
