from __future__ import annotations


class CorruptCandidateStateError(Exception):
    """En lagrad candidate-rad kunde inte deserialiseras till ett giltigt
    Candidate-objekt. `corrupted_field` anger var i deserialiseringskedjan
    felet upptäcktes: "evidence_record", "timestamp", eller "status"/
    "candidate" (se SQLiteRepository.get_candidate — Task 10)."""

    def __init__(self, candidate_id: str, raw_status: str, corrupted_field: str):
        self.candidate_id = candidate_id
        self.raw_status = raw_status
        self.corrupted_field = corrupted_field
        super().__init__(
            f"candidate {candidate_id} has corrupt persisted data in field "
            f"{corrupted_field!r} (raw status={raw_status!r})"
        )
