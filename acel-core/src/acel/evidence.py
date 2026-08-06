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
one to disk or share it. ACEL doesn't redact or hash argument values before
recording them, by design — it can't know which fields are sensitive without
you telling it. If your tools take arguments you wouldn't want sitting in a
log file, keep secrets out of tool *arguments* entirely (pass a reference/ID
and resolve the real secret inside your own tool implementation instead).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

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

    def __init__(self, signer: Signer | None = None) -> None:
        self._bundles: list[EvidenceBundle] = []
        self._signer = signer

    @property
    def bundles(self) -> list[EvidenceBundle]:
        return list(self._bundles)

    def __len__(self) -> int:
        return len(self._bundles)

    def record(self, violation: Violation) -> EvidenceBundle:
        """Append a new bundle for ``violation`` and return it."""
        index = len(self._bundles)
        prev_hash = self._bundles[-1].bundle_hash if self._bundles else GENESIS_HASH
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = _payload_of(index, timestamp, violation.to_dict())
        trace_hash = _sha256_hex(_canonical(payload))
        bundle_hash = _sha256_hex((prev_hash + trace_hash).encode())
        signature = self._signer(bundle_hash.encode()) if self._signer else None
        bundle = EvidenceBundle(
            index=index,
            timestamp=timestamp,
            violation=violation.to_dict(),
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


def ed25519_signer() -> tuple[Signer, str]:
    """Build an Ed25519 signer, returning ``(signer, public_key_hex)``.

    Requires the optional ``cryptography`` dependency. Raises ImportError if it
    is not installed — signing is strictly opt-in.

    The private key is generated fresh in memory on every call and is never
    persisted or returned — only the public key (as hex) comes back. That
    means signatures from one process can only be verified against the public
    key from *that same* call; restart the process (or call this again) and
    you get a brand-new keypair, so old signatures are no longer verifiable
    against the new public key. If you need signatures that remain verifiable
    across restarts, generate and store your own long-lived Ed25519 keypair
    and pass a signer built from it instead of using this convenience
    function — this function exists for the common case of "sign within one
    process's lifetime," not for long-term signature retention.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError(
            "Ed25519 signing requires the 'cryptography' package. "
            "Install it with: pip install cryptography"
        ) from exc

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    def sign(data: bytes) -> str:
        return private_key.sign(data).hex()

    public_hex = public_key.public_bytes_raw().hex()
    return sign, public_hex
