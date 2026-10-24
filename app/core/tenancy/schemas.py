"""This module defines the Pydantic schemas for EKIP's organizations, projects, SSO 
settings, and connector configurations. Schemas can be created directly from database 
rows using `from_attributes=True`, avoiding manual field copying. Authentication 
identity mapping belongs to `core/auth`, while project memberships and role assignments 
belong to `core/users`, keeping responsibilities separate. For security, stored 
credentials are represented by encrypted-secret references rather than raw secrets; 
when a connector is registered, its submitted plaintext credential is encrypted before 
being saved. Finally, invitation and access-rule schemas define who can join an 
organization, while `grants_role` uses a readable role name such as `"engineer"` instead 
of requiring callers to know the role's database UUID.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Local vocabularies ----------------------------------------------------
# Kept local rather than promoted to shared/schemas/common.py for now -- only
# core/tenancy reads/writes these today. Promote them if/when another module
# (e.g. ingestion/, reading ConnectorStatus) needs the same vocabulary.

OrganizationStatus = Literal["onboarding", "active", "suspended"]
SSOProvider = Literal["entra_id", "okta", "auth0", "google_workspace"]
SSOProtocol = Literal["oidc", "saml"]
ConnectorSource = Literal[
    "slack",
    "teams",
    "github",
    "azure_devops",
    "jira",
    "confluence",
    "sharepoint",
    "runbooks",
    "google_drive",
    "gitlab",
    "notion",
    "servicenow",
    "pagerduty",
    "monitoring",
    "incidents",
]
"""`"monitoring"` (added alongside `agents.investigation.live.
MonitoringLiveSource`'s registration into `_LIVE_SOURCES`) has no ingestion
connector or `_CONNECTOR_REGISTRY` entry (`app.ingestion.service`) -- it is
reachable only as a live-evidence source for the Investigation Agent, not as
a document-ingestion connector. A `connector_configs` row with
`source="monitoring"` is therefore only ever consulted by
`agents.investigation.evidence`, never by `app.ingestion.workers`; registering
one will not enqueue ingestion jobs the way the ingestion-backed sources do.
"""
ConnectorStatus = Literal["connecting", "active", "error", "disconnected"]
AccessRuleType = Literal["domain", "group"]
InvitationStatus = Literal["pending", "accepted", "expired", "revoked"]


# --- Organizations -----------------------------------------------------------


class OrganizationCreate(BaseModel):
    """Request body for `create_organization`.

    `slug` is constrained to URL-safe lowercase segments since it's used
    directly in the per-organization login URL:
    `https://app.ekip.io/o/{org-slug}/login` -- validating its shape here,
    once, means every caller (onboarding UI, admin API) gets the same rule
    rather than re-implementing it.
    """

    name: str
    slug: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", min_length=1, max_length=63)


class Organization(BaseModel):
    """A company that has purchased EKIP, as returned by the read surface."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    name: str
    slug: str
    status: OrganizationStatus
    created_at: datetime
    updated_at: datetime


# --- Projects ----------------------------------------------------------------


class ProjectCreate(BaseModel):
    """Request body for `create_project`.

    `is_default` marks the auto-created "General" project every organization
    gets at onboarding time -- callers creating
    additional, explicitly-scoped projects should leave this `False`.
    """

    name: str
    is_default: bool = False


class Project(BaseModel):
    """A scoping unit within an organization (e.g. "Payments team")."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    is_default: bool
    created_at: datetime
    updated_at: datetime


# --- SSO configuration ---------------------------------------------------------


class SSOConfigurationCreate(BaseModel):
    """This schema defines the request body for setting up or replacing an organization's 
    SSO configuration. The protocol defaults to OIDC, which all four currently supported 
    providers use; `saml` is accepted for possible future support. The 
    `client_secret_ref` must already point to a secret stored in the encrypted secret 
    store. Creating and encrypting that secret is handled by `shared/security`, not this 
    schema or `core/tenancy`.
    """

    provider: SSOProvider
    protocol: SSOProtocol = "oidc"
    issuer_url: str
    client_id: str
    client_secret_ref: str


class SSOConfiguration(BaseModel):
    """An organization's SSO federation settings, as returned by the read
    surface. `core/auth` reads this to determine which IdP to redirect an
    employee to.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    provider: SSOProvider
    protocol: SSOProtocol
    issuer_url: str
    client_id: str
    client_secret_ref: str
    created_at: datetime
    updated_at: datetime


# --- Connector configuration ----------------------------------------------------


class ConnectorConfigCreate(BaseModel):
    """Request body for `register_connector`.

    `project_id` is optional: a connector can be organization-wide (e.g. one
    GitHub org covering every team) or scoped to a single project . `config` holds source-specific settings
    (which Slack workspace, which repos) whose shape genuinely differs per
    source.
    """

    source: ConnectorSource
    credential_ref: str
    project_id: uuid.UUID | None = None
    config: dict = Field(default_factory=dict)


class ConnectorConfig(BaseModel):
    """One (organization, external tool) connection, as returned by the read
    surface -- backs `list_connectors`.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID | None
    source: ConnectorSource
    credential_ref: str
    config: dict
    status: ConnectorStatus
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ConnectorEventTrigger(BaseModel):
    """Normalized event from a signature-verifying provider adapter."""

    event_id: str = Field(min_length=1, max_length=512)
    event_type: str = Field(min_length=1, max_length=128)
    occurred_at: datetime | None = None


# Matches `app.shared.schemas.common.IngestionJobStatus` exactly.
IngestionRunStatus = Literal["queued", "running", "succeeded", "failed", "dead_lettered"]


class IngestionRun(BaseModel):
    """One ingestion run (full or incremental sync) of a `ConnectorConfig`,
    as returned by the read surface (`GET /tenancy/connectors/{id}/runs`).

    Deliberately a distinct type from `app.ingestion.schemas.IngestionJob`
    (which this mirrors field-for-field) rather than that type reused
    directly: `app.api` and `app.core` are both import-linter-forbidden from
    depending on `app.ingestion` (see this repo's pyproject.toml contracts
    "api does not depend on mcp or ingestion internals" / "core does not
    depend on mcp or ingestion") -- core/tenancy reads the `ingestion_jobs`
    table directly via its own repository function instead, the same
    "read the raw table directly rather than reach through another module's
    service layer" precedent `app.ingestion.repository` itself already
    established for reading `connector_configs`.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    connector_config_id: uuid.UUID
    status: IngestionRunStatus
    failed_stage: str | None
    documents_processed: int
    pages_fetched: int = 0
    items_discovered: int = 0
    items_skipped: int = 0
    chunks_embedded: int = 0
    retry_count: int = 0
    last_error_type: str | None = None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class IngestionJobStats(BaseModel):
    """Aggregated ingestion-run metrics for one connector, over some
    organization and time window -- the `ingestion_jobs`-side
    counterpart to `app.agents.schemas.AgentExecutionStats`/`core.
    observability.schemas.McpToolStats`. Backs `GET /observability/
    ingestion`, filling the one observability gap those two dashboards
    didn't already cover.
    """

    model_config = ConfigDict(frozen=True)

    connector_config_id: uuid.UUID
    run_count: int
    succeeded_count: int
    failed_count: int
    dead_lettered_count: int = 0
    #: Mean wall-clock seconds of *successful* runs only (``None`` until one
    #: succeeds). Failed/interrupted runs are excluded -- their ``completed_at``
    #: is set by stale-job recovery, not when work actually stopped.
    avg_duration_seconds: float | None
    total_documents_processed: int
    total_pages_fetched: int = 0
    total_items_discovered: int = 0
    total_items_skipped: int = 0
    total_chunks_embedded: int = 0
    total_retries: int = 0


class IngestionQueueHealth(BaseModel):
    queued_jobs: int
    oldest_queued_age_seconds: float | None
    worker_max_concurrency: int


# --- Organization access rules (domain / group auto-join) --------------------


class AccessRuleCreate(BaseModel):
    """Request body for `create_access_rule`.

    `value` is interpreted according to `rule_type`: an email domain (e.g.
    `"nevikenz.com"`, no leading `@`) for `"domain"`, or an IdP group name/id
    (e.g. `"engineering"`) for `"group"`. `grants_role` is the platform role
    *name* a matching login should receive -- resolved to a `grants_role_id`
    by the service layer, not carried as a raw id here.
    """

    rule_type: AccessRuleType
    value: str
    grants_role: str
    is_active: bool = True


class AccessRule(BaseModel):
    """A configured domain/group auto-join rule, as returned by the read
    surface. See `database/models/tenancy_models.py::OrganizationAccessRule`
    for why there is no separate `"email"` rule type -- that need is served
    by `Invitation` instead.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    rule_type: AccessRuleType
    value: str
    grants_role_id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime


# --- Invitations ---------------------------------------------------------------


class InvitationCreate(BaseModel):
    """Request body for `create_invitation`.

    `expires_at` is optional: omitting it lets the service apply a sensible
    platform default (rather than forcing every caller to compute one), while
    still allowing an admin UI to offer a custom expiry if needed.
    """

    email: str
    grants_role: str
    expires_at: datetime | None = None


class Invitation(BaseModel):
    """A pending, accepted, expired, or revoked invitation, as returned by
    the read surface.

    `token` is populated ONLY in `create_invitation`'s own
    response -- the one moment the raw, single-use token exists in memory at
    all. `model_validate(row)` elsewhere (`list_invitations`,
    `revoke_invitation`) leaves it `None` by construction: the ORM row has
    no `token` attribute (only `token_hash`, never read back into this
    schema), so there is no path by which an already-issued token could be
    re-exposed after creation.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    status: InvitationStatus
    grants_role_id: uuid.UUID
    invited_by: uuid.UUID
    expires_at: datetime
    accepted_at: datetime | None
    created_at: datetime
    updated_at: datetime
    token: str | None = None


class InvitationAcceptRequest(BaseModel):
    """Request body for the password-organization acceptance flow
    (`POST /invitations/{id}/accept`, Phase 7.5) -- proves control of the
    invited email address via `token` (the raw value from `Invitation.
    token`'s one-time exposure at creation, not the invitation's own id),
    and provisions the new account's login credential in the same call.
    """

    token: str
    password: str
    display_name: str | None = None


# --- Provisioning decision -----------------------------------------------------


class ProvisioningDecision(BaseModel):
    """The result of evaluating whether a verified SSO login may provision a
    user in an organization -- the return type of `evaluate_provisioning`.

    Not an ORM-backed read model (there is no `provisioning_decisions` table;
    this is a computed value), so no `from_attributes`/`frozen` config is
    needed beyond the default. `reason` is a stable, machine-readable code
    (`"invitation_match"` / `"domain_match"` / `"group_match"` /
    `"no_matching_policy"`) for logging/observability -- not meant to be
    shown to the end user verbatim.
    """

    allowed: bool
    grants_role_id: uuid.UUID | None = None
    matched_invitation_id: uuid.UUID | None = None
    reason: str
