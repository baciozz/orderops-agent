"""Accept or drop a sub-agent envelope.

Schema retries and DispatchStep rows live in the dispatcher. A proposal is
accepted only when both the fencing token and the intent revision still match.
"""

from schemas.subagent import AgentProposal

from app.errors import ProposalRejected


def accept_proposal(
    proposal: AgentProposal,
    *,
    expected_fencing_token: int,
    expected_intent_revision: int,
) -> None:
    if (
        proposal.fencing_token != expected_fencing_token
        or proposal.intent_revision != expected_intent_revision
    ):
        raise ProposalRejected(
            "提案的 fencing_token 或 intent_revision 与当前 attempt 不一致，已丢弃",
            details={
                "fencing_token": proposal.fencing_token,
                "intent_revision": proposal.intent_revision,
            },
        )
