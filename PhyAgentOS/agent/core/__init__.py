"""Public contracts and concrete components for the PhyAgentOS agent core.

The data contracts and Protocols live in :mod:`events`, :mod:`types`, and
:mod:`protocols`; the seven runtime components are re-exported here so an
entry point or a test can assemble them from one place. None of them import
the legacy ``agent.loop``, Forge, verification, or skill runtime.

Protocol names stay the public contract names. When a concrete class would
shadow a Protocol (only ``ModelAdapter`` today), the implementation is
exported under an ``Impl`` name instead:

* :class:`AgentHost` -> :class:`AgentHostImpl`
* :class:`AgentRuntime` -> :class:`AgentRuntimeImpl`
* :class:`LoopDriver` -> :class:`LoopDriverImpl`
* :class:`ModelAdapter` -> :class:`ModelAdapterImpl`
* :class:`PromptAssembler` -> :class:`PromptAssemblerImpl`
* :class:`SessionStore` -> :class:`SQLiteSessionStore`
* :class:`ToolExecutor` -> :class:`ToolExecutorImpl`
"""

from .events import CallKind, EventType, RecoveryReport, RuntimeEvent, UnfinishedCall
from .host import AgentHostImpl
from .loop import LoopDriverImpl
from .model import ModelAdapter as _ProviderModelAdapter
from .prompt import PromptAssemblerImpl
from .protocols import (
    AgentHost,
    AgentRuntime,
    Capability,
    LoopDriver,
    ModelAdapter,
    PromptAssembler,
    SessionStore,
    ToolExecutor,
)
from .runtime import AgentRuntimeImpl
from .session import SQLiteSessionStore
from .tools import ToolExecutorImpl
from .types import (
    Budget,
    CancellationToken,
    DomainRefs,
    ExecutionContext,
    ModelCallError,
    ModelRequest,
    ModelResponse,
    OperationCancelledError,
    PromptBundle,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolHandler,
    ToolOutcome,
    ToolPolicy,
    ToolRegistration,
    ToolStatus,
    TurnRequest,
    TurnResult,
    TurnStart,
    TurnStatus,
)

# The provider-backed adapter class is named ``ModelAdapter`` in ``model.py``;
# export it under an unambiguous name so the Protocol keeps that name here.
ModelAdapterImpl = _ProviderModelAdapter

__all__ = [
    "AgentHost",
    "AgentHostImpl",
    "AgentRuntime",
    "AgentRuntimeImpl",
    "Budget",
    "CallKind",
    "CancellationToken",
    "Capability",
    "DomainRefs",
    "EventType",
    "ExecutionContext",
    "LoopDriver",
    "LoopDriverImpl",
    "ModelAdapter",
    "ModelAdapterImpl",
    "ModelCallError",
    "ModelRequest",
    "ModelResponse",
    "OperationCancelledError",
    "PromptAssembler",
    "PromptAssemblerImpl",
    "PromptBundle",
    "RecoveryReport",
    "RuntimeEvent",
    "SessionStore",
    "SQLiteSessionStore",
    "ToolCall",
    "ToolDefinition",
    "ToolError",
    "ToolExecutor",
    "ToolExecutorImpl",
    "ToolHandler",
    "ToolOutcome",
    "ToolPolicy",
    "ToolRegistration",
    "ToolStatus",
    "TurnRequest",
    "TurnResult",
    "TurnStart",
    "TurnStatus",
    "UnfinishedCall",
]
