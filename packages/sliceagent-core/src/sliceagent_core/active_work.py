"""Immutable, source-linked active-work state.

The active-work graph is deliberately a semantic *record*, not another prompt region and
not a transcript summary.  User language remains authoritative in the immutable event
ledger.  A :class:`SourceRef` identifies an exact half-open range in one such event and
binds both the complete source and selected span by digest.  The model may propose
``WorkDelta`` objects; the host applies them mechanically after checking identity,
provenance, lifecycle, dependency, and revision invariants.

This module has no dependency on ``Slice`` or the persistence stores.  Its ``to_dict`` /
``from_dict`` boundary is JSON-only, so it can be embedded in a checkpoint or artifact
without giving either layer a second interpretation of the work.
"""
from __future__ import annotations

from .interfaces import PeerResult, PeerWait

import hashlib
import json
import math as _math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal, TypeAlias


WORK_GRAPH_VERSION = 1
SOURCE_REF_VERSION = 1

WorkStatus: TypeAlias = Literal[
    "open",
    "in_progress",
    "waiting_user",
    "waiting_peer",
    "ready",
    "delivered",
    "verified",
    "cancelled",
    "superseded",
]
WorkKind: TypeAlias = Literal["request", "task"]

WORK_STATUSES = frozenset({
    "open", "in_progress", "waiting_user", "waiting_peer", "ready", "delivered", "verified", "cancelled", "superseded",
})
WORK_KINDS = frozenset({"request", "task"})
UNRESOLVED_STATUSES = frozenset({"open", "in_progress", "waiting_user", "waiting_peer", "ready"})
# ``waiting_peer`` is a host-managed park set only via ``WorkGraph.seal_current``;
# ``verified`` and ``delivered`` are host-owned too.  Keeping the model-settable
# subset beside the graph lifecycle table lets core validate transitions without
# importing the concrete coding tool host.
MODEL_WORK_STATUSES = frozenset({
    "open", "in_progress", "waiting_user", "ready", "cancelled", "superseded",
})
_SHA256_LENGTH = 64


class ActiveWorkError(ValueError):
    """Base class for active-work records that cannot be accepted mechanically."""


class SourceMismatchError(ActiveWorkError):
    """A source event is absent or no longer matches the exact reference."""


class GraphValidationError(ActiveWorkError):
    """A graph or delta violates an ownership, dependency, or lifecycle invariant."""


class RevisionConflictError(ActiveWorkError):
    """A delta was authored against a graph revision other than the current one."""


def _text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "possibly-empty" if allow_empty else "non-empty"
        raise GraphValidationError(f"{name} must be a {qualifier} string")
    # Active Work metadata is rendered as one record per line.  Exact user source text lives separately in
    # SourceRef-bound ledger events and may be multiline; identifiers, descriptions, kinds, and locators may
    # not smuggle a second rendered record/header through CR/LF control characters.
    if "\r" in value or "\n" in value:
        raise GraphValidationError(f"{name} must not contain CR or LF")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise GraphValidationError(f"{name} must be an integer >= {minimum}")
    return value


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid_digest(value: object, name: str) -> str:
    value = _text(value, name)
    if len(value) != _SHA256_LENGTH or any(ch not in "0123456789abcdef" for ch in value):
        raise GraphValidationError(f"{name} must be a lowercase sha256 digest")
    return value


def _string_tuple(value: Iterable[str] | None, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise GraphValidationError(f"{name} must be a sequence of strings, not one string")
    try:
        result = tuple(value)
    except TypeError as exc:
        raise GraphValidationError(f"{name} must be a sequence of strings") from exc
    for item in result:
        _text(item, f"{name} item")
    if len(set(result)) != len(result):
        raise GraphValidationError(f"{name} must not contain duplicates")
    return result


def _record_tuple(value: Iterable[Any] | None, cls: type, name: str) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)):
        raise GraphValidationError(f"{name} must be a sequence")
    try:
        result = tuple(value)
    except TypeError as exc:
        raise GraphValidationError(f"{name} must be a sequence") from exc
    if any(not isinstance(item, cls) for item in result):
        raise GraphValidationError(f"{name} must contain only {cls.__name__} records")
    return result


def _wire_sequence(value: object, name: str) -> tuple:
    """Decode one JSON array without leaking raw ``TypeError`` from hostile records."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)):
        raise GraphValidationError(f"{name} must be a sequence")
    try:
        return tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise GraphValidationError(f"{name} must be a sequence") from exc


@dataclass(frozen=True, order=True)
class SourceRef:
    """Exact ``[start, end)`` Unicode-codepoint range in one immutable source event.

    ``source_sha256`` prevents an event ID from silently being rebound to different
    content.  ``span_sha256`` prevents a malformed range from being treated as the
    intended clause.  The text itself remains in the event ledger, avoiding a competing
    mutable copy in the work graph.
    """

    event_id: str
    start: int
    end: int
    source_length: int
    source_sha256: str
    span_sha256: str
    version: int = SOURCE_REF_VERSION

    def __post_init__(self) -> None:
        _text(self.event_id, "source_ref.event_id")
        _integer(self.start, "source_ref.start")
        _integer(self.end, "source_ref.end", minimum=1)
        _integer(self.source_length, "source_ref.source_length", minimum=1)
        if self.end <= self.start:
            raise GraphValidationError("source_ref range must be non-empty")
        if self.end > self.source_length:
            raise GraphValidationError("source_ref.end exceeds source_ref.source_length")
        _valid_digest(self.source_sha256, "source_ref.source_sha256")
        _valid_digest(self.span_sha256, "source_ref.span_sha256")
        _integer(self.version, "source_ref.version", minimum=1)
        if self.version != SOURCE_REF_VERSION:
            raise GraphValidationError(f"unsupported source-ref version: {self.version}")

    @classmethod
    def bind(cls, event_id: str, source: str, *, start: int = 0, end: int | None = None) -> "SourceRef":
        """Bind a range to exact source text without interpreting its meaning."""
        _text(event_id, "source_ref.event_id")
        if not isinstance(source, str) or not source:
            raise GraphValidationError("source text must be a non-empty string")
        _integer(start, "source_ref.start")
        if end is None:
            end = len(source)
        _integer(end, "source_ref.end", minimum=1)
        if end <= start or end > len(source):
            raise GraphValidationError("source range must be non-empty and within the source text")
        return cls(
            event_id=event_id,
            start=start,
            end=end,
            source_length=len(source),
            source_sha256=_sha256_text(source),
            span_sha256=_sha256_text(source[start:end]),
        )

    def extract(self, source: str) -> str:
        """Return the exact span, rejecting missing, changed, or differently-sized text."""
        if not isinstance(source, str):
            raise SourceMismatchError(f"source {self.event_id!r} is not text")
        if len(source) != self.source_length or _sha256_text(source) != self.source_sha256:
            raise SourceMismatchError(f"source {self.event_id!r} no longer matches its immutable event")
        span = source[self.start:self.end]
        if _sha256_text(span) != self.span_sha256:
            raise SourceMismatchError(f"source range for {self.event_id!r} does not match its bound span")
        return span

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.version,
            "event_id": self.event_id,
            "start": self.start,
            "end": self.end,
            "source_length": self.source_length,
            "source_sha256": self.source_sha256,
            "span_sha256": self.span_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceRef":
        if not isinstance(value, Mapping):
            raise GraphValidationError("source ref must be an object")
        try:
            return cls(
                event_id=value.get("event_id", ""),
                start=value.get("start"),
                end=value.get("end"),
                source_length=value.get("source_length"),
                source_sha256=value.get("source_sha256", ""),
                span_sha256=value.get("span_sha256", ""),
                version=value.get("v", SOURCE_REF_VERSION),
            )
        except TypeError as exc:
            raise GraphValidationError(f"invalid source ref: {exc}") from exc


@dataclass(frozen=True, order=True)
class EvidenceRef:
    """Typed locator for execution/world evidence; the referenced store owns truth.

    ``qualifier`` carries a bounded mechanical condition such as ``source_partial``. It never upgrades the
    referenced testimony into a correctness verdict; exact detail remains behind ``ref``.
    """

    kind: str
    ref: str
    qualifier: str = ""

    def __post_init__(self) -> None:
        _text(self.kind, "evidence_ref.kind")
        _text(self.ref, "evidence_ref.ref")
        _text(self.qualifier, "evidence_ref.qualifier", allow_empty=True)

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref": self.ref, **({"qualifier": self.qualifier} if self.qualifier else {})}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceRef":
        if not isinstance(value, Mapping):
            raise GraphValidationError("evidence ref must be an object")
        return cls(
            kind=value.get("kind", ""), ref=value.get("ref", ""),
            qualifier=value.get("qualifier", ""),
        )


@dataclass(frozen=True, order=True)
class OutputRef:
    """Typed locator for a user-visible response or another delivered artifact."""

    kind: str
    ref: str

    def __post_init__(self) -> None:
        _text(self.kind, "output_ref.kind")
        _text(self.ref, "output_ref.ref")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref": self.ref}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutputRef":
        if not isinstance(value, Mapping):
            raise GraphValidationError("output ref must be an object")
        return cls(kind=value.get("kind", ""), ref=value.get("ref", ""))


@dataclass(frozen=True, order=True)
class ResourceRef:
    """Locator for live world state consumed by one item.

    The workspace epoch prevents a file observation from workspace A being projected as
    current after a transition to workspace B.  ``revision`` is intentionally an opaque
    store-owned fingerprint rather than a host interpretation of the resource.
    """

    kind: str
    ref: str
    workspace_epoch: int = 0
    revision: str = ""

    def __post_init__(self) -> None:
        _text(self.kind, "resource_ref.kind")
        _text(self.ref, "resource_ref.ref")
        _integer(self.workspace_epoch, "resource_ref.workspace_epoch")
        _text(self.revision, "resource_ref.revision", allow_empty=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "workspace_epoch": self.workspace_epoch,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResourceRef":
        if not isinstance(value, Mapping):
            raise GraphValidationError("resource ref must be an object")
        return cls(
            kind=value.get("kind", ""),
            ref=value.get("ref", ""),
            workspace_epoch=value.get("workspace_epoch", 0),
            revision=value.get("revision", ""),
        )


@dataclass(frozen=True)
class WorkItem:
    """One model-authored unit of work linked back to exact source language."""

    id: str
    root_id: str
    source_refs: tuple[SourceRef, ...]
    status: WorkStatus = "open"
    kind: WorkKind = "task"
    description: str = ""
    logical_id: str = ""
    workspace_epoch: int = 0
    dependencies: tuple[str, ...] = ()
    resource_refs: tuple[ResourceRef, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    output_refs: tuple[OutputRef, ...] = ()
    superseded_by: str = ""
    stop_reason: str = ""
    # Plan-mode acceptance contract, fixed at plan time so execution cannot lower the bar:
    # `verify` = commands whose exit status proves the item (host-run at completion, P2);
    # `done_when` = the human-readable acceptance criterion. Optional; absent on legacy records.
    verify: tuple[str, ...] = ()
    done_when: str = ""
    # Typed peer-wait correlation for a ``waiting_peer`` park (None otherwise). Durable state,
    # not prose in stop_reason: only a matching PeerResult.correlation_id may resume the request.
    peer_wait: "PeerWait | None" = None

    def __post_init__(self) -> None:
        _text(self.id, "work_item.id")
        _text(self.root_id, "work_item.root_id")
        _text(self.description, "work_item.description", allow_empty=True)
        if self.status not in WORK_STATUSES:
            raise GraphValidationError(f"unsupported work status: {self.status!r}")
        if self.kind not in WORK_KINDS:
            raise GraphValidationError(f"unsupported work kind: {self.kind!r}")
        # One predicate used to cover four distinct breaches and reported only the RULE, so a model
        # that sent nine commands, a blank one, or an over-long one got the same sentence and had to
        # guess which. Name the breach and the offending entry — a rejection the caller cannot act on
        # is a dead end, and it retries blind.
        if not isinstance(self.verify, tuple):
            raise GraphValidationError(
                f"work_item.verify must be a list of shell commands, got {type(self.verify).__name__}")
        if len(self.verify) > 8:
            raise GraphValidationError(
                f"work_item.verify has {len(self.verify)} commands; the limit is 8 — keep the checks "
                "that actually gate this item, or split it into separate items")
        for index, cmd in enumerate(self.verify):
            if not isinstance(cmd, str):
                raise GraphValidationError(
                    f"work_item.verify[{index}] must be a string, got {type(cmd).__name__}")
            if not cmd.strip():
                raise GraphValidationError(
                    f"work_item.verify[{index}] is empty — drop the entry rather than sending a blank")
            if len(cmd) > 500:
                raise GraphValidationError(
                    f"work_item.verify[{index}] is {len(cmd)} chars; the limit is 500 — put a long "
                    f"check in a script and verify that instead. It began: {cmd[:80]!r}")
        if not isinstance(self.done_when, str) or len(self.done_when) > 500:
            raise GraphValidationError("work_item.done_when must be a string of at most 500 chars")
        if self.peer_wait is not None and not isinstance(self.peer_wait, PeerWait):
            raise GraphValidationError("work_item.peer_wait must be a PeerWait or None")
        # A durable peer wait is meaningful ONLY in the waiting_peer state, and the
        # waiting_peer state is meaningless WITHOUT its correlation. Enforce the biconditional
        # so no recovery record is internally contradictory (a parked wait that lost its
        # correlation, or stale wait metadata surviving after the request left the state).
        if (self.status == "waiting_peer") != (self.peer_wait is not None):
            raise GraphValidationError(
                "work_item.status=='waiting_peer' iff a non-null peer_wait is present"
            )
        if not self.logical_id and self.kind == "request":
            object.__setattr__(self, "logical_id", self.root_id)
        _text(self.logical_id, "work_item.logical_id", allow_empty=self.kind == "task")
        _integer(self.workspace_epoch, "work_item.workspace_epoch")
        _text(self.superseded_by, "work_item.superseded_by", allow_empty=True)
        _text(self.stop_reason, "work_item.stop_reason", allow_empty=True)
        object.__setattr__(self, "source_refs", _record_tuple(self.source_refs, SourceRef, "work_item.source_refs"))
        object.__setattr__(self, "dependencies", _string_tuple(self.dependencies, "work_item.dependencies"))
        object.__setattr__(self, "resource_refs", _record_tuple(
            self.resource_refs, ResourceRef, "work_item.resource_refs",
        ))
        object.__setattr__(self, "evidence_refs", _record_tuple(
            self.evidence_refs, EvidenceRef, "work_item.evidence_refs",
        ))
        object.__setattr__(self, "output_refs", _record_tuple(self.output_refs, OutputRef, "work_item.output_refs"))
        if not self.source_refs:
            raise GraphValidationError("every work item must cite at least one exact source range")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise GraphValidationError("work_item.source_refs must not contain duplicates")
        if len(set(self.resource_refs)) != len(self.resource_refs):
            raise GraphValidationError("work_item.resource_refs must not contain duplicates")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise GraphValidationError("work_item.evidence_refs must not contain duplicates")
        if len(set(self.output_refs)) != len(self.output_refs):
            raise GraphValidationError("work_item.output_refs must not contain duplicates")
        if self.kind == "request" and self.root_id != self.id:
            raise GraphValidationError("a request root must name itself as root_id")
        if self.status in ("delivered", "verified") and not self.output_refs:
            raise GraphValidationError(f"{self.status} work must cite its delivered output")
        if self.status == "verified" and not self.evidence_refs:
            raise GraphValidationError("verified work must cite verification evidence")
        if self.status == "superseded":
            if not self.superseded_by or self.superseded_by == self.id:
                raise GraphValidationError("superseded work must cite a different replacement item")
        elif self.superseded_by:
            raise GraphValidationError("superseded_by is valid only when status is superseded")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "root_id": self.root_id,
            "kind": self.kind,
            "status": self.status,
            "description": self.description,
            "logical_id": self.logical_id,
            "workspace_epoch": self.workspace_epoch,
            "source_refs": [ref.to_dict() for ref in self.source_refs],
            "dependencies": list(self.dependencies),
            "resource_refs": [ref.to_dict() for ref in self.resource_refs],
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
            "output_refs": [ref.to_dict() for ref in self.output_refs],
            "superseded_by": self.superseded_by,
            "stop_reason": self.stop_reason,
            "verify": list(self.verify),
            "done_when": self.done_when,
            "peer_wait": (
                None if self.peer_wait is None else {
                    "correlation_id": self.peer_wait.correlation_id,
                    "peer_id": self.peer_wait.peer_id,
                    "deadline_s": self.peer_wait.deadline_s,
                }
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkItem":
        if not isinstance(value, Mapping):
            raise GraphValidationError("work item must be an object")
        sources = _wire_sequence(value.get("source_refs") or (), "work_item.source_refs")
        resources = _wire_sequence(value.get("resource_refs") or (), "work_item.resource_refs")
        evidence = _wire_sequence(value.get("evidence_refs") or (), "work_item.evidence_refs")
        outputs = _wire_sequence(value.get("output_refs") or (), "work_item.output_refs")
        dependencies = _wire_sequence(value.get("dependencies") or (), "work_item.dependencies")
        if any(not isinstance(item, Mapping) for item in sources):
            raise GraphValidationError("work_item.source_refs must contain objects")
        if any(not isinstance(item, Mapping) for item in evidence):
            raise GraphValidationError("work_item.evidence_refs must contain objects")
        if any(not isinstance(item, Mapping) for item in resources):
            raise GraphValidationError("work_item.resource_refs must contain objects")
        if any(not isinstance(item, Mapping) for item in outputs):
            raise GraphValidationError("work_item.output_refs must contain objects")
        return cls(
            id=value.get("id", ""),
            root_id=value.get("root_id", ""),
            kind=value.get("kind", "task"),
            status=value.get("status", "open"),
            description=value.get("description", ""),
            logical_id=value.get("logical_id", ""),
            workspace_epoch=value.get("workspace_epoch", 0),
            source_refs=tuple(SourceRef.from_dict(item) for item in sources),
            dependencies=tuple(dependencies),
            resource_refs=tuple(ResourceRef.from_dict(item) for item in resources),
            evidence_refs=tuple(EvidenceRef.from_dict(item) for item in evidence),
            output_refs=tuple(OutputRef.from_dict(item) for item in outputs),
            superseded_by=value.get("superseded_by", ""),
            stop_reason=value.get("stop_reason", ""),
            verify=tuple(str(cmd) for cmd in (value.get("verify") or ()) if str(cmd).strip()),
            done_when=str(value.get("done_when") or ""),
            peer_wait=_peer_wait_from_dict(value.get("peer_wait")),
        )


def _peer_wait_from_dict(value: Any) -> "PeerWait | None":
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise GraphValidationError("work_item.peer_wait must be an object or null")
    correlation = value.get("correlation_id")
    peer = value.get("peer_id")
    deadline = value.get("deadline_s")
    # Type-check raw wire fields BEFORE construction — never coerce. A malformed recovery
    # record (e.g. deadline_s: true, peer_id: 123) must fail closed, not be silently repaired
    # into a plausible-but-fabricated typed value.
    if not isinstance(correlation, str) or not isinstance(peer, str):
        raise GraphValidationError("peer_wait correlation_id/peer_id must be strings")
    if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float))):
        raise GraphValidationError("peer_wait deadline_s must be a number or null")
    try:
        deadline_f = None if deadline is None else float(deadline)
        return PeerWait(correlation_id=correlation, peer_id=peer, deadline_s=deadline_f)
    except (ValueError, OverflowError) as exc:
        raise GraphValidationError(f"invalid peer_wait: {exc}") from exc


@dataclass(frozen=True)
class WorkDelta:
    """One compare-and-swap proposal containing new and replacement item snapshots."""

    expected_revision: int
    creates: tuple[WorkItem, ...] = ()
    updates: tuple[WorkItem, ...] = ()

    def __post_init__(self) -> None:
        _integer(self.expected_revision, "work_delta.expected_revision")
        object.__setattr__(self, "creates", _record_tuple(self.creates, WorkItem, "work_delta.creates"))
        object.__setattr__(self, "updates", _record_tuple(self.updates, WorkItem, "work_delta.updates"))
        if not self.creates and not self.updates:
            raise GraphValidationError("a work delta must create or update at least one item")
        create_ids = [item.id for item in self.creates]
        update_ids = [item.id for item in self.updates]
        if len(set(create_ids)) != len(create_ids):
            raise GraphValidationError("work_delta.creates contains duplicate item IDs")
        if len(set(update_ids)) != len(update_ids):
            raise GraphValidationError("work_delta.updates contains duplicate item IDs")
        if set(create_ids) & set(update_ids):
            raise GraphValidationError("one delta cannot both create and update the same item")

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_revision": self.expected_revision,
            "creates": [item.to_dict() for item in self.creates],
            "updates": [item.to_dict() for item in self.updates],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkDelta":
        if not isinstance(value, Mapping):
            raise GraphValidationError("work delta must be an object")
        creates = _wire_sequence(value.get("creates") or (), "work_delta.creates")
        updates = _wire_sequence(value.get("updates") or (), "work_delta.updates")
        if any(not isinstance(item, Mapping) for item in creates + updates):
            raise GraphValidationError("work delta entries must be objects")
        return cls(
            expected_revision=value.get("expected_revision"),
            creates=tuple(WorkItem.from_dict(item) for item in creates),
            updates=tuple(WorkItem.from_dict(item) for item in updates),
        )


_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    # ``verified`` is reachable from every working status — but ONLY via the host's promotion path:
    # build_work_delta rewrites a model-supplied 'ready' to 'verified' after the item's verify commands
    # ran green host-side (P2). The model itself can never submit 'verified' (intake guard), and a
    # verified item must carry its typed verification evidence/output refs (invariants above).
    "open": frozenset({"open", "in_progress", "waiting_user", "waiting_peer", "ready", "delivered",
                       "verified", "cancelled", "superseded"}),
    "in_progress": frozenset({
        "in_progress", "waiting_user", "waiting_peer", "ready", "delivered", "verified", "cancelled", "superseded",
    }),
    "waiting_user": frozenset({
        "waiting_user", "in_progress", "ready", "delivered", "verified", "cancelled", "superseded",
    }),
    # A peer-parked request resumes to in_progress on a correlated PeerResult (the horizontal
    # analogue of waiting_user); it may also be delivered/cancelled/superseded like any wait.
    # `waiting_user` is reachable too: once a park is EXPLICITLY resolved, the same request may
    # legitimately end that segment waiting on the user instead. Omitting it made an explicit
    # resolution into a user wait raise, so the two wait axes could not hand off to each other.
    "waiting_peer": frozenset({
        "waiting_peer", "waiting_user", "in_progress", "ready", "delivered", "verified",
        "cancelled", "superseded",
    }),
    # ``ready`` says a child contribution is prepared. It may be model-maintained for local work or derived
    # by the host from a bound child's successful immutable seal. Only the host can attach the real response
    # artifact and advance it to delivered.
    "ready": frozenset({"ready", "in_progress", "delivered", "verified", "cancelled", "superseded"}),
    # A delivered answer can be reopened when new evidence shows it is incomplete; verification is distinct.
    "delivered": frozenset({"delivered", "verified", "in_progress", "cancelled", "superseded"}),
    "verified": frozenset({"verified"}),
    "cancelled": frozenset({"cancelled"}),
    "superseded": frozenset({"superseded"}),
}


@dataclass(frozen=True)
class WorkGraph:
    """Immutable graph of request roots and their dependency-linked work items."""

    items: tuple[WorkItem, ...] = ()
    revision: int = 0
    version: int = WORK_GRAPH_VERSION
    _by_id: Mapping[str, WorkItem] = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        _integer(self.revision, "work_graph.revision")
        _integer(self.version, "work_graph.version", minimum=1)
        if self.version != WORK_GRAPH_VERSION:
            raise GraphValidationError(f"unsupported work-graph version: {self.version}")
        items = _record_tuple(self.items, WorkItem, "work_graph.items")
        roots = {item.id: item for item in items if item.kind == "request"}
        # Child constructors may omit a redundant logical ID. Normalize it once at the
        # immutable graph boundary; serialized graph records are always explicit.
        items = tuple(
            replace(item, logical_id=roots[item.root_id].logical_id)
            if item.kind == "task" and not item.logical_id and item.root_id in roots else item
            for item in items
        )
        object.__setattr__(self, "items", items)
        by_id = {item.id: item for item in items}
        if len(by_id) != len(self.items):
            raise GraphValidationError("work graph contains duplicate item IDs")
        object.__setattr__(self, "_by_id", MappingProxyType(by_id))
        self._validate_graph(by_id)

    def __deepcopy__(self, _memo) -> "WorkGraph":
        """An immutable graph is its own deep copy.

        ``_by_id`` is a read-only ``mappingproxy`` cache, which Python's generic deepcopy cannot pickle. Slice
        sealing and transactional reducer rollback deep-copy the containing Slice; returning this frozen value
        preserves those boundaries without reconstructing or sharing mutable state.
        """
        return self

    def _validate_graph(self, by_id: Mapping[str, WorkItem]) -> None:
        roots = {item.id for item in self.items if item.kind == "request"}
        logical_ids = [item.logical_id for item in self.items if item.kind == "request"]
        if len(set(logical_ids)) != len(logical_ids):
            raise GraphValidationError("request roots must have unique logical IDs")
        for root_id in roots:
            root = by_id[root_id]
            if root.status in UNRESOLVED_STATUSES:
                continue
            unresolved_children = sorted(
                item.id for item in self.items
                if item.id != root_id and item.root_id == root_id
                and item.status in UNRESOLVED_STATUSES
            )
            if unresolved_children:
                raise GraphValidationError(
                    f"terminal request root {root_id!r} has unresolved child work: "
                    + ", ".join(unresolved_children),
                )
        source_digests: dict[str, tuple[int, str]] = {}
        for item in self.items:
            if item.root_id not in roots:
                raise GraphValidationError(
                    f"work item {item.id!r} points to missing/non-request root {item.root_id!r}",
                )
            if item.logical_id != by_id[item.root_id].logical_id:
                raise GraphValidationError(
                    f"work item {item.id!r} logical_id differs from its request root",
                )
            for dependency in item.dependencies:
                if dependency not in by_id:
                    raise GraphValidationError(f"work item {item.id!r} has unknown dependency {dependency!r}")
                if dependency == item.id:
                    raise GraphValidationError(f"work item {item.id!r} depends on itself")
                if by_id[dependency].root_id != item.root_id:
                    # Unfixed sibling of the per-turn root-minting dead end: every user message mints
                    # a new root, so depending on last turn's item is a natural move that fails. Name
                    # WHICH dependency (add_dependencies is a list) and the legal way forward.
                    raise GraphValidationError(
                        f"work item {item.id!r} cannot depend across request roots: {dependency!r} belongs "
                        "to an earlier request. Re-create the prerequisite under the current request "
                        "and depend on that, or drop the dependency. Retrying this edge cannot succeed",
                    )
            if item.status == "superseded" and item.superseded_by not in by_id:
                raise GraphValidationError(
                    f"work item {item.id!r} has unknown replacement {item.superseded_by!r}",
                )
            if item.status == "superseded":
                replacement = by_id[item.superseded_by]
                same_request = replacement.root_id == item.root_id
                request_correction = item.kind == replacement.kind == "request"
                if not same_request and not request_correction:
                    raise GraphValidationError(
                        f"work item {item.id!r} replacement belongs to a different request root",
                    )
            for ref in item.source_refs:
                identity = (ref.source_length, ref.source_sha256)
                old = source_digests.setdefault(ref.event_id, identity)
                if old != identity:
                    raise GraphValidationError(
                        f"source event {ref.event_id!r} is bound to conflicting immutable content",
                    )
        self._reject_dependency_cycles(by_id)
        self._reject_supersession_cycles(by_id)

    @staticmethod
    def _reject_dependency_cycles(by_id: Mapping[str, WorkItem]) -> None:
        # Kahn's algorithm avoids recursion depth becoming a denial-of-service on a large valid graph.
        indegree = {item_id: 0 for item_id in by_id}
        dependants: dict[str, list[str]] = {item_id: [] for item_id in by_id}
        for item in by_id.values():
            for dependency in item.dependencies:
                indegree[item.id] += 1
                dependants[dependency].append(item.id)
        ready = [item_id for item_id, degree in indegree.items() if degree == 0]
        visited = 0
        while ready:
            item_id = ready.pop()
            visited += 1
            for dependant in dependants[item_id]:
                indegree[dependant] -= 1
                if indegree[dependant] == 0:
                    ready.append(dependant)
        if visited != len(by_id):
            cyclic = sorted(item_id for item_id, degree in indegree.items() if degree)
            raise GraphValidationError(f"work dependency cycle detected: {', '.join(cyclic)}")

    @staticmethod
    def _reject_supersession_cycles(by_id: Mapping[str, WorkItem]) -> None:
        """A correction chain is directional history, never a way to resurrect old work."""
        for start in by_id:
            seen: set[str] = set()
            current = start
            while current:
                if current in seen:
                    raise GraphValidationError(f"work supersession cycle detected at {current!r}")
                seen.add(current)
                item = by_id[current]
                current = item.superseded_by if item.status == "superseded" else ""

    def get(self, item_id: str) -> WorkItem | None:
        return self._by_id.get(item_id)

    @property
    def request_roots(self) -> tuple[WorkItem, ...]:
        return tuple(item for item in self.items if item.kind == "request")

    @property
    def unresolved_roots(self) -> tuple[WorkItem, ...]:
        return tuple(item for item in self.request_roots if item.status in UNRESOLVED_STATUSES)

    def active_frontier(self) -> tuple[WorkItem, ...]:
        """Return every unresolved unit, including children below a delivered progress response."""
        return tuple(item for item in self.items if item.status in UNRESOLVED_STATUSES)

    def dependency_closure(self, roots: Iterable[str] | None = None) -> tuple[WorkItem, ...]:
        """Return roots plus transitive dependencies in stable graph order."""
        if isinstance(roots, (str, bytes)):
            raise GraphValidationError("dependency closure roots must be a sequence of item IDs")
        root_ids = tuple(roots) if roots is not None else tuple(item.id for item in self.active_frontier())
        if any(not isinstance(item_id, str) or item_id not in self._by_id for item_id in root_ids):
            raise GraphValidationError("dependency closure roots must name existing work items")
        selected: set[str] = set()
        pending = list(root_ids)
        while pending:
            item_id = pending.pop()
            if item_id in selected:
                continue
            selected.add(item_id)
            # ``root_id`` is an ownership edge. Include it even when a child does not redundantly list its
            # request root as an ordinary dependency.
            selected.add(self._by_id[item_id].root_id)
            pending.extend(self._by_id[item_id].dependencies)
        return tuple(item for item in self.items if item.id in selected)

    def validate_sources(self, sources: Mapping[str, str]) -> None:
        """Validate every source locator against an immutable-event lookup."""
        if not isinstance(sources, Mapping):
            raise SourceMismatchError("source lookup must be a mapping")
        for item in self.items:
            for ref in item.source_refs:
                if ref.event_id not in sources:
                    raise SourceMismatchError(f"source event {ref.event_id!r} is unavailable")
                ref.extract(sources[ref.event_id])

    def apply(self, delta: WorkDelta) -> "WorkGraph":
        """Validate and atomically apply one model-authored delta."""
        if not isinstance(delta, WorkDelta):
            raise TypeError("WorkGraph.apply requires a WorkDelta")
        if delta.expected_revision != self.revision:
            raise RevisionConflictError(
                f"work delta expected revision {delta.expected_revision}, current revision is {self.revision}",
            )
        existing = dict(self._by_id)
        for item in delta.creates:
            if item.id in existing:
                raise GraphValidationError(f"cannot create existing work item {item.id!r}")
        for item in delta.updates:
            previous = existing.get(item.id)
            if previous is None:
                raise GraphValidationError(f"cannot update missing work item {item.id!r}")
            self._validate_update(previous, item)

        updates = {item.id: item for item in delta.updates}
        next_items = tuple(updates.get(item.id, item) for item in self.items) + delta.creates
        if next_items == self.items:
            return self
        return WorkGraph(items=next_items, revision=self.revision + 1)

    def apply_delta(self, delta: WorkDelta) -> "WorkGraph":
        """Explicit integration name for :meth:`apply`."""
        return self.apply(delta)

    @staticmethod
    def _validate_update(previous: WorkItem, current: WorkItem) -> None:
        if current.kind != previous.kind:
            raise GraphValidationError(f"work item {current.id!r} cannot change kind")
        if current.root_id != previous.root_id:
            raise GraphValidationError(f"work item {current.id!r} cannot change request root")
        if current.logical_id != previous.logical_id:
            raise GraphValidationError(f"work item {current.id!r} cannot change logical request identity")
        if current.workspace_epoch != previous.workspace_epoch:
            raise GraphValidationError(f"work item {current.id!r} cannot change its admission workspace epoch")
        if current.status not in _ALLOWED_TRANSITIONS[previous.status]:
            # The legal set is one lookup away and was withheld, so the caller guessed and burned a
            # call per guess. From a terminal state NOTHING is legal — say that outright instead of
            # offering a set the caller will read as a menu.
            # Derive from BOTH gates. _ALLOWED_TRANSITIONS is the GRAPH's table; the model is
            # additionally barred from the host-owned statuses, so advertising the graph set alone
            # offered delivered/verified/waiting_peer — 4 of 7 suggested moves refused on the very
            # next call. Advertising a move that is then refused is worse than naming none: it is the
            # advice-to-nowhere and retry-bait this message exists to prevent.
            legal = tuple(s for s in sorted(_ALLOWED_TRANSITIONS[previous.status])
                          if s != previous.status and s in _MODEL_WORK_STATUSES)
            hint = (f"; from {previous.status!r} the legal next statuses are: {', '.join(legal)}"
                    if legal else
                    f"; {previous.status!r} is TERMINAL — no status change is possible, "
                    "create a new item instead")
            raise GraphValidationError(
                f"invalid work status transition for {current.id!r}: "
                f"{previous.status} -> {current.status}{hint}",
            )
        if not set(previous.source_refs).issubset(current.source_refs):
            raise GraphValidationError(f"work item {current.id!r} cannot erase source provenance")
        if not set(previous.dependencies).issubset(current.dependencies):
            raise GraphValidationError(f"work item {current.id!r} cannot erase dependency edges")
        if not set(previous.resource_refs).issubset(current.resource_refs):
            raise GraphValidationError(f"work item {current.id!r} cannot erase resource references")
        if not set(previous.evidence_refs).issubset(current.evidence_refs):
            raise GraphValidationError(f"work item {current.id!r} cannot erase evidence references")
        if not set(previous.output_refs).issubset(current.output_refs):
            raise GraphValidationError(f"work item {current.id!r} cannot erase output references")

    def open_request(self, source_artifact: str, text: str, *, workspace_epoch: int = 0,
                     logical_id: str | None = None, item_id: str | None = None) -> "WorkGraph":
        """Mechanically create exactly one request root for one exact user event.

        Retrying the same event is idempotent and does not advance the revision.  No NLP,
        classification, or model paraphrase participates in request admission.
        """
        candidate = request_root_item(
            source_artifact,
            text,
            workspace_epoch=workspace_epoch,
            logical_id=logical_id,
            item_id=item_id,
        )
        for root in self.request_roots:
            if root.logical_id == candidate.logical_id or any(
                    ref.event_id == source_artifact for ref in root.source_refs):
                # Lifecycle/output/evidence fields legitimately change after admission.  A retry is the same
                # admission when every immutable identity/provenance field still matches; comparing the whole
                # root to a fresh ``open`` candidate would make a crash retry fail after any progress transition.
                same_admission = (
                    root.id == candidate.id
                    and root.root_id == candidate.root_id
                    and root.logical_id == candidate.logical_id
                    and root.workspace_epoch == candidate.workspace_epoch
                    and set(candidate.source_refs).issubset(root.source_refs)
                )
                if same_admission:
                    return self
                raise GraphValidationError(
                    f"source/logical request {source_artifact!r}/{candidate.logical_id!r} "
                    f"already owns request root {root.id!r}",
                )
        if candidate.id in self._by_id:
            raise GraphValidationError(f"request-root ID {candidate.id!r} already belongs to another item")
        # Terminal roots are durable in the event/artifact stores, not resident work. Drop their complete
        # ownership subgraphs when a distinct request arrives so checkpoint size follows unresolved work rather
        # than elapsed turns. Existing-source idempotency is checked above before this compaction.
        terminal_roots = {
            root.id for root in self.request_roots if root.status not in UNRESOLVED_STATUSES
        }
        base = self
        if terminal_roots:
            kept = tuple(item for item in self.items if item.root_id not in terminal_roots)
            base = WorkGraph(items=kept, revision=self.revision, version=self.version)
        return base.apply(WorkDelta(expected_revision=self.revision, creates=(candidate,)))

    def add_request_root(self, event_id: str, utterance: str, *, item_id: str | None = None) -> "WorkGraph":
        """Backward-compatible spelling for callers that do not yet carry segment identity."""
        return self.open_request(event_id, utterance, item_id=item_id)

    def upsert(self, item: WorkItem, *, expected_revision: int | None = None) -> "WorkGraph":
        """Create or replace one item through the same validated delta boundary."""
        if not isinstance(item, WorkItem):
            raise TypeError("WorkGraph.upsert requires a WorkItem")
        revision = self.revision if expected_revision is None else expected_revision
        if item.id in self._by_id:
            if self._by_id[item.id] == item and revision == self.revision:
                return self
            return self.apply_delta(WorkDelta(expected_revision=revision, updates=(item,)))
        return self.apply_delta(WorkDelta(expected_revision=revision, creates=(item,)))

    def transition(self, item_id: str, status: WorkStatus, *,
                   evidence_refs: Iterable[EvidenceRef] = (),
                   output_refs: Iterable[OutputRef] = (), superseded_by: str = "",
                   stop_reason: str | None = None,
                   expected_revision: int | None = None) -> "WorkGraph":
        """Transition one item while append-only references remain mechanically preserved."""
        current = self.get(item_id)
        if current is None:
            raise GraphValidationError(f"cannot transition missing work item {item_id!r}")
        evidence = _record_tuple(evidence_refs, EvidenceRef, "transition.evidence_refs")
        outputs = _record_tuple(output_refs, OutputRef, "transition.output_refs")
        updated = replace(
            current,
            status=status,
            evidence_refs=tuple(dict.fromkeys(current.evidence_refs + evidence)),
            output_refs=tuple(dict.fromkeys(current.output_refs + outputs)),
            superseded_by=superseded_by,
            stop_reason=current.stop_reason if stop_reason is None else stop_reason,
        )
        return self.upsert(updated, expected_revision=expected_revision)

    def seal_current(self, stop_reason: str, response_ref: OutputRef | None = None, *,
                     transitioned: bool = False, logical_id: str | None = None,
                     expected_revision: int | None = None,
                     peer_wait: "PeerWait | None" = None,
                     resolve_peer_wait: bool = False) -> "WorkGraph":
        """Seal one runtime segment without confusing transport with task completion.

        A context/workspace transition keeps the request ``in_progress`` even if a progress
        response was emitted.  ``waiting_user`` is the one explicit stop reason that keeps
        the request pending on dialogue.  Other stops with a response are delivered; stops
        without a response remain active for recovery.
        """
        _text(stop_reason, "seal stop_reason")
        if response_ref is not None and not isinstance(response_ref, OutputRef):
            raise TypeError("seal_current response_ref must be an OutputRef or None")
        candidates = [item for item in self.unresolved_roots
                      if logical_id is None or item.logical_id == logical_id]
        if not candidates:
            raise GraphValidationError("there is no unresolved request root to seal")
        current = candidates[-1]
        deliver_ready = bool(
            response_ref is not None and not transitioned
            and stop_reason not in ("waiting_user", "waiting_peer")
        )
        ready_children = tuple(
            item for item in self.items
            if item.id != current.id and item.root_id == current.id and item.status == "ready"
        ) if deliver_ready else ()
        unresolved_children = any(
            item.id != current.id
            and item.root_id == current.id
            and item.status in UNRESOLVED_STATUSES
            and item.status != "ready"
            for item in self.items
        )
        # A peer park is DURABLE REQUEST state, not per-segment state. The status and its typed
        # `peer_wait` must move together by construction: the old code let the fallthrough carry
        # `waiting_peer` forward while unconditionally forcing `peer_wait=None`, which either
        # tripped the biconditional (re-sealing a parked root raised GraphValidationError for
        # every stop reason, escaping _seal_local_turn's TurnCommitted(ok=False) lane) or, with a
        # response ref, silently destroyed the park so the peer's eventual reply could never land.
        # Resolution is now always EXPLICIT: pass `resolve_peer_wait=True` (or resume through
        # resume_waiting_peer). Anything else preserves the park.
        # `resolve_peer_wait` is an authority flag, so it must be an exact bool: a truthy
        # string/int from a wire or a caller's kwargs must not silently discard a durable park.
        if not isinstance(resolve_peer_wait, bool):
            raise GraphValidationError("seal_current resolve_peer_wait must be a bool")
        parked = current.status == "waiting_peer"
        # Park preservation is checked FIRST, ahead of `transitioned`. The CLI passes
        # transitioned=True for a workspace handoff, and an earlier ordering let that branch
        # silently drop a durable park (in_progress + peer_wait=None) even with
        # resolve_peer_wait=False — a workspace transition must not destroy a peer wait.
        if parked and not resolve_peer_wait and stop_reason != "waiting_peer":
            status: WorkStatus = "waiting_peer"
        elif transitioned:
            status = "in_progress"
        elif stop_reason == "waiting_user":
            status = "waiting_user"
        elif stop_reason == "waiting_peer":
            status = "waiting_peer"
        elif response_ref is not None and not unresolved_children:
            status = "delivered"
        elif current.status in ("open", "waiting_peer"):
            status = "in_progress"
        else:
            status = current.status
        if status == "waiting_peer":
            # Carry the existing park unless this seal supplies a new one; never leave the
            # status set without its typed correlation state.
            next_peer_wait = peer_wait if peer_wait is not None else current.peer_wait
            if next_peer_wait is None:
                raise GraphValidationError(
                    "sealing waiting_peer requires typed PeerWait correlation state"
                )
        else:
            next_peer_wait = None
        outputs = (response_ref,) if response_ref is not None else ()
        updated_root = replace(
            current,
            status=status,
            output_refs=tuple(dict.fromkeys(current.output_refs + outputs)),
            stop_reason=stop_reason,
            peer_wait=next_peer_wait,
        )
        updated_children = tuple(
            replace(
                child,
                status="delivered",
                output_refs=tuple(dict.fromkeys(child.output_refs + outputs)),
                stop_reason=stop_reason,
            )
            for child in ready_children
        )
        revision = self.revision if expected_revision is None else expected_revision
        return self.apply_delta(WorkDelta(
            expected_revision=revision,
            updates=(*updated_children, updated_root),
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.version,
            "revision": self.revision,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkGraph":
        if not isinstance(value, Mapping):
            raise GraphValidationError("work graph must be an object")
        items = value.get("items", ())
        if items is None:
            items = ()
        if isinstance(items, (str, bytes, Mapping)):
            raise GraphValidationError("work_graph.items must be a sequence")
        try:
            parsed = tuple(WorkItem.from_dict(item) for item in items)
        except TypeError as exc:
            raise GraphValidationError("work_graph.items must contain objects") from exc
        return cls(
            items=parsed,
            revision=value.get("revision", 0),
            version=value.get("v", WORK_GRAPH_VERSION),
        )

    def to_records(self) -> list[dict[str, Any]]:
        """Return checkpoint-friendly records without losing graph revision/version."""
        return [
            {"record_type": "active_work_graph", "v": self.version, "revision": self.revision},
            *(dict(item.to_dict(), record_type="work_item") for item in self.items),
        ]

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, Any]] | None) -> "WorkGraph":
        """Rebuild from checkpoint records; absence means a pre-Active-Work checkpoint."""
        if records is None:
            return cls()
        records = _wire_sequence(records, "active-work records")
        if not records:
            return cls()
        if any(not isinstance(record, Mapping) for record in records):
            raise GraphValidationError("active-work records must contain objects")
        header = records[0]
        if header.get("record_type") != "active_work_graph":
            raise GraphValidationError("active-work records are missing the graph header")
        items = []
        for record in records[1:]:
            if record.get("record_type") != "work_item":
                raise GraphValidationError("active-work record has an unsupported record_type")
            value = dict(record)
            value.pop("record_type", None)
            items.append(WorkItem.from_dict(value))
        return cls(
            items=tuple(items),
            revision=header.get("revision", 0),
            version=header.get("v", WORK_GRAPH_VERSION),
        )

    @property
    def digest(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_work_delta(
    graph: WorkGraph,
    args: dict,
    *,
    logical_id: str,
    workspace_epoch: int,
    verified_ok: frozenset = frozenset(),
) -> WorkDelta:
    """Normalize the public ``update_work`` shape into the graph contract.

    ``verified_ok`` is host-only: item ids whose ``verify`` commands the host
    has just run green.  A change landing such an item on ``ready`` is promoted
    to ``verified``; the model still cannot supply ``verified`` directly.
    """
    if not isinstance(graph, WorkGraph):
        raise ValueError("ACTIVE WORK is unavailable")
    expected = args.get("expected_revision")
    if expected is None:
        # Defaulting an omitted token to the LIVE revision made the conflict check below vacuous:
        # a delta authored against a graph that has since moved applied silently. The token must
        # state the revision the AUTHOR saw, so omission is a hard reject, with the escape named.
        raise ValueError(
            "expected_revision is required: echo the 'graph revision' shown in ACTIVE WORK, or the "
            f"one your latest accepted update_work returned (the graph is at revision {graph.revision})"
        )
    if not isinstance(expected, int) or isinstance(expected, bool):
        raise ValueError("expected_revision must be an integer")
    changes = args.get("changes")
    if not isinstance(changes, list) or not changes or len(changes) > 32:
        raise ValueError("changes must contain 1..32 work-item objects")
    roots = [root for root in graph.unresolved_roots if not logical_id or root.logical_id == logical_id]
    if not roots:
        raise ValueError("no active request root is available for update_work")
    root = roots[-1]
    creates, updates = [], []
    seen: set[str] = set()
    for raw in changes:
        if not isinstance(raw, dict):
            raise ValueError("each work change must be an object")
        item_id = str(raw.get("id") or "").strip()
        if not item_id or len(item_id) > 120 or item_id in seen:
            raise ValueError("each work change needs a unique ID of at most 120 characters")
        seen.add(item_id)
        previous = graph.get(item_id)
        # Omission means "leave this field alone" for an existing record.
        status = str(raw.get("status") or (previous.status if previous is not None else "open"))
        if status not in _MODEL_WORK_STATUSES:
            host_owned = {
                "delivered": "the host sets it when the turn delivers a response",
                "verified": "the host sets it when your verify commands run green",
                "waiting_peer": "the host sets it when a turn parks on a peer",
            }
            if status in host_owned:
                raise ValueError(
                    f"work item {item_id!r}: the model cannot set delivered/verified/waiting_peer — "
                    f"{status!r} is HOST-OWNED, {host_owned[status]}. "
                    f"Set 'ready' instead and let the host promote it"
                )
            raise ValueError(
                f"work item {item_id!r}: unknown work status {status!r}; you may set "
                f"{', '.join(sorted(_MODEL_WORK_STATUSES))}"
            )
        add_dependencies = raw.get("add_dependencies") or []
        if not isinstance(add_dependencies, list) or any(
            not isinstance(value, str) or not value.strip() for value in add_dependencies
        ):
            raise ValueError("add_dependencies must be a list of non-empty work-item IDs")
        resource_rows = raw.get("add_resources") or []
        if not isinstance(resource_rows, list) or len(resource_rows) > 32:
            raise ValueError("add_resources must be a list of at most 32 objects")
        resources = []
        for resource in resource_rows:
            if not isinstance(resource, dict):
                raise ValueError("each resource must be an object")
            resources.append(ResourceRef(
                str(resource.get("kind") or ""),
                str(resource.get("ref") or ""),
                workspace_epoch=int(workspace_epoch),
                revision=str(resource.get("revision") or ""),
            ))
        superseded_by = str(
            raw.get("superseded_by")
            or (previous.superseded_by if previous is not None else "")
        ).strip()
        if status == "superseded" and not superseded_by:
            raise ValueError("superseded work must name superseded_by")
        verify_rows = raw.get("verify")
        if verify_rows is not None and (
            not isinstance(verify_rows, list)
            or any(not isinstance(cmd, str) for cmd in verify_rows)
        ):
            raise ValueError("verify must be a list of shell command strings")
        done_when = raw.get("done_when")
        if done_when is not None and not isinstance(done_when, str):
            raise ValueError("done_when must be a string")
        if previous is None:
            description = str(raw.get("description") or "").strip()
            if not description:
                raise ValueError("new work items require a non-empty description")
            host_verify_proof = ()
            host_output_proof = ()
            if status == "ready" and item_id in verified_ok:
                status = "verified"
                host_verify_proof = (EvidenceRef("verify_receipt", f"host-verify:{item_id}"),)
                host_output_proof = (OutputRef("verified_checks", f"host-verify:{item_id}"),)
            creates.append(WorkItem(
                id=item_id,
                root_id=root.id,
                source_refs=root.source_refs,
                description=description,
                status=status,
                logical_id=root.logical_id,
                workspace_epoch=int(workspace_epoch),
                dependencies=tuple(dict.fromkeys(add_dependencies)),
                resource_refs=tuple(dict.fromkeys(resources)),
                superseded_by=superseded_by,
                verify=tuple(dict.fromkeys(cmd.strip() for cmd in (verify_rows or []) if cmd.strip())),
                done_when=str(done_when or "").strip(),
                evidence_refs=host_verify_proof,
                output_refs=host_output_proof,
            ))
            continue
        if previous.kind == "request":
            if previous.id == root.id or status not in {"cancelled", "superseded"}:
                raise ValueError(
                    "update_work may only cancel/supersede an older request root; the current root is host-owned"
                )
            if status == "superseded" and superseded_by != root.id:
                raise ValueError("an older request root may be superseded only by the current request root")
            updates.append(replace(
                previous,
                status=status,
                superseded_by=superseded_by,
                peer_wait=None,
            ))
            updates.extend(
                replace(
                    child,
                    status="cancelled",
                    superseded_by="",
                    stop_reason=f"request_{status}",
                    peer_wait=None,
                )
                for child in graph.items
                if child.id != previous.id
                and child.root_id == previous.id
                and child.status in UNRESOLVED_STATUSES
            )
            continue
        if previous.root_id != root.id:
            raise ValueError(
                f"work item {item_id!r} belongs to an EARLIER request, and items are owned by the "
                "request that created them. Nothing is lost — the item is still on record. Two legal "
                "moves, and the SECOND is usually right: (1) create a fresh item under the current "
                "request with the same description and re-state its verify/dependencies — a new item "
                "starts unverified, so evidence already earned does not carry; or (2) in the SAME "
                "batch, supersede the earlier ROOT (superseded_by = the current root) and re-create "
                "what still matters — that retires the old request instead of leaving it unfinished "
                "forever. Do not retry this update — it cannot succeed."
            )
        extra_evidence = ()
        extra_outputs = ()
        if status == "ready" and item_id in verified_ok:
            status = "verified"
            extra_evidence = (EvidenceRef("verify_receipt", f"host-verify:{item_id}"),)
            extra_outputs = (OutputRef("verified_checks", f"host-verify:{item_id}"),)
        updates.append(replace(
            previous,
            description=str(raw.get("description", previous.description)).strip(),
            status=status,
            dependencies=tuple(dict.fromkeys((*previous.dependencies, *add_dependencies))),
            resource_refs=tuple(dict.fromkeys((*previous.resource_refs, *resources))),
            evidence_refs=tuple(dict.fromkeys((*previous.evidence_refs, *extra_evidence))),
            output_refs=tuple(dict.fromkeys((*previous.output_refs, *extra_outputs))),
            superseded_by=superseded_by,
            verify=(
                tuple(dict.fromkeys(cmd.strip() for cmd in verify_rows if cmd.strip()))
                if verify_rows is not None
                else previous.verify
            ),
            done_when=(str(done_when).strip() if done_when is not None else previous.done_when),
        ))
    return WorkDelta(expected_revision=expected, creates=tuple(creates), updates=tuple(updates))


def plan_progress_payload(graph: WorkGraph, logical_id: str) -> dict[str, object]:
    """Project the current request's Active Work into UI-only plan position."""
    roots = [
        root for root in graph.request_roots
        if not logical_id or root.logical_id == logical_id
    ]
    if not roots:
        return {"total": 0, "done": 0, "current": "", "current_index": 0}
    root = roots[-1]
    items = [
        item for item in graph.items
        if item.kind != "request"
        and item.root_id == root.id
        and item.status not in {"cancelled", "superseded"}
    ]
    done = sum(item.status == "verified" for item in items)
    current = None
    for wanted in ("in_progress", "waiting_user", "waiting_peer", "open", "ready"):
        current = next((item for item in items if item.status == wanted), None)
        if current is not None:
            break
    return {
        "total": len(items),
        "done": done,
        "current": current.description if current is not None else "",
        "current_index": items.index(current) + 1 if current is not None else 0,
        "items": [
            {
                "id": item.id,
                "status": item.status,
                "description": item.description,
                "done_when": item.done_when,
                "host_verified": (
                    item.status == "verified"
                    and any(
                        ref.kind == "verify_receipt" and ref.ref == f"host-verify:{item.id}"
                        for ref in item.evidence_refs
                    )
                ),
            }
            for item in items
        ],
    }


def request_root_item(event_id: str, utterance: str, *, workspace_epoch: int = 0,
                      logical_id: str | None = None, item_id: str | None = None) -> WorkItem:
    """Create the canonical, non-semantic request root for one exact user utterance."""
    _text(event_id, "request event_id")
    if not isinstance(utterance, str) or not utterance:
        raise GraphValidationError("request utterance must be a non-empty string")
    _integer(workspace_epoch, "request workspace_epoch")
    if logical_id is None:
        logical_id = event_id
    _text(logical_id, "request logical_id")
    if item_id is None:
        item_id = f"request-{hashlib.sha256(logical_id.encode('utf-8')).hexdigest()[:24]}"
    _text(item_id, "request item_id")
    source = SourceRef.bind(event_id, utterance)
    return WorkItem(
        id=item_id,
        root_id=item_id,
        kind="request",
        status="open",
        description="",
        logical_id=logical_id,
        workspace_epoch=workspace_epoch,
        source_refs=(source,),
    )


__all__ = [
    "ActiveWorkError",
    "EvidenceRef",
    "GraphValidationError",
    "OutputRef",
    "ResourceRef",
    "RevisionConflictError",
    "SOURCE_REF_VERSION",
    "SourceMismatchError",
    "SourceRef",
    "UNRESOLVED_STATUSES",
    "WORK_GRAPH_VERSION",
    "WORK_KINDS",
    "WORK_STATUSES",
    "WorkDelta",
    "WorkGraph",
    "WorkItem",
    "WorkKind",
    "WorkStatus",
    "request_root_item",
    "expire_peer_waits",
    "resume_waiting_peer",
]


# AUTHORITY BOUNDARY (task #101). The work graph's peer park is a PROJECTION of the durable
# collaboration state: it records that this request is waiting and on what correlation, so the
# frontier renders truthfully and the wait survives restart. It is NOT the terminal authority.
# The host's PeerParkStore decides resume/expire/cancel under a generation-fenced CAS; this graph
# follows that decision. Anything here that starts deciding terminal outcomes has created a second
# source of truth, which is how a park and its store disagree after a crash.
def expire_peer_waits(
    graph: "WorkGraph",
    elapsed_by_correlation: Mapping[str, float],
    *,
    expected_revision: int | None = None,
) -> tuple["WorkGraph", tuple[str, ...]]:
    """Unpark every ``waiting_peer`` request whose deadline has passed.

    Without this, a park is a permanent trap: ``PeerWait.deadline_s`` was validated and
    serialized but never compared against anything, so an unanswered peer wait would wait
    forever with nothing surfacing the overdue deadline. A park is only as good as the
    thing that reaps it.

    The kernel deliberately does NOT read a clock. ``deadline_s`` is a DURATION, and the
    elapsed time per correlation is supplied by the host — the same discipline as
    ``correlate_peer_result(..., elapsed_s=...)``. That keeps core logic deterministic and
    replayable, and keeps wall-time authority with the host that owns it.

    Returns the updated graph and the correlation ids that were expired. An expired park
    returns the request to ``in_progress`` with a typed ``peer_wait_expired`` stop reason —
    the work is live again and the frontier can converge — never silently to ``delivered``.
    """
    if not isinstance(elapsed_by_correlation, Mapping):
        raise ActiveWorkError("expire_peer_waits requires a mapping of elapsed seconds")
    updates: list[WorkItem] = []
    expired: list[str] = []
    for item in graph.unresolved_roots:
        if item.status != "waiting_peer":
            continue
        wait = item.peer_wait
        if wait is None or wait.deadline_s is None:
            continue                      # an unbounded park never expires by time
        if wait.correlation_id not in elapsed_by_correlation:
            continue                      # the host has no elapsed reading for this park
        raw = elapsed_by_correlation[wait.correlation_id]
        # Same hostile-input discipline as correlate_peer_result: a NaN elapsed defeats every
        # `>` comparison and would silently keep the park alive forever.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ActiveWorkError("expire_peer_waits elapsed seconds must be numbers")
        try:
            elapsed = float(raw)
        except (OverflowError, ValueError) as exc:
            raise ActiveWorkError("expire_peer_waits elapsed seconds must be finite") from exc
        if not _math.isfinite(elapsed) or elapsed < 0.0:
            raise ActiveWorkError("expire_peer_waits elapsed seconds must be finite and non-negative")
        if elapsed <= float(wait.deadline_s):
            continue                      # inclusive boundary, matching correlate_peer_result
        updates.append(replace(
            item,
            status="in_progress",
            stop_reason="peer_wait_expired",
            peer_wait=None,
        ))
        expired.append(wait.correlation_id)
    if not updates:
        return graph, ()
    revision = graph.revision if expected_revision is None else expected_revision
    return (
        graph.apply_delta(WorkDelta(expected_revision=revision, updates=tuple(updates))),
        tuple(expired),
    )


def resume_waiting_peer(
    graph: "WorkGraph",
    result: PeerResult,
    *,
    logical_id: str | None = None,
    expected_revision: int | None = None,
) -> "WorkGraph":
    """Resume a ``waiting_peer``-parked request on a correlated peer result.

    Only a ``PeerResult`` whose ``correlation_id`` matches the park's ``PeerWait``
    resumes the request (back to ``in_progress`` with the durable wait cleared). A
    mismatch — or no parked request — raises rather than silently resuming unrelated
    work, mirroring the exact-correlation discipline of the confidential lane.
    """
    if not isinstance(result, PeerResult):
        raise ActiveWorkError("resume_waiting_peer requires a PeerResult")
    parked = [
        item for item in graph.unresolved_roots
        if item.status == "waiting_peer"
        and (logical_id is None or item.logical_id == logical_id)
    ]
    if not parked:
        raise ActiveWorkError("no waiting_peer request is parked for this correlation")
    current = parked[-1]
    wait = current.peer_wait
    if wait is None or not result.correlation_id or result.correlation_id != wait.correlation_id:
        raise ActiveWorkError(
            "peer result correlation does not match the parked request"
        )
    # Correlation alone is not authority: the result must also come from the expected peer.
    # A matching correlation ID from a different peer_id must not resume the request.
    if wait.peer_id and result.peer_id != wait.peer_id:
        raise ActiveWorkError(
            "peer result sender does not match the parked request's expected peer"
        )
    revision = graph.revision if expected_revision is None else expected_revision
    resumed = replace(
        current,
        status="in_progress",
        stop_reason="",
        peer_wait=None,
    )
    return graph.apply_delta(WorkDelta(expected_revision=revision, updates=(resumed,)))


# Public since the SDK surface audit (2026-08-02): hosts legitimately need the model-visible
# status set and the plan-progress projection. Private aliases kept for in-repo callers.
_MODEL_WORK_STATUSES = MODEL_WORK_STATUSES
_plan_progress_payload = plan_progress_payload
