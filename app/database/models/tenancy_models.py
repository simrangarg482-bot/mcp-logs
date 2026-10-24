import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class Organization(Base):
    """One row per company that has purchased EKIP.

    `organization_id` (this table's primary key, referenced everywhere else)
    *is* the tenant id used throughout the system -- there is no separate
    "tenant" concept above organization (PROJECT_PLAN.md section 3.1).
    """

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Used in the per-organization login URL (e.g. /o/{slug}/login), per
    # PROJECT_PLAN.md section 3.3.
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # onboarding / active / suspended -- the onboarding state machine
    # referenced in PROJECT_PLAN.md section 9.2.
    status: Mapped[str] = mapped_column(Text, nullable=False, default="onboarding")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Project(Base):
    """A scoping unit within an organization (e.g. "Payments team").

    Every organization has at least one project -- `is_default` marks the
    auto-created "General" project so small customers who don't need
    fine-grained scoping still have a uniform `project_id` on every incident
    and document, rather than that column being optional
    """

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_projects_org_name"),
        Index("ix_projects_organization_id", "organization_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SSOConfiguration(Base): #Single Sign-On.
    """One row per organization, describing how its employees log in.

    All four supported providers (Entra ID, Okta, Auth0, Google Workspace)
    speak OIDC, so one row shape handles all of them -- `provider` is just a
    label; `protocol` leaves room for a future SAML-only IdP without a schema
    change.
    """

    __tablename__ = "sso_configurations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # One SSO config per organization for now -- unique enforces that.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)  # entra_id/okta/auth0/google_workspace
    protocol: Mapped[str] = mapped_column(Text, nullable=False, default="oidc")
    issuer_url: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    # A reference/identifier into the encrypted secret store (PROJECT_PLAN.md
    # section 12.5) -- never the raw client secret itself.
    client_secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExternalIdentityMapping(Base):
    """Maps one IdP's subject claim to one EKIP user, within one organization.

    This is the lookup table that resolves "Continue with Microsoft" into a
    specific EKIP account (PROJECT_PLAN.md section 3.3, step 6). The unique
    constraint on (organization_id, idp_subject) is the actual login lookup
    key -- exactly one EKIP user per IdP identity per organization.
    """

    __tablename__ = "external_identity_mappings"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "idp_subject", name="uq_external_identity_org_subject"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # The IdP's stable subject claim (e.g. Entra ID's `oid`).
    idp_subject: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConnectorConfig(Base):
    """One row per (organization, external tool) connection.

    `project_id` is nullable: a connector can be org-wide (e.g. one GitHub
    org covering every team) or scoped to a single project. `credential_ref`
    is, like `SSOConfiguration.client_secret_ref`, a reference into the
    encrypted secret store -- this table never holds a usable raw credential. `config` holds source-specific settings
    (which Slack workspace, which repos) whose shape genuinely differs per
    source -- JSONB here for the same reason `document_metadata` is EAV-style
    in the original design: fixed columns would force a migration per new
    connector.
    """

    __tablename__ = "connector_configs"
    __table_args__ = (
        Index("ix_connector_configs_org_status", "organization_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)  # slack/github/azure_devops/jira/...
    credential_ref: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="connecting")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ProjectMembership(Base):
    """Grants a user a role within one specific project.

    This is the project-level authorization tier from -- distinct from `user_roles` (organization-level role
    assignment). Reuses the existing `roles` catalog rather than inventing
    project-specific roles, so the same permission vocabulary applies at both
    the organization and project scope.
    """

    __tablename__ = "project_memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )


class OrganizationAccessRule(Base):
    """A coarse, admin-configured rule describing who may auto-join an
    organization via SSO, without an individual invitation.

    Two rule types, deliberately not three: `domain` (any verified SSO login
    whose email domain matches `value`, e.g. `"nevikenz.com"`) and `group`
    (any verified SSO login whose IdP `groups` claim contains `value`, e.g.
    `"engineering"`). A per-email allow-rule is intentionally NOT a third
    rule_type here -- that exact need is served by `Invitation` below, which
    is strictly more capable (expiry, status, who invited them) than a bare
    email match would be; having both would be two mechanisms for one job.

    `grants_role_id` is evaluated together with the rule matching, not just
    the rule's existence: a match determines *which* role the auto-provisioned
    user receives ("assign the default role according to the organization's
    policy"), so two rules (e.g. one domain rule granting `viewer`, one group
    rule granting `engineer`) can coexist for the same organization.
    `is_active` lets an admin suspend a rule (e.g. temporarily pause
    domain-based auto-join) without deleting its history.
    """

    __tablename__ = "organization_access_rules"
    __table_args__ = (
        Index("ix_org_access_rules_org_type", "organization_id", "rule_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    rule_type: Mapped[str] = mapped_column(Text, nullable=False)  # domain / group
    value: Mapped[str] = mapped_column(Text, nullable=False)
    grants_role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Invitation(Base):
    """A time-boxed, per-person invitation to join an organization.

    Status lifecycle: `pending` -> `accepted` (consumed either at the
    invitee's successful first SSO login matching `email`, or -- Phase 7.5 --
    via `POST /invitations/{id}/accept` with a matching `token_hash` for a
    password-auth organization) | `expired` (past `expires_at`, never
    accepted) | `revoked` (an admin cancelled it before acceptance or
    expiry). Unlike `OrganizationAccessRule`, this grants access to exactly
    one email, once -- `grants_role_id` mirrors that table's "which role
    does a match receive" field.

    `token_hash`: `NULL` for every invitation created before
    this column existed, and for the SSO-auto-provisioned acceptance path,
    which proves identity via the IdP's own signed `id_token` instead and
    never needs a separate token. Populated only by `create_invitation` for
    the password-acceptance flow -- a SHA-256 hash of a random token shown
    to the caller exactly once (`app.shared.security.generate_opaque_token`/
    `hash_opaque_token`), never the raw value itself (section 12.1's "never
    stored in plaintext" discipline, the same one `refresh_tokens.token_hash`
    already follows). Before this column existed, the invitation's own
    database `id` was the only "token" -- a UUID4 primary key anyone who
    learned it (a leaked log line, another org member, an admin API
    response) could use to consume the invitation with no proof of email
    ownership at all.

    `invited_by` uses RESTRICT, not this file's usual CASCADE for
    organization-scoped rows: an invitation is itself a small audit record of
    who invited whom, and should not silently vanish if the inviting admin's
    `users` row is later removed.

    The partial unique index enforces "at most one pending invitation per
    email per organization" -- a second invite to the same address is either
    a resend of the existing pending one or should wait until it's
    accepted/expired/revoked, rather than creating a second live invitation.
    """

    __tablename__ = "invitations"
    __table_args__ = (
        Index("ix_invitations_org_email", "organization_id", "email"),
        Index(
            "uq_invitations_org_email_pending",
            "organization_id",
            "email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    grants_role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    invited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
