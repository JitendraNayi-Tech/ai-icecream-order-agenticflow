from app.agents import checkout, followup, intake, recommender, tracker
from app.agents.base import AgentSpec

AGENTS: dict[str, AgentSpec] = {
    a.name: a
    for a in (intake.AGENT, recommender.AGENT, checkout.AGENT, tracker.AGENT, followup.AGENT)
}
