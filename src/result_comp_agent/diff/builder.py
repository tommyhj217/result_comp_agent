"""Change 객체 생성기 — 비교쌍 내에서 결정론적 순번 ID 를 부여한다."""
from __future__ import annotations

from typing import Any, Iterable, Optional

from ..models import Anchor, ArtifactType, Change, ChangeType, Location


class ChangeBuilder:
    def __init__(self, pair_id: str, agent_id: str, artifact: ArtifactType):
        self.pair_id = pair_id
        self.agent_id = agent_id
        self.artifact = artifact
        self.changes: list[Change] = []

    def add(
        self,
        change_type: ChangeType,
        location: Location,
        *,
        before: Optional[str] = None,
        after: Optional[str] = None,
        format_only: bool = False,
        pairing: str = "n/a",
        pairing_score: float = 1.0,
        detail: Optional[dict[str, Any]] = None,
        anchors: Iterable[Anchor] = (),
    ) -> Change:
        seq = len(self.changes) + 1
        uniq: list[Anchor] = []
        seen = set()
        for a in anchors:
            if a is None or not a.text or not a.text.strip():
                continue
            k = (a.text.strip(), a.role)
            if k in seen:
                continue
            seen.add(k)
            uniq.append(Anchor(text=a.text.strip(), role=a.role))
        ch = Change(
            id=f"{self.pair_id}:C{seq:04d}",
            pair_id=self.pair_id,
            agent_id=self.agent_id,
            artifact=self.artifact,
            change_type=change_type,
            location=location,
            before=before,
            after=after,
            format_only=format_only,
            pairing=pairing,
            pairing_score=round(float(pairing_score), 4),
            detail=detail or {},
            anchors=uniq,
        )
        self.changes.append(ch)
        return ch
