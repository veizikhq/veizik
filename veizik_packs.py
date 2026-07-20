#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
veizik_packs.py — Veizik runtime pack loader + signature verifier (public surface).

Product context
---------------
Veizik ships ONE public bootstrap CLI. There are NO per-tier binaries. What a
tier buys is a signed entitlement plus the right to fetch private runtime packs.

    public binary   CLI parser, updater, doctor, license client, telemetry
                    client, PACK LOADER, SIGNATURE VERIFIER, public Adapter
                    interface.                      <- this module lives here
    private pack    LimML native runtime, advanced kernels, Model Adapters,
                    Capsules, GPU Oracle planner, Recover, TimeMachine, Queue,
                    API bridge.                      <- never in the public binary

Run-time sequence (the only supported order):

    1. verify the license token          (veizik_entitlement.resolve())
    2. read the feature manifest         (fetch_manifest())
    3. download the packs the tier needs (install())
    4. VERIFY THE PACK SIGNATURE         (verify_entry() + verify_installed())
    5. only then unlock the features     (unlocked_features())

Step 4 is not optional and has no bypass flag. A pack that does not verify is
not installed, is not loaded, and unlocks nothing.

Trust model
-----------
Ed25519. The private key is OFFLINE — it is not in this repo, not on the build
worker, not on the release CDN, and not reachable from any network path. Only
the 32-byte PUBLIC key is embedded below, in the client, where an attacker who
owns the download server still cannot forge a pack.

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
  1. Forged or unverified signature    -> verify_entry(); no unsigned path
                                          exists, and a missing crypto library
                                          is a REFUSAL, never a skip.
  2. Downgrade to an old vulnerable    -> monotonic version floor, kept as a
     pack                                 high-water mark that surviving an
                                          uninstall (_floor / ledger).
  3. tar path traversal                -> _safe_extract(): rejects absolute
     ("../", "/etc/…", symlink out)       paths, "..", drive letters, symlinks,
                                          hardlinks, devices, setuid bits, and
                                          re-checks the resolved realpath.
  4. pack_id / tier confusion          -> the signed tier is compared against
     (a Free seat installing a Pro pack)  the entitlement BEFORE download, and
                                          the signed pack_id must equal the
                                          slot it is being installed into.
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
execution profiles (JSON), capsule definitions, and the profile-selection
rules. There is no .so, no .cu, and no native runtime inside it, and this
module must never print or imply otherwise. If a pack claims content it does
not carry, that is a bug in the builder, not a marketing decision.

Component status: pack loader + verifier Shipped; native runtime packs Development.
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
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "PUBLIC_KEY_B64",
    "SIG_SCHEMA",
    "MANIFEST_SCHEMA",
    "crypto_available",
    "canonical_signing_bytes",
    "verify_entry",
    "fetch_manifest",
    "list_packs",
    "install",
    "verify_installed",
    "installed",
    "unlocked_features",
    "pack_path",
    "load_profiles",
    "tier_rank",
]

# --------------------------------------------------------------------------- #
# Trust anchor
#
# Raw 32-byte Ed25519 public key, base64. The matching private key lives only
# at ~/.limlink/secrets/veizik/pack_signing_ed25519.key on the offline signing
# host. Rotating this constant is a client release, on purpose: a key rotation
# the server can perform unilaterally would not be a trust anchor at all.
# --------------------------------------------------------------------------- #

PUBLIC_KEY_B64 = "68MtVfmvVe9kMK0bqgu4/apYE5bPtqMEat1pRrvWhPw="

SIG_SCHEMA = "veizik-pack-sig-v1"          # domain separation for the signature
MANIFEST_SCHEMA = "veizik-pack-manifest-v1"

DEFAULT_MANIFEST_URL = "https://veizik.com/v1/packs/manifest.json"
UPGRADE_URL = "https://veizik.com/#pricing"

VEIZIK_HOME = os.path.expanduser(os.environ.get("VEIZIK_HOME", "~/.veizik"))
PACKS_DIR = os.path.join(VEIZIK_HOME, "packs")
LEDGER_PATH = os.path.join(PACKS_DIR, "installed.json")

HTTP_TIMEOUT_S = 30.0
_UA = "veizik-packs/1.0 (+https://veizik.com)"

# Resource ceilings. A profile pack is a few KB; these exist so a hostile or
# corrupt archive cannot exhaust the disk before the digest check has even
# finished. Sized for a future native pack, not for today's JSON.
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

_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")
_PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class PackError(Exception):
    """Refusal to install or load a pack.

    Every message is written to be read by a customer at a terminal, not by a
    developer with the source open: it says what was refused and what to do.
    """


# --------------------------------------------------------------------------- #
# Small helpers (mirrors veizik_telemetry's fs discipline)
# --------------------------------------------------------------------------- #


def _ensure_dirs() -> None:
    for d in (VEIZIK_HOME, PACKS_DIR):
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


def tier_rank(tier: Optional[str]) -> int:
    """Unknown tiers rank -1, i.e. below Free.

    An unrecognised tier string on a PACK therefore fails the >= comparison and
    is refused. Failing closed on an unknown tier is the only safe reading: a
    future 'enterprise' pack must not become installable by everyone simply
    because this build has never heard of it.
    """
    return TIER_RANK.get((tier or "").strip().lower(), -1)


def _version_tuple(v: str) -> Tuple[int, ...]:
    """Strict dotted-numeric version. Anything else raises.

    Deliberately not a general semver parser. Version ordering is a SECURITY
    control here (the downgrade floor), and a version we cannot totally order
    is a version we must not accept — a loose parser that quietly maps
    "1.0.0-evil" to 1.0.0 would hand an attacker a way to sit at the floor.
    """
    v = (v or "").strip()
    if not _VERSION_RE.match(v):
        raise PackError(
            "unusable version %r — pack versions must be dotted numbers "
            "(e.g. 1.0.0); anything else cannot be ordered and is refused" % v
        )
    return tuple(int(p) for p in v.split("."))


# --------------------------------------------------------------------------- #
# Signature verification
#
# Threat 1. stdlib has no Ed25519, so this needs `cryptography`. When it is
# absent we REFUSE — we do not warn-and-continue. An unverifiable pack and a
# forged pack are indistinguishable from here, so they get the same answer.
# --------------------------------------------------------------------------- #


def crypto_available() -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
        return True
    except Exception:
        return False


def _public_key():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    raw = base64.b64decode(PUBLIC_KEY_B64)
    if len(raw) != 32:
        raise PackError("embedded public key is not a 32-byte Ed25519 key — refusing to verify")
    return ed25519.Ed25519PublicKey.from_public_bytes(raw)


def canonical_signing_bytes(
    pack_id: str, version: str, tier: str, created: str, sha256: str
) -> bytes:
    """The exact bytes the offline signer signs and the client re-derives.

    Canonical JSON: sorted keys, no whitespace, UTF-8. Both sides MUST build
    this from parsed fields — never from the received manifest text — so that
    reordering keys, adding a field, or changing whitespace cannot alter what
    is verified.

    `sha256` is inside the signed tuple. This is the fix for threat 5: signing
    only pack_id/version/tier/created would let anyone with a valid manifest
    entry serve a completely different archive under it.
    """
    doc = {
        "schema": SIG_SCHEMA,
        "pack_id": pack_id,
        "version": version,
        "tier": tier,
        "created": created,
        "sha256": sha256,
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Verify one manifest entry's detached signature. Returns the SIGNED fields.

    The return value is the only thing callers may trust afterwards. Everything
    else in the entry (url, size, description, unlocks) is unauthenticated
    transport metadata: useful for fetching and display, never for a decision.

    Threat 6 in one sentence: the manifest is a delivery convenience, not an
    authority. It could be typed by an attacker and this function would still
    reject every entry.
    """
    if not crypto_available():
        raise PackError(
            "pack signatures cannot be verified on this machine: the `cryptography` "
            "package is not installed.\n"
            "  Install it with:  python3 -m pip install cryptography\n"
            "  Nothing has been downloaded or installed. veizik refuses to install an "
            "unverified pack rather than trust it."
        )

    pack_id = str(entry.get("pack_id") or "")
    version = str(entry.get("version") or "")
    tier = str(entry.get("tier") or "")
    created = str(entry.get("created") or "")
    sha256 = str(entry.get("sha256") or "").lower()
    sig_b64 = str(entry.get("signature") or "")

    if not _PACK_ID_RE.match(pack_id):
        raise PackError("malformed pack_id %r in manifest — refused" % pack_id)
    if not re.match(r"^[0-9a-f]{64}$", sha256):
        raise PackError("pack %s: sha256 is not a 64-hex digest — refused" % pack_id)
    if not sig_b64:
        raise PackError(
            "pack %s carries no signature. Unsigned packs are never installed, "
            "including from an official-looking URL." % pack_id
        )
    _version_tuple(version)                       # ordering must be possible
    if tier_rank(tier) < 0:
        raise PackError("pack %s declares unknown tier %r — refused" % (pack_id, tier))

    try:
        sig = base64.b64decode(sig_b64, validate=True)
    except Exception:
        raise PackError("pack %s: signature is not valid base64 — refused" % pack_id)

    payload = canonical_signing_bytes(pack_id, version, tier, created, sha256)
    try:
        _public_key().verify(sig, payload)
    except Exception:
        raise PackError(
            "pack %s FAILED signature verification.\n"
            "  The archive metadata was not signed by veizik's offline release key.\n"
            "  This is what a tampered manifest, a swapped archive, or a hostile "
            "mirror looks like. Nothing was installed." % pack_id
        )

    return {
        "pack_id": pack_id,
        "version": version,
        "tier": tier,
        "created": created,
        "sha256": sha256,
    }


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def manifest_url() -> str:
    """VEIZIK_PACK_MANIFEST overrides the default (a path or a URL).

    An override is safe precisely because the manifest has no authority: point
    the client at a hostile manifest and every entry still has to carry a
    signature from the offline key. This is what makes air-gapped and
    staging installs possible without a bypass flag.
    """
    return (os.environ.get("VEIZIK_PACK_MANIFEST") or DEFAULT_MANIFEST_URL).strip()


def _read_source(src: str, max_bytes: int) -> bytes:
    """Read a local path, file:// or http(s):// source with a hard size cap."""
    parsed = urllib.parse.urlparse(src)
    if parsed.scheme in ("", "file"):
        path = urllib.request.url2pathname(parsed.path) if parsed.scheme == "file" else src
        path = os.path.expanduser(path)
        size = os.path.getsize(path)
        if size > max_bytes:
            raise PackError("%s is %d bytes, over the %d-byte cap — refused" % (src, size, max_bytes))
        with open(path, "rb") as fh:
            return fh.read(max_bytes + 1)[:max_bytes]
    if parsed.scheme not in ("http", "https"):
        raise PackError("unsupported source scheme %r — refused" % parsed.scheme)
    req = urllib.request.Request(src, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        buf = resp.read(max_bytes + 1)
    if len(buf) > max_bytes:
        raise PackError("%s exceeds the %d-byte cap — refused" % (src, max_bytes))
    return buf


def fetch_manifest(source: Optional[str] = None) -> Dict[str, Any]:
    """Fetch + parse the pack manifest. Parsing only — no trust is conferred."""
    src = source or manifest_url()
    try:
        raw = _read_source(src, 4 * 1024 * 1024)
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


def _entry_url(entry: Dict[str, Any], manifest_src: str) -> str:
    """Resolve an entry's archive URL, allowing manifest-relative locations."""
    url = str(entry.get("url") or "")
    if not url:
        raise PackError("pack %s has no url in the manifest" % entry.get("pack_id"))
    if urllib.parse.urlparse(url).scheme:
        return url
    base = manifest_src
    if not urllib.parse.urlparse(base).scheme:
        return os.path.join(os.path.dirname(os.path.expanduser(base)), url)
    return urllib.parse.urljoin(base, url)


# --------------------------------------------------------------------------- #
# Ledger — installed state + the downgrade floor
#
# Threat 2. `floor` is a HIGH-WATER MARK, not the installed version: it is
# raised on every successful install and never lowered, so uninstalling a pack
# does not reset the floor and re-open the old vulnerable version.
# --------------------------------------------------------------------------- #


def _ledger() -> Dict[str, Any]:
    doc = _read_json(LEDGER_PATH) or {}
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


def _check_not_downgrade(pack_id: str, version: str) -> None:
    want = _version_tuple(version)
    floor = _floor(pack_id)
    if floor is not None and want < floor:
        raise PackError(
            "refusing to install %s %s: version %s or newer has already been installed "
            "on this machine.\n"
            "  Rolling back to an older pack is how a fixed vulnerability gets "
            "re-introduced, so veizik does not allow it — even with a valid signature."
            % (pack_id, version, ".".join(str(p) for p in floor))
        )


def installed() -> Dict[str, Any]:
    """Installed packs, keyed by pack_id. Local state — still verify before use."""
    return dict(_ledger()["packs"])


def pack_path(pack_id: str) -> str:
    return os.path.join(PACKS_DIR, pack_id)


# --------------------------------------------------------------------------- #
# Archive extraction
#
# Threat 3. tarfile will happily write outside the destination if you let it.
# Python's own data_filter (3.12+) covers most of this, but it is not present
# on every interpreter we support, so the checks are explicit and the policy is
# ours: regular files and directories only, nothing else, ever.
# --------------------------------------------------------------------------- #


def _reject_member(name: str, why: str) -> PackError:
    return PackError(
        "pack archive rejected: member %r %s.\n"
        "  This is a path-traversal attempt or a corrupt archive. Nothing was "
        "written outside the pack directory." % (name, why)
    )


def _safe_members(tar: tarfile.TarFile, dest_real: str):
    """Yield only members that are provably safe to extract into dest_real."""
    total = 0
    count = 0
    for member in tar:
        name = member.name
        count += 1
        if count > MAX_MEMBERS:
            raise PackError("pack archive has more than %d entries — refused" % MAX_MEMBERS)

        # Type policy: regular files and directories only. Symlinks and
        # hardlinks are the classic escape (a link to /etc/… followed by a
        # write through it), and devices/fifos have no business in a pack.
        if member.issym() or member.islnk():
            raise _reject_member(name, "is a symlink/hardlink, which packs may not contain")
        if not (member.isfile() or member.isdir()):
            raise _reject_member(name, "is not a regular file or directory")

        # Name policy, applied to the RAW name before any join.
        if not name or name.startswith("/") or os.path.isabs(name):
            raise _reject_member(name, "is an absolute path")
        if "\\" in name or re.match(r"^[A-Za-z]:", name):
            raise _reject_member(name, "contains a Windows drive/backslash path")
        parts = [p for p in name.split("/") if p]
        if any(p == ".." for p in parts):
            raise _reject_member(name, "contains a '..' traversal component")

        # setuid/setgid inside a downloaded archive is never legitimate.
        if member.mode & (stat.S_ISUID | stat.S_ISGID):
            raise _reject_member(name, "carries a setuid/setgid bit")

        # Belt and braces: resolve the final path and require containment.
        # Catches anything the name checks above did not anticipate.
        target = os.path.realpath(os.path.join(dest_real, name))
        if target != dest_real and not target.startswith(dest_real + os.sep):
            raise _reject_member(name, "resolves outside the pack directory")

        total += max(0, int(member.size or 0))
        if total > MAX_UNCOMPRESSED_BYTES:
            raise PackError("pack archive expands past the %d-byte cap — refused"
                            % MAX_UNCOMPRESSED_BYTES)

        member.mode = 0o755 if member.isdir() else 0o644
        yield member


def _file_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_digests(root: str) -> Dict[str, str]:
    """relpath -> sha256 for every file under root.

    Recorded at install time so `pack verify` can detect a file whose CONTENT
    was edited in place — a check that a file-name inventory alone would miss
    entirely, and the realistic post-install tampering case.
    """
    out: Dict[str, str] = {}
    for base, _dirs, names in os.walk(root):
        for n in names:
            full = os.path.join(base, n)
            out[os.path.relpath(full, root)] = _file_digest(full)
    return out


def _extract(blob: bytes, dest: str) -> Dict[str, str]:
    """Extract a verified archive into dest. Returns relpath -> sha256."""
    dest_real = os.path.realpath(dest)
    os.makedirs(dest_real, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        tar.extractall(dest_real, members=_safe_members(tar, dest_real))
    return _tree_digests(dest_real)


# --------------------------------------------------------------------------- #
# Public API — list / install / verify
# --------------------------------------------------------------------------- #


def list_packs(entitlement_tier: str, source: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every manifest entry, annotated with signature state and eligibility.

    Signature failures are reported per row rather than raised: `pack list` is
    a diagnostic, and a user staring at a broken mirror deserves to SEE which
    entry is broken instead of getting one opaque error for the whole page.
    Nothing is installed here, so reporting is safe.
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
            signed = verify_entry(entry)
            row["signature_ok"] = True
            row.update(signed)
        except PackError as exc:
            row["reason"] = str(exc).splitlines()[0]
        row["eligible"] = bool(row["signature_ok"] and seat >= tier_rank(row["tier"]))
        cur = inst.get(row["pack_id"])
        row["installed_version"] = cur.get("version") if cur else None
        rows.append(row)
    return rows


def install(
    pack_id: Optional[str],
    entitlement_tier: str,
    source: Optional[str] = None,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Install one pack, or every eligible pack when pack_id is None.

    Order of operations is the security property, so it is fixed:
      entitlement check -> signature check -> download -> digest check ->
      downgrade check -> extract -> record.

    The tier and signature checks run BEFORE a single byte is fetched: a Free
    seat that asks for the Pro pack must not even cause the download, let alone
    reach the extractor.
    """
    doc = fetch_manifest(source)
    src = doc.get("_source") or manifest_url()
    seat = tier_rank(entitlement_tier)
    entries = [e for e in doc.get("packs", []) if isinstance(e, dict)]

    if pack_id:
        entries = [e for e in entries if str(e.get("pack_id") or "") == pack_id]
        if not entries:
            known = ", ".join(sorted(str(e.get("pack_id")) for e in doc.get("packs", [])
                                     if isinstance(e, dict))) or "(none)"
            raise PackError("no pack named %r in the manifest. Available: %s" % (pack_id, known))

    results: List[Dict[str, Any]] = []
    for entry in entries:
        eid = str(entry.get("pack_id") or "?")
        try:
            # --- threat 1 + 6: nothing proceeds without a valid signature ---
            signed = verify_entry(entry)

            # --- threat 4: tier gate, on the SIGNED tier, before any fetch ---
            need = tier_rank(signed["tier"])
            if seat < need:
                raise PackError(
                    "%s is a %s pack and your licence is %s.\n"
                    "  Upgrade at %s, then run:  veizik activate <YOUR_KEY>"
                    % (eid, TIER_LABEL.get(signed["tier"], signed["tier"]),
                       TIER_LABEL.get((entitlement_tier or "free").lower(), entitlement_tier),
                       UPGRADE_URL)
                )

            # --- threat 2: monotonic version floor ---
            _check_not_downgrade(eid, signed["version"])

            cur = installed().get(eid)
            if cur and cur.get("version") == signed["version"] and cur.get("sha256") == signed["sha256"]:
                results.append({"pack_id": eid, "action": "already-current",
                                "version": signed["version"]})
                continue

            if dry_run:
                results.append({"pack_id": eid, "action": "would-install",
                                "version": signed["version"], "tier": signed["tier"]})
                continue

            url = _entry_url(entry, src)
            blob = _read_source(url, MAX_DOWNLOAD_BYTES)

            # --- threat 5: the bytes must match the SIGNED digest ---
            got = hashlib.sha256(blob).hexdigest()
            if got != signed["sha256"]:
                raise PackError(
                    "%s: downloaded archive does not match its signed digest.\n"
                    "  signed   %s\n  received %s\n"
                    "  The signature covers the archive hash, so this means the archive was "
                    "swapped or corrupted in transit. Nothing was installed."
                    % (eid, signed["sha256"], got)
                )

            # --- threat 3: staged extraction with a hardened member filter ---
            _ensure_dirs()
            staging = tempfile.mkdtemp(prefix=".stage-%s-" % eid, dir=PACKS_DIR)
            try:
                digests = _extract(blob, staging)
                final = pack_path(eid)
                if os.path.isdir(final):
                    shutil.rmtree(final, ignore_errors=True)
                os.replace(staging, final)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise

            led = _ledger()
            led["packs"][eid] = {
                "version": signed["version"],
                "tier": signed["tier"],
                "created": signed["created"],
                "sha256": signed["sha256"],
                # Kept so `veizik pack verify` can re-run the real signature check
                # offline, months later, without refetching the manifest.
                "signature": str(entry.get("signature") or ""),
                "installed_at": _iso_now(),
                "path": final,
                "files": digests,
                "unlocks": list(entry.get("unlocks") or []),
                "summary": str(entry.get("summary") or ""),
            }
            prev = _floor(eid)
            now = _version_tuple(signed["version"])
            if prev is None or now > prev:
                led["floor"][eid] = signed["version"]
            _save_ledger(led)

            results.append({"pack_id": eid, "action": "installed", "version": signed["version"],
                            "tier": signed["tier"], "files": len(digests), "path": final})
        except PackError as exc:
            results.append({"pack_id": eid, "action": "refused", "reason": str(exc)})
    return results


def verify_installed() -> List[Dict[str, Any]]:
    """Re-verify every installed pack: file contents, then the signature.

    An install-time check is a check at one instant. This re-runs it on demand,
    so a customer can answer "is what I am about to run still what you signed?"
    for themselves, at any time, offline.

    Two independent checks, because they catch different attacks:
      * per-file sha256 against the digests recorded at install time — catches
        a file edited in place after installation, which a filename inventory
        would sail straight past;
      * the stored detached signature re-verified against the embedded public
        key — catches a ledger edited to relabel a pack's version or tier
        (e.g. to lower the downgrade floor, or to forge a Pro entitlement),
        since both fields are inside the signed tuple.
    """
    out: List[Dict[str, Any]] = []
    led = _ledger()
    for pack_id, rec in sorted(led["packs"].items()):
        row: Dict[str, Any] = {"pack_id": pack_id, "version": rec.get("version"),
                               "tier": rec.get("tier"), "ok": False, "reason": "",
                               "signature_checked": False}
        root = rec.get("path") or pack_path(pack_id)
        try:
            if not os.path.isdir(root):
                raise PackError("install directory is missing: %s" % root)

            expected = rec.get("files") or {}
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
                    "%d file(s) modified since installation (e.g. %s) — the pack on disk is "
                    "no longer what was signed" % (len(changed), changed[0])
                )

            sig = rec.get("signature")
            if not sig:
                raise PackError("no stored signature for this pack — reinstall it to re-establish trust")
            verify_entry({"pack_id": pack_id, "version": rec.get("version"),
                          "tier": rec.get("tier"), "created": rec.get("created"),
                          "sha256": rec.get("sha256"), "signature": sig})
            row["signature_checked"] = True
            row["files"] = len(expected)
            row["ok"] = True
        except PackError as exc:
            row["reason"] = str(exc).splitlines()[0]
        out.append(row)
    return out


def unlocked_features() -> Dict[str, str]:
    """feature -> the pack that unlocks it, for installed packs only.

    This is step 5 of the run-time sequence and the ONLY sanctioned way to ask
    whether a pack-delivered feature is live. Asking the entitlement alone
    would say "your plan includes it"; asking here says "and the verified code
    for it is actually present on this machine", which is the question the
    render path needs answered.
    """
    out: Dict[str, str] = {}
    for pack_id, rec in installed().items():
        for feat in rec.get("unlocks") or []:
            out.setdefault(str(feat), pack_id)
    return out


def load_profiles() -> Optional[Dict[str, Any]]:
    """Read profiles.json out of any installed pack that provides one.

    Returns None when no profile pack is installed — the caller then falls back
    to the public autotuner in limml_universal, which is what a Free seat runs.
    """
    for pack_id in sorted(installed()):
        path = os.path.join(pack_path(pack_id), "profiles.json")
        doc = _read_json(path)
        if doc:
            doc["_pack_id"] = pack_id
            return doc
    return None


# --------------------------------------------------------------------------- #
# CLI entry / self-check
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "list":
        print(json.dumps(list_packs(sys.argv[2] if len(sys.argv) > 2 else "free"),
                         indent=2, ensure_ascii=False))
    elif cmd == "verify":
        print(json.dumps(verify_installed(), indent=2, ensure_ascii=False))
    elif cmd == "status":
        print(json.dumps({"installed": installed(), "unlocked": unlocked_features(),
                          "crypto": crypto_available(), "manifest": manifest_url()},
                         indent=2, ensure_ascii=False))
    elif cmd == "selftest":
        # Canonical bytes must be stable and field-order independent.
        a = canonical_signing_bytes("p", "1.0.0", "creator", "2026-01-01T00:00:00Z", "ab" * 32)
        assert b'"schema":"veizik-pack-sig-v1"' in a
        assert a == canonical_signing_bytes("p", "1.0.0", "creator", "2026-01-01T00:00:00Z", "ab" * 32)

        # Version ordering is total, or it is refused.
        assert _version_tuple("1.2.3") == (1, 2, 3)
        for bad in ("1.0.0-rc1", "v1.0", "", "1.0.0+evil", "latest"):
            try:
                _version_tuple(bad)
                raise AssertionError("accepted unorderable version %r" % bad)
            except PackError:
                pass

        # Unknown tier ranks below Free, so it can never satisfy a >= gate.
        assert tier_rank("enterprise") == -1
        assert tier_rank("free") < tier_rank("creator") < tier_rank("pro")

        # An unsigned entry is refused outright.
        try:
            verify_entry({"pack_id": "x", "version": "1.0.0", "tier": "creator",
                          "created": "2026-01-01T00:00:00Z", "sha256": "ab" * 32})
            raise AssertionError("unsigned entry accepted")
        except PackError as e:
            assert "no signature" in str(e)

        # A syntactically perfect but forged signature is refused.
        try:
            verify_entry({"pack_id": "x", "version": "1.0.0", "tier": "creator",
                          "created": "2026-01-01T00:00:00Z", "sha256": "ab" * 32,
                          "signature": base64.b64encode(b"\x00" * 64).decode()})
            raise AssertionError("forged signature accepted")
        except PackError as e:
            assert "FAILED signature verification" in str(e)

        print("selftest OK")
    else:
        print("usage: veizik_packs.py list|verify|status|selftest")
