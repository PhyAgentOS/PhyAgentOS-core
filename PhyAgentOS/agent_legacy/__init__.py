"""Legacy agent runtime, kept at its own path during the core migration.

The new, decoupled runtime lives in :mod:`PhyAgentOS.agent.core`. This package
still serves existing entry points and behavior comparisons; it is not
imported by the new core.
"""
