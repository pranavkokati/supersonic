"""Signed Turn Receipts — a cryptographically verifiable reproducibility record
for every turn that actually ships.

The problem this solves: once a turn passes the Verify gate and gets
checkpointed, the *evidence* for why it was trusted lives only in an SSE
event stream that nobody was necessarily watching, and in log lines that
scroll away. Ask "what exactly did the agent see, and what exactly did the
gate check, for the turn that shipped this file?" a week later, and the
honest answer is usually "we don't really know anymore."

A signed turn receipt is a small, self-contained, Ed25519-signed JSON
document — written to `.supersonic/receipts/turn-<n>.json` in the SAME git
commit as the checkpoint it describes — that records exactly:
  - a SHA-256 fingerprint of the prompt actually sent to the coding agent
    this turn (not the prompt *template*; the literal text, including any
    corrective re-prompt that was the one which actually got a passing diff)
  - a SHA-256 fingerprint of the diff that prompt produced, plus a plain
    files/insertions/deletions stat
  - which LLM provider/model Supersonic's own loop (planner, critic) used
    this turn, and which coding-agent CLI wrote the diff
  - the full Verify gate verdict (every signal, not just pass/fail)
  - an Ed25519 signature over all of the above, plus the public key needed
    to check it — the receipt is self-verifying, no external key file needed

WHAT THIS IS NOT: it is not a Sigstore/SLSA-style attestation with a
transparency log or a trusted third-party CA, and it does not prove the
prompt text wasn't itself misleading, or that the coding-agent CLI's own
internal model choice matches what its provider reports — Supersonic can't
see inside a third-party CLI subprocess. What it proves, cryptographically,
is narrower and still useful: *this specific receipt file, byte for byte,
was produced by a specific local Supersonic install's private key, and has
not been edited since*. That's enough to catch the concrete failure mode
this feature targets — a receipt (or the checkpoint tree around it) being
hand-edited after the fact to misrepresent what a shipped turn's diff
actually was or what the gate actually found.

The private signing key is generated once per machine and stored at
`~/.supersonic/keys/receipt_ed25519.pem` — never committed, same trust
boundary as the LLM API keys in config.json. The public key is embedded in
every receipt itself (and mirrored to `.supersonic/receipts/pubkey.txt` in
the project repo) specifically so a receipt can be verified by anyone who
has the repo, without needing anything from `~/.supersonic` — a teammate
reviewing a PR, or CI, can run `sonic verify-receipts <path>` against a
checkout with zero additional setup.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, List

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from supersonic.config import CONFIG_DIR

if TYPE_CHECKING:
    from supersonic.verify.gate import GateResult

logger = logging.getLogger(__name__)

RECEIPTS_DIRNAME = ".supersonic/receipts"
PUBKEY_FILENAME = "pubkey.txt"


# ── Key management ───────────────────────────────────────────────────────

def _keys_dir() -> Path:
    return CONFIG_DIR / "keys"


def _private_key_path() -> Path:
    return _keys_dir() / "receipt_ed25519.pem"


def load_or_create_signing_key() -> Ed25519PrivateKey:
    """One Ed25519 keypair per machine, generated on first use. The private
    key never leaves `~/.supersonic/keys/` and is never written into a
    project's git tree — only its public half is, inside each receipt."""
    path = _private_key_path()
    if path.exists():
        data = path.read_bytes()
        key = serialization.load_pem_private_key(data, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{path} does not contain an Ed25519 private key")
        return key
    key = Ed25519PrivateKey.generate()
    _keys_dir().mkdir(parents=True, exist_ok=True)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    try:
        path.chmod(0o600)
    except OSError:
        pass  # best-effort on platforms without POSIX permission bits
    logger.info("generated new receipt signing key at %s", path)
    return key


def public_key_hex(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return raw.hex()


# ── Receipt construction ─────────────────────────────────────────────────

def _diff_stat(diff: str) -> dict:
    """Plain files-changed/insertions/deletions count parsed directly from a
    unified diff — no external `git diff --stat` call, so it works on
    whatever diff text the caller already has in memory."""
    files = set()
    insertions = 0
    deletions = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            files.add(line)
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            insertions += 1
        elif line.startswith("-"):
            deletions += 1
    return {"files_changed": len(files), "insertions": insertions, "deletions": deletions}


def _canonical_bytes(payload: dict) -> bytes:
    """Deterministic serialization so the exact same payload always signs/
    verifies to the exact same bytes, regardless of dict insertion order."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class TurnReceipt:
    turn: int
    payload: dict = field(default_factory=dict)  # everything that gets signed, minus the signature itself
    signature: str = ""

    def to_dict(self) -> dict:
        d = dict(self.payload)
        d["signature"] = self.signature
        return d


def build_receipt(
    *,
    turn: int,
    goal: str,
    prompt: str,
    diff: str,
    coding_agent: str,
    provider_name: str,
    model: str,
    temperature: float,
    gate: "GateResult",
) -> TurnReceipt:
    """Build and sign a receipt for a turn that just passed the Verify gate.
    Caller is responsible for writing it into the working tree BEFORE the
    checkpoint commit is made, so the receipt and the diff it describes land
    in the same git commit."""
    key = load_or_create_signing_key()
    pubkey_hex = public_key_hex(key)
    payload = {
        "turn": turn,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "goal": goal[:300],
        "coding_agent": coding_agent,
        "provider": provider_name,
        "model": model,
        "temperature": temperature,
        "prompt_sha256": _sha256_hex(prompt),
        "diff_sha256": _sha256_hex(diff),
        "diff_stat": _diff_stat(diff),
        "gate": gate.to_dict(),
        "public_key": pubkey_hex,
    }
    signature = key.sign(_canonical_bytes(payload)).hex()
    return TurnReceipt(turn=turn, payload=payload, signature=signature)


def write_receipt(workdir: Path, receipt: TurnReceipt) -> Path:
    out_dir = Path(workdir) / RECEIPTS_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"turn-{receipt.turn}.json"
    out_path.write_text(json.dumps(receipt.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Mirrored so a receipt is verifiable from a bare checkout with no
    # dependency on the machine that generated it — the pubkey travels with
    # the repo, the private key never does.
    pubkey_path = out_dir / PUBKEY_FILENAME
    pubkey_path.write_text(receipt.payload.get("public_key", "") + "\n", encoding="utf-8")
    return out_path


# ── Verification ──────────────────────────────────────────────────────────

@dataclass
class ReceiptVerification:
    path: Path
    turn: int
    ok: bool
    reason: str = ""


def verify_receipt_file(path: Path) -> ReceiptVerification:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return ReceiptVerification(path=path, turn=-1, ok=False, reason=f"unreadable: {e}")

    turn = data.get("turn", -1)
    signature = data.get("signature")
    pubkey_hex = data.get("public_key")
    if not signature or not pubkey_hex:
        return ReceiptVerification(path=path, turn=turn, ok=False, reason="missing signature or public_key field")

    payload = {k: v for k, v in data.items() if k != "signature"}
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        pub.verify(bytes.fromhex(signature), _canonical_bytes(payload))
        return ReceiptVerification(path=path, turn=turn, ok=True)
    except (InvalidSignature, ValueError) as e:
        return ReceiptVerification(path=path, turn=turn, ok=False, reason=f"signature invalid: {e}")


def _turn_num(path: Path) -> int:
    try:
        return int(path.stem.split("-")[-1])
    except ValueError:
        return -1


def verify_all_receipts(workdir: Path) -> List[ReceiptVerification]:
    out_dir = Path(workdir) / RECEIPTS_DIRNAME
    if not out_dir.exists():
        return []
    paths = sorted(out_dir.glob("turn-*.json"), key=_turn_num)
    return [verify_receipt_file(p) for p in paths]
