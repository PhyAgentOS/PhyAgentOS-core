"""Workspace template diagnosis and upgrade."""

from PhyAgentOS.workspace.baseline import (
    BASELINE_RELATIVE_PATH,
    PROTECTED_TEMPLATE_PATHS,
    TemplateTarget,
    baseline_path,
    is_within_workspace,
    iter_template_targets,
    read_baseline,
    record_template_baseline,
    template_set_sha256,
)
from PhyAgentOS.workspace.upgrade import (
    WorkspaceContext,
    WorkspaceUpgradeError,
    apply_workspace,
    check_workspace,
    resolve_context,
)

__all__ = [
    "BASELINE_RELATIVE_PATH",
    "PROTECTED_TEMPLATE_PATHS",
    "TemplateTarget",
    "WorkspaceContext",
    "WorkspaceUpgradeError",
    "apply_workspace",
    "baseline_path",
    "check_workspace",
    "is_within_workspace",
    "iter_template_targets",
    "read_baseline",
    "record_template_baseline",
    "resolve_context",
    "template_set_sha256",
]
