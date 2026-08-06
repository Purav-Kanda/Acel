"""Tamper-evident evidence bundles.

On each violation ACEL emits an :class:`EvidenceBundle` capturing the trace, the
broken contract, and the state snapshot. Bundles are sha256 **hash-chained**:
every bundle stores the hash of the previous one, so any after-the-fact edit to
historical evidence breaks the chain and is detectable — a minimal Merkle-style
chain, no blockchain/consensus involved.

Ed25519 signing is layered on top *only* when the optional ``cryptography``
dependency is installed (see :func:`ed25519_signer`); the hash chain works with
the standard library alone.

Security note: a bundle's ``violation`` field embeds the full call arguments,
result, and state snapshot at the moment of the violation (``Violation.to_dict``
in ``violations.py``) — that's what makes a bundle useful evidence, but it
also means anything sensitive passed as a tool argument (a password, a raw
token, a secret) ends up persisted in the evidence log verbatim if you save
one to disk or share it. By default ACEL does *not* redact or hash argument
values before recording them, because it can't know which fields are
sensitive without you telling it — but you can tell it: pass
``redact_fields={"password", "api_key", ...}`` to :class:`EvidenceLog` (or
to :class:`~acel.session.Session`) and any dict key matching one of those
names, anywhere in the bundle's args/result/trace/state_snapshot (including
nested), is replaced with a short, non-reversible hash marker instead of the
real value before the bundle is ever hashed or written. See
:func:`redact_violation` for the redaction rule itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .violations import Violation

GENESIS_HASH = "0" * 64

# A signer takes the bytes to sign and returns a hex signature string.
Signer = Callable[[bytes], str]


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(payload: dict[str, Any]) -> bytes:
    """Deterministic JSON encoding so hashes are reproducible."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def _payload_of(index: int, timestamp: str, violation: dict[str, Any]) -> dict[str, Any]:
    return {"index": index, "timestamp": timestamp, "violation": violation}


def _redact_marker(value: Any) -> str:
    """A short, non-reversible stand-in for a redacted value.

    Hashing (rather than just writing ``"***REDACTED***"``) means two
    redacted entries that came from the *same* original value still look
    identical to an auditor — e.g. "this session used the same API key in
    calls 3 and 7" is still visible — without the plaintext ever being
    recoverable from the marker.
    """
    digest = _sha256_hex(_canonical({"v": value}))[:16]
    return f"***REDACTED(sha256:{digest})***"


def _redact_tree(node: Any, fields_lower: set[str]) -> Any:
    """Recursively replace any dict value whose key matches ``fields_lower``
    (case-insensitively) with a redaction marker, walking into nested dicts
    and lists (including lists of dicts, e.g. a violation's ``trace``)."""
    if isinstance(node, dict):
        return {
            key: _redact_marker(value) if key.lower() in fields_lower else _redact_tree(value, fields_lower)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_redact_tree(item, fields_lower) for item in node]
    return node


def redact_violation(violation: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    """Return a copy of a violation dict with sensitive fields masked.

    ``fields`` is a set of dict-key names (matched case-insensitively) to
    redact wherever they appear inside ``args``, ``result``,
    ``state_snapshot``, or ``trace`` — including nested dicts and the
    per-call dicts inside ``trace``. Everything else (``kind``, ``spec``,
    ``tool``, ``step``) is left untouched since none of it can carry
    arbitrary user data.
    """
    fields_lower = {f.lower() for f in fields}
    if not fields_lower:
        return dict(violation)
    redacted = dict(violation)
    for key in ("args", "result", "state_snapshot", "trace"):
        if key in redacted:
            redacted[key] = _redact_tree(redacted[key], fields_lower)
    return redacted


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """One signed, hash-chained record of a violation."""

    index: int
    timestamp: str
    violation: dict[str, Any]
    prev_hash: str
    trace_hash: str  # sha256 of the canonical payload
    bundle_hash: str  # sha256(prev_hash + trace_hash) — the chain link
    signature: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvidenceLog:
    """An append-only, hash-chained log of evidence bundles for one session."""

    def __init__(
        self,
        signer: Signer | None = None,
        *,
        redact_fields: Iterable[str] | None = None,
    ) -> None:
        self._bundles: list[EvidenceBundle] = []
        self._signer = signer
        self._redact_fields = set(redact_fields) if redact_fields else None

    @property
    def bundles(self) -> list[EvidenceBundle]:
        return list(self._bundles)

    def __len__(self) -> int:
        return len(self._bundles)

    def record(self, violation: Violation) -> EvidenceBundle:
        """Append a new bundle for ``violation`` and return it.

        If ``redact_fields`` was set on this log, sensitive values are
        masked (see :func:`redact_violation`) *before* anything is hashed or
        signed — a redacted bundle's hash chain is computed over the
        redacted payload, so verification still works against the redacted
        copy without ever needing the original values back.
        """
        index = len(self._bundles)
        prev_hash = self._bundles[-1].bundle_hash if self._bundles else GENESIS_HASH
        timestamp = datetime.now(timezone.utc).isoformat()
        violation_dict = violation.to_dict()
        if self._redact_fields:
            violation_dict = redact_violation(violation_dict, self._redact_fields)
        payload = _payload_of(index, timestamp, violation_dict)
        trace_hash = _sha256_hex(_canonical(payload))
        bundle_hash = _sha256_hex((prev_hash + trace_hash).encode())
        signature = self._signer(bundle_hash.encode()) if self._signer else None
        bundle = EvidenceBundle(
            index=index,
            timestamp=timestamp,
            violation=violation_dict,
            prev_hash=prev_hash,
            trace_hash=trace_hash,
            bundle_hash=bundle_hash,
            signature=signature,
        )
        self._bundles.append(bundle)
        return bundle

    def verify(self) -> bool:
        """Return True if the current in-memory chain is internally consistent."""
        return self.verify_bundles([b.to_dict() for b in self._bundles])

    def to_json(self) -> str:
        return json.dumps([b.to_dict() for b in self._bundles], indent=2, default=str)

    @staticmethod
    def verify_bundles(bundles: list[dict[str, Any]]) -> bool:
        """Verify a list of bundle dicts (e.g. loaded from disk) for tampering.

        Recomputes every hash from the payloads and checks the chain links.
        Any altered field anywhere in the history makes this return False.
        """
        ok, _ = EvidenceLog.verify_bundles_detailed(bundles)
        return ok

    @staticmethod
    def verify_bundles_detailed(bundles: list[dict[str, Any]]) -> tuple[bool, int | None]:
        """Like :meth:`verify_bundles`, but also reports *where* the chain broke.

        Returns ``(True, None)`` if every bundle checks out, or ``(False, i)``
        where ``i`` is the index of the first bundle that fails to verify —
        because the chain is cumulative, everything after that point is
        untrustworthy too, but the *first* break is what tells you where the
        tampering (or corruption) actually happened.
        """
        prev = GENESIS_HASH
        for i, bundle in enumerate(bundles):
            payload = _payload_of(bundle["index"], bundle["timestamp"], bundle["violation"])
            trace_hash = _sha256_hex(_canonical(payload))
            if trace_hash != bundle["trace_hash"]:
                return False, i
            if bundle["prev_hash"] != prev:
                return False, i
            if _sha256_hex((prev + trace_hash).encode()) != bundle["bundle_hash"]:
                return False, i
            prev = bundle["bundle_hash"]
        return True, None


def ed25519_signer(key_path: str | Path | None = None) -> tuple[Signer, str]:
    """Build an Ed25519 signer, returning ``(signer, public_key_hex)``.

    Requires the optional ``cryptography`` dependency. Raises ImportError if it
    is not installed — signing is strictly opt-in.

    ``key_path`` controls where the private key lives:

    - **Omitted (default):** the private key is generated fresh in memory
      and never written to disk. Signatures are only verifiable within the
      current process's lifetime — call this again (or restart) and you get
      a brand-new keypair, so old signatures stop matching the new public
      key. Fine for a short-lived script or a test; not fine for evidence
      you actually want to be able to verify later.
    - **A path:** if the file already exists, the key is loaded from it and
      reused; if it doesn't, a new key is generated and written there
      (mode ``0o600``, readable only by the owner) so every future call with
      the same path reuses the same identity. This is what you want for a
      real deployment — the public key stays stable across restarts, so
      evidence signed last week still verifies against the public key you
      have on file today.

    The private key file is raw 32-byte Ed25519 key material, not
    PEM-wrapped — treat it exactly like an SSH private key: back it up if
    you need old signatures to stay verifiable, and never commit it to a
    repo or evidence log.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError(
            "Ed25519 signing requires the 'cryptography' package. "
            "Install it with: pip install cryptography"
        ) from exc

    if key_path is None:
        private_key = Ed25519PrivateKey.generate()
    else:
        path = Path(key_path)
        if path.exists():
            private_key = Ed25519PrivateKey.from_private_bytes(path.read_bytes())
        else:
            private_key = Ed25519PrivateKey.generate()
            raw = private_key.private_bytes_raw()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(0o600)

    public_key = private_key.public_key()

    def sign(data: bytes) -> str:
        return private_key.sign(data).hex()

    public_hex = public_key.public_bytes_raw().hex()
    return sign, public_hex
