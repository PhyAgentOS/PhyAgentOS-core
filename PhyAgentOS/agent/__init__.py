"""Agent package for the decoupled runtime core.

The new core lives in :mod:`PhyAgentOS.agent.core`, domain adapters such as
:mod:`PhyAgentOS.agent.capabilities.forge_capability` live in ``capabilities``,
and :mod:`PhyAgentOS.agent.composition` is the production composition root. The
legacy loop and its services live in :mod:`PhyAgentOS.agent_legacy`; this
package's ``__init__`` does not import them, so importing ``PhyAgentOS.agent``
stays cheap.
"""
