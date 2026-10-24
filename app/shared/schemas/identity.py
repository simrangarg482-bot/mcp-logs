"""`Identity` is a shared object that carries who the caller is, which organization they
 belong to, and what permissions they have. It keeps authorization consistent across 
 REST, MCP, `core/`, and `agents/` because every module receives the same resolved 
 identity instead of handling tokens or HTTP details itself. Every identity must belong 
 to exactly one organization, while `project_permissions` provides finer-grained 
 permissions for individual projects. If no project-specific permissions are populated, 
 the system falls back to the identity’s organization-level permissions.
"""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ActorKind(str, Enum):
    """Distinguishes human users from non-human callers.

    Mirrors the human-vs-AI authorship distinction that DATABASE_DESIGN.md
    keeps unambiguous at the data layer (the tagged `actor` strings on
    incident_timeline / postmortems / audit_logs). `Identity.audit_tag`
    produces exactly those strings, so the identity that performed an action
    and the string recorded against it never drift apart.
    """

    USER = "user"
    SERVICE = "service"
    AGENT = "agent"


class Identity(BaseModel):
    """A resolved caller, scoped to exactly one organization. Immutable once
    constructed.

    `permissions` is the flattened set of org-level permission codes granted
    via the caller's roles *within `organization_id`* (resolved once by
    core/users/), so downstream `authorize()` checks are a pure set-membership
    test with no further database access. `project_permissions` holds any
    project-scoped overrides, keyed by project id -- a project
    absent from that mapping simply has no override.
    """

    # Frozen: an identity is a fact about who is calling, resolved once at the
    # boundary; nothing downstream should be able to mutate it (e.g. quietly
    # grant itself a permission mid-request).
    model_config = ConfigDict(frozen=True)

    kind: ActorKind
    # The stable principal id: a stringified user UUID, a service name, or an
    # agent name -- whatever uniquely identifies this caller of `kind`.
    subject: str
    # The organization this identity is scoped to. Required, not optional:
    # a session -- and therefore every
    # resolved Identity -- belongs to exactly one organization; there is no
    # "global" Identity to construct without one.
    organization_id: uuid.UUID
    # Populated only when kind == USER; None for services/agents that have no
    # `users` row (consistent with DATABASE_DESIGN.md's rationale for tagged
    # actors rather than strict FKs).
    user_id: uuid.UUID | None = None
    display_name: str | None = None
    roles: tuple[str, ...] = ()
    permissions: frozenset[str] = Field(default_factory=frozenset)
    # Project-scoped permission overrides (PROJECT_PLAN.md section 3.6):
    # project_id -> permission codes granted *for that project*. Populated by
    # `core.users.service.resolve_identity`; empty by default for identities
    # constructed some other way (e.g. `Identity.for_agent`) -- see the
    # module docstring.
    project_permissions: dict[uuid.UUID, frozenset[str]] = Field(default_factory=dict)

    @property
    def audit_tag(self) -> str:
        """The `actor` string recorded in incident_timeline / audit_logs / etc.

        e.g. ``user:3f2a...`` or ``agent:postmortem_agent``. Single source of
        that format, so it stays consistent everywhere it is written.
        """
        return f"{self.kind.value}:{self.subject}"

    def has_permission(
        self, permission_code: str, project_id: uuid.UUID | None = None
    ) -> bool:
        """Pure, database-free permission check against the pre-resolved
        set(s).

        With no `project_id`, checks the org-level `permissions` set. With a
        `project_id` that has an explicit override in `project_permissions`,
        checks *only* that project-scoped set -- it does not also fall back to
        `permissions`, since a project override that's intentionally more
        restrictive (e.g. a `viewer`-only project role) must actually be able
        to restrict. A `project_id` with no override falls back to the
        org-level set, per PROJECT_PLAN.md section 3.6.
        """
        if project_id is not None and project_id in self.project_permissions:
            return permission_code in self.project_permissions[project_id]
        return permission_code in self.permissions

    @classmethod
    def for_agent(cls, agent_name: str, organization_id: uuid.UUID) -> "Identity":
        """Convenience constructor for internal agent-initiated calls.

        Agents act with no interactive user (AGENT_WORKFLOWS.md), but still
        act *within* one organization's scope -- e.g. the organization that
        owns the incident being triaged -- so `organization_id` is required
        here too, keeping agent identity construction consistent with every
        other `Identity` in the system rather than a special-cased exception.
        """
        return cls(
            kind=ActorKind.AGENT, subject=agent_name, organization_id=organization_id
        )
