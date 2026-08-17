from enum import StrEnum


class AIActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    EVALUATED = "EVALUATED"
    ALLOWED = "ALLOWED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    DENIED = "DENIED"
    BLOCKED = "BLOCKED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REVALIDATING = "REVALIDATING"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS = {
    AIActionStatus.PROPOSED: {
        AIActionStatus.EVALUATED, AIActionStatus.ALLOWED, AIActionStatus.FAILED,
    },
    AIActionStatus.EVALUATED: {
        AIActionStatus.ALLOWED, AIActionStatus.AWAITING_APPROVAL,
        AIActionStatus.DENIED, AIActionStatus.BLOCKED, AIActionStatus.FAILED,
    },
    AIActionStatus.ALLOWED: {AIActionStatus.EXECUTING, AIActionStatus.FAILED},
    AIActionStatus.AWAITING_APPROVAL: {
        AIActionStatus.APPROVED, AIActionStatus.REJECTED, AIActionStatus.EXPIRED,
        AIActionStatus.FAILED,
    },
    AIActionStatus.APPROVED: {AIActionStatus.REVALIDATING, AIActionStatus.FAILED},
    AIActionStatus.REVALIDATING: {
        AIActionStatus.EXECUTING, AIActionStatus.AWAITING_APPROVAL,
        AIActionStatus.DENIED, AIActionStatus.BLOCKED, AIActionStatus.FAILED,
    },
    AIActionStatus.EXECUTING: {AIActionStatus.SUCCEEDED, AIActionStatus.FAILED},
}


def assert_transition(current: str, target: AIActionStatus) -> None:
    current_status = AIActionStatus(current)
    if target not in ALLOWED_TRANSITIONS.get(current_status, set()):
        raise ValueError(f"AI action cannot transition from {current_status} to {target}.")
