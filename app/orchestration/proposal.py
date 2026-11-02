"""Accept or drop a sub-agent envelope.

TODO(stage-1C): validate payload against the agent's output schema, retry on
schema failure (R19), and persist a DispatchStep.
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
