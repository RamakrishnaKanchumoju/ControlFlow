from dataclasses import Field
from typing import Annotated, Literal

from pydantic.functional_serializers import PlainSerializer

from controlflow.agents.agent import Agent
from controlflow.events.base import UnpersistedEvent
from controlflow.orchestration.orchestrator import Orchestrator


class OrchestratorStart(UnpersistedEvent):
    event: Literal["orchestrator-start"] = "orchestrator-start"
    persist: bool = False
    orchestrator: Orchestrator


class OrchestratorEnd(UnpersistedEvent):
    event: Literal["orchestrator-end"] = "orchestrator-end"
    persist: bool = False
    orchestrator: Orchestrator


class OrchestratorError(UnpersistedEvent):
    event: Literal["orchestrator-error"] = "orchestrator-error"
    persist: bool = False
    orchestrator: Orchestrator
    error: Annotated[Exception, PlainSerializer(lambda x: str(x), return_type=str)]


class AgentTurnStart(UnpersistedEvent):
    event: Literal["agent-turn-start"] = "agent-turn-start"
    persist: bool = False
    orchestrator: Orchestrator
    agent: Agent


class AgentTurnEnd(UnpersistedEvent):
    event: Literal["agent-turn-end"] = "agent-turn-end"
    persist: bool = False
    orchestrator: Orchestrator
    agent: Agent
