"""Sent 对账共用的无副作用证据决策与时间边界。"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone


COMPOSITE_EVIDENCE_WINDOW_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class ReconciliationDecision:
    status: str
    evidence_type: str
    confidence: str
    candidate_count: int
    candidate_id: str | None = None
    decision_reason: str = ""

    def as_result(self, *, id_key: str) -> dict[str, object]:
        result: dict[str, object] = {
            "status": self.status,
            "evidence_type": self.evidence_type,
            "confidence": self.confidence,
            "candidate_count": self.candidate_count,
            id_key: self.candidate_id,
        }
        if self.decision_reason:
            result["decision_reason"] = self.decision_reason
        return result


def decide_reconciliation_candidate(
    *,
    strong_matches: Mapping[str, Collection[str]],
    evidence_priority: Sequence[str],
    message_id_candidates: Collection[str] = (),
    composite_candidates: Collection[str] = (),
    message_id_override_evidence: Collection[str] = (),
) -> ReconciliationDecision:
    """Select only deterministic evidence; a lone Message-ID never matches."""
    normalized = {
        evidence: {str(candidate) for candidate in candidates if str(candidate)}
        for evidence, candidates in strong_matches.items()
    }
    message_candidates = {
        str(candidate) for candidate in message_id_candidates if str(candidate)
    }
    composite = {
        str(candidate) for candidate in composite_candidates if str(candidate)
    }
    ordered = [
        evidence
        for evidence in evidence_priority
        if normalized.get(evidence)
    ]
    ordered.extend(
        sorted(
            evidence
            for evidence, candidates in normalized.items()
            if candidates and evidence not in ordered
        )
    )
    if ordered:
        strongest_evidence = ordered[0]
        strongest_candidates = normalized[strongest_evidence]
        if len(strongest_candidates) != 1:
            return ReconciliationDecision(
                status="ambiguous",
                evidence_type=strongest_evidence,
                confidence="manual_review",
                candidate_count=len(strongest_candidates),
            )
        selected = next(iter(strongest_candidates))
        conflicts: set[str] = set()
        for evidence in ordered[1:]:
            candidates = normalized[evidence]
            if len(candidates) == 1 and selected not in candidates:
                conflicts.update(candidates)
            elif candidates and selected not in candidates:
                conflicts.update(candidates)
        if conflicts:
            return ReconciliationDecision(
                status="ambiguous",
                evidence_type="conflicting_strong_evidence",
                confidence="manual_review",
                candidate_count=len({selected, *conflicts}),
            )
        message_conflict = message_candidates and selected not in message_candidates
        if message_conflict and strongest_evidence not in set(
            message_id_override_evidence
        ):
            return ReconciliationDecision(
                status="ambiguous",
                evidence_type="conflicting_exact_evidence",
                confidence="manual_review",
                candidate_count=len({selected, *message_candidates}),
            )
        reason = "strong_evidence_overrode_message_id" if message_conflict else ""
        return ReconciliationDecision(
            status="matched",
            evidence_type=strongest_evidence,
            confidence="exact",
            candidate_count=1,
            candidate_id=selected,
            decision_reason=reason,
        )

    if len(message_candidates) > 1:
        return ReconciliationDecision(
            status="ambiguous",
            evidence_type="ambiguous_message_id_candidates",
            confidence="manual_review",
            candidate_count=len(message_candidates),
        )
    if len(message_candidates) == 1:
        selected = next(iter(message_candidates))
        if composite == {selected}:
            return ReconciliationDecision(
                status="matched",
                evidence_type="deterministic_message_composite",
                confidence="high",
                candidate_count=1,
                candidate_id=selected,
            )
        if composite and selected not in composite:
            return ReconciliationDecision(
                status="ambiguous",
                evidence_type="conflicting_composite_evidence",
                confidence="manual_review",
                candidate_count=len({selected, *composite}),
            )
        return ReconciliationDecision(
            status="unmatched",
            evidence_type="weak_message_id_only",
            confidence="none",
            candidate_count=1,
        )
    return ReconciliationDecision(
        status="unmatched",
        evidence_type="unmatched",
        confidence="none",
        candidate_count=0,
    )


def within_composite_evidence_window(first: str, second: str) -> bool:
    """Use an absolute seven-day window; missing or invalid times are weak evidence."""
    first_value = _parse_fact_time(first)
    second_value = _parse_fact_time(second)
    if first_value is None or second_value is None:
        return False
    return abs((first_value - second_value).total_seconds()) <= (
        COMPOSITE_EVIDENCE_WINDOW_SECONDS
    )


def request_content_fingerprints(
    *,
    subject: str,
    body_text: str,
    to_emails: Collection[str],
    cc_emails: Collection[str],
    bcc_emails: Collection[str],
    attachments: Collection[tuple[str, int, str]],
) -> set[str]:
    """Mirror the archive content fact for public and Provider-kept Bcc headers."""
    public_recipients = {
        str(address).strip().casefold()
        for address in (*to_emails, *cc_emails)
        if str(address).strip()
    }
    bcc_recipients = {
        str(address).strip().casefold()
        for address in bcc_emails
        if str(address).strip()
    }
    recipient_sets = [public_recipients]
    if bcc_recipients:
        recipient_sets.append(public_recipients | bcc_recipients)
    attachment_parts = sorted(
        f"{str(name).casefold()}:{int(size)}:{str(sha256).casefold()}"
        for name, size, sha256 in attachments
    )
    fingerprints: set[str] = set()
    for recipients in recipient_sets:
        material = "\n".join(
            (
                str(subject or "").strip(),
                str(body_text or "").replace("\r\n", "\n").strip(),
                ",".join(sorted(recipients)),
                "|".join(attachment_parts),
            )
        )
        fingerprints.add(hashlib.sha256(material.encode("utf-8")).hexdigest())
    return fingerprints


def _parse_fact_time(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed
