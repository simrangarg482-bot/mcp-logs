"""Tests for `app.core.tenancy.service` -- Milestone 10's envelope-encryption
addition to `register_connector`, plus the integration-gaps pass's
`create_organization` optional-actor/audit addition, `accept_invitation`
hardening, and `register_connector`'s new project-scoped permission check.
Not a full test suite for `core.tenancy.service` (no test infrastructure for
this module existed before the first of these additions). Monkeypatches
`repository.*` functions (capturing what they were actually called with),
the same "monkeypatch the module-level dependency" style used throughout
this test suite.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    ValidationError,
)
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import (
    ConnectorConfig,
    ConnectorConfigCreate,
    OrganizationCreate,
    SSOConfigurationCreate,
)
from app.shared.schemas import ActorKind, Identity
from app.shared.security import KmsUnavailableError, decrypt_secret, get_kms


def _admin(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"tenancy:manage"}),
    )


class _FakeConnectorConfigRow:
    def __init__(self, **kwargs: object) -> None:
        now = datetime.now(timezone.utc)
        self.id = uuid.uuid4()
        self.organization_id = kwargs["organization_id"]
        self.source = kwargs["source"]
        self.credential_ref = kwargs["credential_ref"]
        self.project_id = kwargs.get("project_id")
        self.config = kwargs.get("config") or {}
        self.status = "connecting"
        self.last_synced_at = None
        self.created_at = now
        self.updated_at = now


@pytest.mark.asyncio
async def test_register_connector_encrypts_credential_before_storing(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)
    plaintext_credential = "xoxb-11725744885042-fake-slack-bot-token"
    captured: dict[str, object] = {}

    async def fake_insert_connector_config(session, **kwargs):
        captured.update(kwargs)
        return _FakeConnectorConfigRow(**kwargs)

    async def fake_record_audit_event(session, actor, **kwargs):
        return None

    monkeypatch.setattr(
        tenancy_service.repository, "insert_connector_config", fake_insert_connector_config
    )
    monkeypatch.setattr(tenancy_service, "record_audit_event", fake_record_audit_event)

    result = await tenancy_service.register_connector(
        None,
        actor,
        organization_id,
        ConnectorConfigCreate(source="slack", credential_ref=plaintext_credential),
    )

    stored_credential_ref = captured["credential_ref"]
    assert stored_credential_ref != plaintext_credential
    assert plaintext_credential not in stored_credential_ref

    # The stored value is a real, working envelope -- round-trips back to
    # the original plaintext via the same KMS `ingestion.service` uses.
    assert await decrypt_secret(get_kms(), stored_credential_ref) == plaintext_credential

    # The response never echoes back the encrypted column value either --
    # `register_connector` redacts it the same way `core.tenancy.service.
    # _redact_client_secret` already does for `SSOConfiguration` (a Phase 3
    # security-audit fix: identical sensitivity, previously inconsistent).
    assert result.credential_ref == tenancy_service._REDACTED_CREDENTIAL
    assert result.credential_ref != stored_credential_ref


@pytest.mark.asyncio
async def test_register_connector_translates_kms_unavailable_to_service_unavailable(
    monkeypatch,
) -> None:
    """Production incident this session diagnosed: an unreachable/
    misconfigured KMS (Azure Key Vault unreachable, or no usable
    `DefaultAzureCredential` source -- the actual case found live) made
    `encrypt_secret` raise a raw, untranslated exception that became an
    unhandled 500 -- one that never passed back through `CORSMiddleware`,
    so the browser reported it as a CORS failure rather than a real error,
    for every connector source (GitHub, Slack, ...) identically, since they
    all share this one call site.

    `KmsUnavailableError` (`app.shared.security.kms`) is what
    `AzureKeyVaultKeyManagementService` now raises instead of a bare Azure
    SDK exception (see `tests/shared/security/test_azure_kms.py`); this
    pins the other half of the fix -- `register_connector` catching it and
    raising an ordinary `ServiceUnavailableError` (503), which the existing
    `EKIPError` handler renders as a normal, CORS-visible JSON error like
    any other domain error.
    """
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)

    async def fake_encrypt_secret(kms, plaintext):
        raise KmsUnavailableError("Azure Key Vault is unreachable.")

    monkeypatch.setattr(tenancy_service, "encrypt_secret", fake_encrypt_secret)

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await tenancy_service.register_connector(
            None,
            actor,
            organization_id,
            ConnectorConfigCreate(source="github", credential_ref="ghp_fake"),
        )

    assert exc_info.value.error_code == "connector_config.kms_unavailable"
    assert isinstance(exc_info.value.__cause__, KmsUnavailableError)


def test_connector_redaction_removes_worker_owned_config_state() -> None:
    row = _FakeConnectorConfigRow(
        organization_id=uuid.uuid4(),
        source="sharepoint",
        credential_ref="encrypted-ref",
        config={
            "site_ids": ["public-site-id"],
            "_resume_token": "sensitive-delta-link",
            "_ingestion_checkpoint": {"cursor": "sensitive-page-cursor"},
        },
    )

    result = tenancy_service._redact_credential(ConnectorConfig.model_validate(row))

    assert result.credential_ref == tenancy_service._REDACTED_CREDENTIAL
    assert result.config == {"site_ids": ["public-site-id"]}


@pytest.mark.asyncio
async def test_update_connector_sync_status_threads_config_patch_through(monkeypatch) -> None:
    """`config_patch` (ingestion's persisted cross-sync resume token, see
    `app.ingestion.service._execute_ingestion_job`) must reach `repository.
    update_connector_config_sync_status` unchanged -- the actual JSONB
    shallow-merge is a one-line operation inside that repository function
    itself (no test infra for direct repository calls exists in this test
    file; every test here monkeypatches at the `repository.*` boundary).
    """
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)
    connector_config_id = uuid.uuid4()
    existing_row = _FakeConnectorConfigRow(
        organization_id=organization_id, source="sharepoint", credential_ref="encrypted-ref"
    )
    existing_row.id = connector_config_id
    captured: dict[str, object] = {}

    async def fake_get_connector_config_by_id(session, config_id):
        assert config_id == connector_config_id
        return existing_row

    async def fake_update_connector_config_sync_status(session, config_id, **kwargs):
        captured.update(kwargs)
        return existing_row

    async def fake_record_audit_event(session, actor, **kwargs):
        return None

    monkeypatch.setattr(
        tenancy_service.repository,
        "get_connector_config_by_id",
        fake_get_connector_config_by_id,
    )
    monkeypatch.setattr(
        tenancy_service.repository,
        "update_connector_config_sync_status",
        fake_update_connector_config_sync_status,
    )
    monkeypatch.setattr(tenancy_service, "record_audit_event", fake_record_audit_event)

    await tenancy_service.update_connector_sync_status(
        None,
        actor,
        organization_id,
        connector_config_id,
        status="active",
        config_patch={"_resume_token": '{"site-1": "https://example.com/delta"}'},
    )

    assert captured["config_patch"] == {"_resume_token": '{"site-1": "https://example.com/delta"}'}


@pytest.mark.asyncio
async def test_disconnect_connector_sets_status_disconnected_not_a_hard_delete(monkeypatch) -> None:
    """The "Delete connector" feature: `ingestion_jobs.connector_config_id`
    is `ON DELETE RESTRICT`, so a real row deletion isn't available for any
    connector that's ever synced. This must go through `repository.
    update_connector_config_sync_status` (the same function ingestion's own
    worker uses to report outcomes) with `status="disconnected"`, reusing
    `get_connector`'s existing ownership + `tenancy:manage` check rather
    than duplicating it.
    """
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)
    connector_config_id = uuid.uuid4()
    existing_row = _FakeConnectorConfigRow(
        organization_id=organization_id, source="github", credential_ref="encrypted-ref"
    )
    existing_row.id = connector_config_id
    captured: dict[str, object] = {}

    async def fake_get_connector_config_by_id(session, config_id):
        assert config_id == connector_config_id
        return existing_row

    async def fake_update_connector_config_sync_status(session, config_id, **kwargs):
        captured["config_id"] = config_id
        captured.update(kwargs)
        existing_row.status = kwargs["status"]
        return existing_row

    async def fake_record_audit_event(session, event_actor, **kwargs):
        captured["audit_actor"] = event_actor
        captured["audit_kwargs"] = kwargs

    monkeypatch.setattr(
        tenancy_service.repository, "get_connector_config_by_id", fake_get_connector_config_by_id
    )
    monkeypatch.setattr(
        tenancy_service.repository,
        "update_connector_config_sync_status",
        fake_update_connector_config_sync_status,
    )
    monkeypatch.setattr(tenancy_service, "record_audit_event", fake_record_audit_event)

    result = await tenancy_service.disconnect_connector(
        None, actor, organization_id, connector_config_id
    )

    assert result.status == "disconnected"
    assert captured["config_id"] == connector_config_id
    assert captured["status"] == "disconnected"
    assert captured["audit_kwargs"]["action"] == "connector_config.disconnect"
    assert captured["audit_actor"] is actor


@pytest.mark.asyncio
async def test_disconnect_connector_denies_a_connector_from_a_different_organization(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)
    other_org_row = _FakeConnectorConfigRow(
        organization_id=uuid.uuid4(), source="github", credential_ref="encrypted-ref"
    )

    async def fake_get_connector_config_by_id(session, config_id):
        return other_org_row

    monkeypatch.setattr(
        tenancy_service.repository, "get_connector_config_by_id", fake_get_connector_config_by_id
    )

    with pytest.raises(NotFoundError):
        await tenancy_service.disconnect_connector(None, actor, organization_id, other_org_row.id)


class _FakeOrganizationRow:
    def __init__(self, organization_id: uuid.UUID) -> None:
        self.id = organization_id
        self.slug = "acme"


class _FakeSSOConfigurationRow:
    def __init__(self, organization_id: uuid.UUID) -> None:
        now = datetime.now(timezone.utc)
        self.id = uuid.uuid4()
        self.organization_id = organization_id
        self.provider = "okta"
        self.protocol = "oidc"
        self.issuer_url = "https://acme.okta.com"
        self.client_id = "client-123"
        self.client_secret_ref = "encrypted-ref"
        self.created_at = now
        self.updated_at = now


@pytest.mark.asyncio
async def test_get_organization_sso_config_sets_tenant_context_before_reading_sso_row(
    monkeypatch,
) -> None:
    """Milestone 10 RLS note: `organizations` isn't RLS-protected (no bypass
    needed for the slug lookup), but `sso_configurations` is -- this test
    asserts `set_tenant_context` runs after the org is resolved by slug and
    before the `sso_configurations` read.
    """
    organization_id = uuid.uuid4()
    org_row = _FakeOrganizationRow(organization_id)
    sso_row = _FakeSSOConfigurationRow(organization_id)
    call_order: list[str] = []

    async def fake_get_organization_by_slug(session, slug):
        call_order.append("get_organization_by_slug")
        assert slug == "acme"
        return org_row

    async def fake_set_tenant_context(session, org_id) -> None:
        call_order.append("set_tenant_context")
        assert org_id == organization_id

    async def fake_get_sso_configuration_by_organization_id(session, org_id):
        call_order.append("get_sso_configuration_by_organization_id")
        assert org_id == organization_id
        return sso_row

    monkeypatch.setattr(
        tenancy_service.repository, "get_organization_by_slug", fake_get_organization_by_slug
    )
    monkeypatch.setattr(tenancy_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(
        tenancy_service.repository,
        "get_sso_configuration_by_organization_id",
        fake_get_sso_configuration_by_organization_id,
    )

    result = await tenancy_service.get_organization_sso_config(None, "acme")

    assert result.organization_id == organization_id
    assert call_order == [
        "get_organization_by_slug",
        "set_tenant_context",
        "get_sso_configuration_by_organization_id",
    ]


@pytest.mark.asyncio
async def test_evaluate_provisioning_sets_tenant_context_before_any_query(monkeypatch) -> None:
    """`evaluate_provisioning` already receives `organization_id` as a
    parameter (unlike `get_organization_sso_config`, which has to discover
    it) -- Milestone 10 note: `set_tenant_context` must run before its first
    RLS-protected query (`invitations`), not after.
    """
    organization_id = uuid.uuid4()
    call_order: list[str] = []

    async def fake_set_tenant_context(session, org_id) -> None:
        call_order.append("set_tenant_context")
        assert org_id == organization_id

    async def fake_get_pending_invitation(session, org_id, email):
        call_order.append("get_pending_invitation")
        return None

    async def fake_get_active_rules_by_type(session, org_id, rule_type):
        call_order.append(f"get_active_rules_by_type:{rule_type}")
        return []

    monkeypatch.setattr(tenancy_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(
        tenancy_service.repository, "get_pending_invitation", fake_get_pending_invitation
    )
    monkeypatch.setattr(
        tenancy_service.repository, "get_active_rules_by_type", fake_get_active_rules_by_type
    )

    decision = await tenancy_service.evaluate_provisioning(
        None, organization_id=organization_id, email="new.hire@acme.com"
    )

    assert decision.allowed is False
    assert call_order[0] == "set_tenant_context"
    assert "get_pending_invitation" in call_order


# --- create_organization (optional actor + audit) -----------------------------


class _FakeOrgRow:
    def __init__(self, *, name: str, slug: str) -> None:
        now = datetime.now(timezone.utc)
        self.id = uuid.uuid4()
        self.name = name
        self.slug = slug
        self.status = "onboarding"
        self.created_at = now
        self.updated_at = now


@pytest.mark.asyncio
async def test_create_organization_without_actor_records_no_audit_event(monkeypatch) -> None:
    """Existing callers with no `Identity` available at all
    (`scripts/seed_test_organization.py`, `scripts/test_milestone6.py`) must
    keep working unchanged: omitting `actor` must not call
    `record_audit_event` at all (there is nothing to attribute it to).
    """
    audit_calls: list[dict[str, object]] = []

    async def fake_get_organization_by_slug(session, slug):
        return None

    async def fake_insert_organization(session, *, name, slug):
        return _FakeOrgRow(name=name, slug=slug)

    async def fake_insert_project(session, **kwargs):
        return None

    async def fake_set_tenant_context(session, organization_id):
        return None

    async def fake_record_audit_event(session, actor, **kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(
        tenancy_service.repository, "get_organization_by_slug", fake_get_organization_by_slug
    )
    monkeypatch.setattr(tenancy_service.repository, "insert_organization", fake_insert_organization)
    monkeypatch.setattr(tenancy_service.repository, "insert_project", fake_insert_project)
    monkeypatch.setattr(tenancy_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(tenancy_service, "record_audit_event", fake_record_audit_event)

    result = await tenancy_service.create_organization(
        None, OrganizationCreate(name="Acme", slug="acme")
    )

    assert result.slug == "acme"
    assert audit_calls == []


@pytest.mark.asyncio
async def test_create_organization_with_actor_records_audit_event(monkeypatch) -> None:
    """A caller that already has an `Identity` (the REST `POST /organizations`
    endpoint) gets a real audit event, attributed to that actor.
    """
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)
    audit_calls: list[dict[str, object]] = []

    async def fake_get_organization_by_slug(session, slug):
        return None

    async def fake_insert_organization(session, *, name, slug):
        return _FakeOrgRow(name=name, slug=slug)

    async def fake_insert_project(session, **kwargs):
        return None

    async def fake_set_tenant_context(session, organization_id):
        return None

    async def fake_record_audit_event(session, event_actor, **kwargs):
        assert event_actor is actor
        audit_calls.append(kwargs)

    monkeypatch.setattr(
        tenancy_service.repository, "get_organization_by_slug", fake_get_organization_by_slug
    )
    monkeypatch.setattr(tenancy_service.repository, "insert_organization", fake_insert_organization)
    monkeypatch.setattr(tenancy_service.repository, "insert_project", fake_insert_project)
    monkeypatch.setattr(tenancy_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(tenancy_service, "record_audit_event", fake_record_audit_event)

    result = await tenancy_service.create_organization(
        None, OrganizationCreate(name="Acme", slug="acme"), actor=actor
    )

    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "organization.create"
    assert audit_calls[0]["resource_id"] == result.id


@pytest.mark.asyncio
async def test_create_organization_with_actor_requires_tenancy_manage_permission() -> None:
    """The REST `POST /organizations` endpoint passes an already-
    authenticated `actor` through to `create_organization` -- this confirms
    that path is gated by `tenancy:manage` like every other mutating
    operation in this module. Previously unenforced: any authenticated user,
    in any organization, with any or no permissions, could call this
    endpoint to create an arbitrary new organization.

    Deliberately does not monkeypatch any `repository` function: the
    permission check must reject before any database call is attempted.
    """
    organization_id = uuid.uuid4()
    actor = _member_no_permissions(organization_id)

    with pytest.raises(PermissionDeniedError):
        await tenancy_service.create_organization(
            None, OrganizationCreate(name="Acme", slug="acme"), actor=actor
        )


@pytest.mark.asyncio
async def test_create_organization_without_actor_bypasses_permission_check(monkeypatch) -> None:
    """Self-serve signup (`core.auth.service.signup`) and the dev bootstrap
    scripts call `create_organization` with no `Identity` at all, since none
    exists yet at signup time -- confirms this path is untouched by the new
    `tenancy:manage` gate, which only applies when `actor` is provided.
    """

    async def fake_get_organization_by_slug(session, slug):
        return None

    async def fake_insert_organization(session, *, name, slug):
        return _FakeOrgRow(name=name, slug=slug)

    async def fake_insert_project(session, **kwargs):
        return None

    async def fake_set_tenant_context(session, organization_id):
        return None

    monkeypatch.setattr(
        tenancy_service.repository, "get_organization_by_slug", fake_get_organization_by_slug
    )
    monkeypatch.setattr(tenancy_service.repository, "insert_organization", fake_insert_organization)
    monkeypatch.setattr(tenancy_service.repository, "insert_project", fake_insert_project)
    monkeypatch.setattr(tenancy_service, "set_tenant_context", fake_set_tenant_context)

    result = await tenancy_service.create_organization(
        None, OrganizationCreate(name="Acme", slug="acme")
    )

    assert result.slug == "acme"


@pytest.mark.asyncio
async def test_create_organization_sets_tenant_context_before_default_project(
    monkeypatch,
) -> None:
    """Regression test for a real bug: Milestone 10 RLS (`c7d4e8f19a2b`)
    enforces `app.current_organization_id` on every tenant-owned table,
    `projects` included. The default project is created in the same call as
    the organization, before any `actor`/Identity exists to have already set
    the tenant context (see this function's own docstring) -- so
    `create_organization` must set it itself, using the just-created
    organization's own id, before inserting the default project. Without
    this, the project INSERT fails outright under real RLS with
    `InsufficientPrivilegeError: new row violates row-level security policy
    for table "projects"` -- invisible in this monkeypatched unit test
    unless it specifically asserts call order and argument, which is what
    this test does; only a live Postgres actually enforces the policy
    (see `scripts/verify_rls_isolation.py`).
    """
    calls: list[str] = []
    captured_tenant_context_org_id: uuid.UUID | None = None
    captured_insert_project_org_id: uuid.UUID | None = None

    async def fake_get_organization_by_slug(session, slug):
        return None

    async def fake_insert_organization(session, *, name, slug):
        return _FakeOrgRow(name=name, slug=slug)

    async def fake_set_tenant_context(session, organization_id):
        nonlocal captured_tenant_context_org_id
        captured_tenant_context_org_id = organization_id
        calls.append("set_tenant_context")

    async def fake_insert_project(session, *, organization_id, **kwargs):
        nonlocal captured_insert_project_org_id
        captured_insert_project_org_id = organization_id
        calls.append("insert_project")
        return None

    monkeypatch.setattr(
        tenancy_service.repository, "get_organization_by_slug", fake_get_organization_by_slug
    )
    monkeypatch.setattr(tenancy_service.repository, "insert_organization", fake_insert_organization)
    monkeypatch.setattr(tenancy_service.repository, "insert_project", fake_insert_project)
    monkeypatch.setattr(tenancy_service, "set_tenant_context", fake_set_tenant_context)

    result = await tenancy_service.create_organization(
        None, OrganizationCreate(name="Acme", slug="acme")
    )

    assert calls == ["set_tenant_context", "insert_project"], (
        "tenant context must be set before the default project is inserted, "
        "not after or not at all"
    )
    assert captured_tenant_context_org_id == result.id
    assert captured_insert_project_org_id == result.id


# --- accept_invitation hardening -----------------------------------------------


class _FakeInvitationRow:
    def __init__(self, *, status: str, expires_at: datetime) -> None:
        self.id = uuid.uuid4()
        self.status = status
        self.expires_at = expires_at


@pytest.mark.asyncio
async def test_accept_invitation_raises_not_found_for_unknown_id(monkeypatch) -> None:
    async def fake_get_invitation_by_id(session, invitation_id):
        return None

    monkeypatch.setattr(
        tenancy_service.repository, "get_invitation_by_id", fake_get_invitation_by_id
    )

    with pytest.raises(NotFoundError):
        await tenancy_service.accept_invitation(None, uuid.uuid4())


@pytest.mark.asyncio
async def test_accept_invitation_raises_conflict_when_not_pending(monkeypatch) -> None:
    row = _FakeInvitationRow(
        status="accepted", expires_at=datetime.now(timezone.utc) + timedelta(days=1)
    )

    async def fake_get_invitation_by_id(session, invitation_id):
        return row

    monkeypatch.setattr(
        tenancy_service.repository, "get_invitation_by_id", fake_get_invitation_by_id
    )

    with pytest.raises(ConflictError):
        await tenancy_service.accept_invitation(None, row.id)


@pytest.mark.asyncio
async def test_accept_invitation_raises_conflict_when_expired(monkeypatch) -> None:
    row = _FakeInvitationRow(
        status="pending", expires_at=datetime.now(timezone.utc) - timedelta(days=1)
    )

    async def fake_get_invitation_by_id(session, invitation_id):
        return row

    monkeypatch.setattr(
        tenancy_service.repository, "get_invitation_by_id", fake_get_invitation_by_id
    )

    with pytest.raises(ConflictError):
        await tenancy_service.accept_invitation(None, row.id)


@pytest.mark.asyncio
async def test_accept_invitation_succeeds_for_pending_unexpired_invitation(monkeypatch) -> None:
    row = _FakeInvitationRow(
        status="pending", expires_at=datetime.now(timezone.utc) + timedelta(days=1)
    )
    updates: list[dict[str, object]] = []

    async def fake_get_invitation_by_id(session, invitation_id):
        return row

    async def fake_update_invitation_status(session, invitation_id, **kwargs):
        updates.append({"invitation_id": invitation_id, **kwargs})

    monkeypatch.setattr(
        tenancy_service.repository, "get_invitation_by_id", fake_get_invitation_by_id
    )
    monkeypatch.setattr(
        tenancy_service.repository, "update_invitation_status", fake_update_invitation_status
    )

    await tenancy_service.accept_invitation(None, row.id)

    assert updates == [{"invitation_id": row.id, "status": "accepted", "accepted_at": updates[0]["accepted_at"]}]


# --- register_connector project-scoped permission -----------------------------


@pytest.mark.asyncio
async def test_register_connector_with_project_id_checks_project_scoped_permission(
    monkeypatch,
) -> None:
    """A caller granted `tenancy:manage` only on a *different* project must
    be denied when registering a connector scoped to this one -- confirms
    `register_connector` now checks the permission against `data.project_id`,
    not just the organization as a whole.
    """
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        project_permissions={other_project_id: frozenset({"tenancy:manage"})},
    )

    class _FakeProjectRow:
        def __init__(self) -> None:
            self.id = project_id
            self.organization_id = organization_id

    async def fake_get_project_by_id(session, project_id_arg):
        return _FakeProjectRow()

    monkeypatch.setattr(tenancy_service.repository, "get_project_by_id", fake_get_project_by_id)

    with pytest.raises(PermissionDeniedError):
        await tenancy_service.register_connector(
            None,
            actor,
            organization_id,
            ConnectorConfigCreate(source="slack", credential_ref="xoxb-token", project_id=project_id),
        )


@pytest.mark.asyncio
async def test_register_connector_with_project_id_succeeds_with_project_scoped_permission(
    monkeypatch,
) -> None:
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        project_permissions={project_id: frozenset({"tenancy:manage"})},
    )

    class _FakeProjectRow:
        def __init__(self) -> None:
            self.id = project_id
            self.organization_id = organization_id

    async def fake_get_project_by_id(session, project_id_arg):
        return _FakeProjectRow()

    async def fake_insert_connector_config(session, **kwargs):
        return _FakeConnectorConfigRow(**kwargs)

    async def fake_record_audit_event(session, actor_arg, **kwargs):
        return None

    monkeypatch.setattr(tenancy_service.repository, "get_project_by_id", fake_get_project_by_id)
    monkeypatch.setattr(
        tenancy_service.repository, "insert_connector_config", fake_insert_connector_config
    )
    monkeypatch.setattr(tenancy_service, "record_audit_event", fake_record_audit_event)

    result = await tenancy_service.register_connector(
        None,
        actor,
        organization_id,
        ConnectorConfigCreate(source="slack", credential_ref="xoxb-token", project_id=project_id),
    )

    assert result.project_id == project_id


@pytest.mark.asyncio
async def test_register_connector_without_project_id_still_requires_org_level_permission(
    monkeypatch,
) -> None:
    """An org-wide connector (`project_id=None`) has no narrower scope to
    check against, so it must fall back to the plain org-level
    `tenancy:manage` check -- an actor with only a project-scoped grant (and
    none at the org level) must still be denied.
    """
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    actor = Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        project_permissions={project_id: frozenset({"tenancy:manage"})},
    )

    with pytest.raises(PermissionDeniedError):
        await tenancy_service.register_connector(
            None,
            actor,
            organization_id,
            ConnectorConfigCreate(source="slack", credential_ref="xoxb-token"),
        )


def _member_no_permissions(organization_id: uuid.UUID) -> Identity:
    """An authenticated org member holding no permissions at all -- used to
    verify the three read-endpoint RBAC gaps found by audit (list_connectors/
    list_access_rules/list_invitations were reachable by any org member even
    though their sibling writes already required `tenancy:manage`).
    """
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_list_connectors_requires_tenancy_manage_permission() -> None:
    organization_id = uuid.uuid4()
    actor = _member_no_permissions(organization_id)

    with pytest.raises(PermissionDeniedError):
        await tenancy_service.list_connectors(None, actor, organization_id)


@pytest.mark.asyncio
async def test_list_connectors_succeeds_with_tenancy_manage_permission(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)

    async def fake_list_connector_configs(session, org_id):
        assert org_id == organization_id
        return []

    monkeypatch.setattr(
        tenancy_service.repository, "list_connector_configs", fake_list_connector_configs
    )

    result = await tenancy_service.list_connectors(None, actor, organization_id)
    assert result == []


@pytest.mark.asyncio
async def test_list_access_rules_requires_tenancy_manage_permission() -> None:
    organization_id = uuid.uuid4()
    actor = _member_no_permissions(organization_id)

    with pytest.raises(PermissionDeniedError):
        await tenancy_service.list_access_rules(None, actor, organization_id)


@pytest.mark.asyncio
async def test_list_access_rules_succeeds_with_tenancy_manage_permission(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)

    async def fake_list_access_rules(session, org_id):
        assert org_id == organization_id
        return []

    monkeypatch.setattr(tenancy_service.repository, "list_access_rules", fake_list_access_rules)

    result = await tenancy_service.list_access_rules(None, actor, organization_id)
    assert result == []


@pytest.mark.asyncio
async def test_list_invitations_requires_tenancy_manage_permission() -> None:
    organization_id = uuid.uuid4()
    actor = _member_no_permissions(organization_id)

    with pytest.raises(PermissionDeniedError):
        await tenancy_service.list_invitations(None, actor, organization_id)


class _FakeInsertedSSOConfigurationRow:
    def __init__(self, **kwargs: object) -> None:
        now = datetime.now(timezone.utc)
        self.id = uuid.uuid4()
        self.organization_id = kwargs["organization_id"]
        self.provider = kwargs["provider"]
        self.protocol = kwargs["protocol"]
        self.issuer_url = kwargs["issuer_url"]
        self.client_id = kwargs["client_id"]
        self.client_secret_ref = kwargs["client_secret_ref"]
        self.created_at = now
        self.updated_at = now


@pytest.mark.asyncio
async def test_get_sso_config_requires_tenancy_manage_permission() -> None:
    organization_id = uuid.uuid4()
    actor = _member_no_permissions(organization_id)

    with pytest.raises(PermissionDeniedError):
        await tenancy_service.get_sso_config(None, actor, organization_id)


@pytest.mark.asyncio
async def test_get_sso_config_rejects_a_different_organization() -> None:
    actor = _admin(uuid.uuid4())

    with pytest.raises(PermissionDeniedError):
        await tenancy_service.get_sso_config(None, actor, uuid.uuid4())


@pytest.mark.asyncio
async def test_get_sso_config_raises_not_found_when_unconfigured(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)

    async def fake_get_sso_configuration_by_organization_id(session, org_id):
        return None

    monkeypatch.setattr(
        tenancy_service.repository,
        "get_sso_configuration_by_organization_id",
        fake_get_sso_configuration_by_organization_id,
    )

    with pytest.raises(NotFoundError):
        await tenancy_service.get_sso_config(None, actor, organization_id)


@pytest.mark.asyncio
async def test_get_sso_config_never_returns_the_real_client_secret_ref_column_value(
    monkeypatch,
) -> None:
    """`sso_configurations.client_secret_ref` holds envelope-encrypted
    ciphertext (see `configure_sso`), not a human-readable reference -- this
    must never reach the wire, redacted or not decrypted.
    """
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)
    sso_row = _FakeSSOConfigurationRow(organization_id)

    async def fake_get_sso_configuration_by_organization_id(session, org_id):
        assert org_id == organization_id
        return sso_row

    monkeypatch.setattr(
        tenancy_service.repository,
        "get_sso_configuration_by_organization_id",
        fake_get_sso_configuration_by_organization_id,
    )

    result = await tenancy_service.get_sso_config(None, actor, organization_id)

    assert result.organization_id == organization_id
    assert result.provider == "okta"
    assert result.client_secret_ref == tenancy_service._REDACTED_CLIENT_SECRET
    assert result.client_secret_ref != sso_row.client_secret_ref


@pytest.mark.asyncio
async def test_configure_sso_encrypts_client_secret_before_storing(monkeypatch) -> None:
    """Regression test for the SSO client-secret KMS bypass: `configure_sso`
    must envelope-encrypt `client_secret_ref` before persisting it, matching
    `register_connector`'s identical treatment of connector credentials --
    previously this was stored unencrypted, and `core.auth.service.
    _resolve_client_secret` read it back as if it already were plaintext.
    """
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)
    plaintext_secret = "super-secret-oidc-client-secret"
    captured: dict[str, object] = {}

    async def fake_get_sso_configuration_by_organization_id(session, org_id):
        return None

    async def fake_insert_sso_configuration(session, **kwargs):
        captured.update(kwargs)
        return _FakeInsertedSSOConfigurationRow(**kwargs)

    async def fake_record_audit_event(session, actor, **kwargs):
        return None

    monkeypatch.setattr(
        tenancy_service.repository,
        "get_sso_configuration_by_organization_id",
        fake_get_sso_configuration_by_organization_id,
    )
    monkeypatch.setattr(
        tenancy_service.repository, "insert_sso_configuration", fake_insert_sso_configuration
    )
    monkeypatch.setattr(tenancy_service, "record_audit_event", fake_record_audit_event)

    result = await tenancy_service.configure_sso(
        None,
        actor,
        organization_id,
        SSOConfigurationCreate(
            provider="okta",
            issuer_url="https://example.okta.com",
            client_id="client-123",
            client_secret_ref=plaintext_secret,
        ),
    )

    stored_client_secret_ref = captured["client_secret_ref"]
    assert stored_client_secret_ref != plaintext_secret
    assert plaintext_secret not in stored_client_secret_ref
    assert await decrypt_secret(get_kms(), stored_client_secret_ref) == plaintext_secret
    # The response never echoes back the encrypted column value either --
    # `configure_sso` redacts it the same way `get_sso_config` does, so
    # nothing that ever touched a real (or fake) secret crosses the wire.
    assert result.client_secret_ref == tenancy_service._REDACTED_CLIENT_SECRET
    assert result.client_secret_ref != stored_client_secret_ref


@pytest.mark.asyncio
async def test_configure_sso_translates_kms_unavailable_to_service_unavailable(monkeypatch) -> None:
    """Same production incident and same fix as
    `test_register_connector_translates_kms_unavailable_to_service_unavailable`
    -- `configure_sso` shares the exact same `encrypt_secret(get_kms(), ...)`
    call and was reproduced live failing identically (verified directly
    against production during this session's investigation: both endpoints
    returned the same unhandled 500 with no CORS headers).
    """
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)

    async def fake_get_sso_configuration_by_organization_id(session, org_id):
        return None

    async def fake_encrypt_secret(kms, plaintext):
        raise KmsUnavailableError("Azure Key Vault is unreachable.")

    monkeypatch.setattr(
        tenancy_service.repository,
        "get_sso_configuration_by_organization_id",
        fake_get_sso_configuration_by_organization_id,
    )
    monkeypatch.setattr(tenancy_service, "encrypt_secret", fake_encrypt_secret)

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await tenancy_service.configure_sso(
            None,
            actor,
            organization_id,
            SSOConfigurationCreate(
                provider="okta",
                issuer_url="https://example.okta.com",
                client_id="client-123",
                client_secret_ref="super-secret",
            ),
        )

    assert exc_info.value.error_code == "sso_configuration.kms_unavailable"
    assert isinstance(exc_info.value.__cause__, KmsUnavailableError)


@pytest.mark.asyncio
async def test_list_invitations_succeeds_with_tenancy_manage_permission(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)

    async def fake_list_invitations(session, org_id):
        assert org_id == organization_id
        return []

    monkeypatch.setattr(tenancy_service.repository, "list_invitations", fake_list_invitations)

    result = await tenancy_service.list_invitations(None, actor, organization_id)
    assert result == []


class _FakeIngestionJobRow:
    def __init__(self, **kwargs: object) -> None:
        now = datetime.now(timezone.utc)
        self.id = uuid.uuid4()
        self.organization_id = kwargs["organization_id"]
        self.connector_config_id = kwargs["connector_config_id"]
        self.status = kwargs.get("status", "succeeded")
        self.failed_stage = kwargs.get("failed_stage")
        self.documents_processed = kwargs.get("documents_processed", 0)
        self.started_at = now
        self.completed_at = now
        self.created_at = now


@pytest.mark.asyncio
async def test_list_ingestion_runs_requires_tenancy_manage_permission(monkeypatch) -> None:
    """Regression test for the Phase 2D `GET /tenancy/connectors/{id}/runs`
    addition -- no new permission was introduced, so this must be denied by
    the exact same `tenancy:manage` gate `get_connector` already applies.
    """
    organization_id = uuid.uuid4()
    connector_config_id = uuid.uuid4()
    actor = _member_no_permissions(organization_id)
    connector_row = _FakeConnectorConfigRow(
        organization_id=organization_id, source="github", credential_ref="encrypted-ref"
    )
    connector_row.id = connector_config_id

    async def fake_get_connector_config_by_id(session, config_id):
        return connector_row

    monkeypatch.setattr(
        tenancy_service.repository, "get_connector_config_by_id", fake_get_connector_config_by_id
    )

    with pytest.raises(PermissionDeniedError):
        await tenancy_service.list_ingestion_runs(None, actor, organization_id, connector_config_id)


@pytest.mark.asyncio
async def test_list_ingestion_runs_denies_connector_from_a_different_organization(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    connector_config_id = uuid.uuid4()
    actor = _admin(organization_id)
    connector_row = _FakeConnectorConfigRow(
        organization_id=uuid.uuid4(), source="github", credential_ref="encrypted-ref"
    )
    connector_row.id = connector_config_id

    async def fake_get_connector_config_by_id(session, config_id):
        return connector_row

    monkeypatch.setattr(
        tenancy_service.repository, "get_connector_config_by_id", fake_get_connector_config_by_id
    )

    with pytest.raises(NotFoundError):
        await tenancy_service.list_ingestion_runs(None, actor, organization_id, connector_config_id)


@pytest.mark.asyncio
async def test_list_ingestion_runs_succeeds_and_maps_rows(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    connector_config_id = uuid.uuid4()
    actor = _admin(organization_id)
    connector_row = _FakeConnectorConfigRow(
        organization_id=organization_id, source="github", credential_ref="encrypted-ref"
    )
    connector_row.id = connector_config_id
    job_row = _FakeIngestionJobRow(
        organization_id=organization_id, connector_config_id=connector_config_id, status="failed", failed_stage="fetch"
    )
    captured: dict[str, object] = {}

    async def fake_get_connector_config_by_id(session, config_id):
        return connector_row

    async def fake_list_ingestion_runs(session, org_id, conn_id, *, limit, offset):
        captured["organization_id"] = org_id
        captured["connector_config_id"] = conn_id
        captured["limit"] = limit
        captured["offset"] = offset
        return [job_row]

    monkeypatch.setattr(
        tenancy_service.repository, "get_connector_config_by_id", fake_get_connector_config_by_id
    )
    monkeypatch.setattr(tenancy_service.repository, "list_ingestion_runs", fake_list_ingestion_runs)

    result = await tenancy_service.list_ingestion_runs(
        None, actor, organization_id, connector_config_id, limit=10, offset=0
    )

    assert len(result) == 1
    assert result[0].status == "failed"
    assert result[0].failed_stage == "fetch"
    assert captured["organization_id"] == organization_id
    assert captured["connector_config_id"] == connector_config_id
    assert captured["limit"] == 10


def _observability_reader(organization_id: uuid.UUID) -> Identity:
    return Identity(
        kind=ActorKind.USER,
        subject=str(uuid.uuid4()),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        permissions=frozenset({"observability:read"}),
    )


class _FakeIngestionJobStatsRow:
    def __init__(self, **kwargs: object) -> None:
        self.connector_config_id = kwargs["connector_config_id"]
        self.run_count = kwargs.get("run_count", 0)
        self.succeeded_count = kwargs.get("succeeded_count", 0)
        self.failed_count = kwargs.get("failed_count", 0)
        self.avg_duration_seconds = kwargs.get("avg_duration_seconds")
        self.total_documents_processed = kwargs.get("total_documents_processed", 0)


@pytest.mark.asyncio
async def test_get_ingestion_job_stats_requires_observability_read_permission() -> None:
    """Phase 5.6: this is a new aggregate dashboard, not a pre-existing
    ungated read -- unlike `list_ingestion_runs`'s history, deliberately
    gated by `observability:read` from the start, matching `agents.service.
    get_agent_execution_stats`/`core.observability.service.
    get_mcp_dashboard`'s existing convention for this exact class of data.
    """
    organization_id = uuid.uuid4()
    actor = _member_no_permissions(organization_id)

    with pytest.raises(PermissionDeniedError):
        await tenancy_service.get_ingestion_job_stats(None, actor)


@pytest.mark.asyncio
async def test_tenancy_manage_alone_does_not_grant_ingestion_stats_access() -> None:
    """`tenancy:manage` gates connector configuration/history
    (`list_ingestion_runs`); it must NOT also satisfy this dashboard's
    distinct `observability:read` requirement -- these are two different
    questions about the same table, per `_OBSERVABILITY_READ_PERMISSION`'s
    own comment.
    """
    organization_id = uuid.uuid4()
    actor = _admin(organization_id)  # holds only "tenancy:manage"

    with pytest.raises(PermissionDeniedError):
        await tenancy_service.get_ingestion_job_stats(None, actor)


@pytest.mark.asyncio
async def test_get_ingestion_job_stats_maps_aggregate_rows(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    actor = _observability_reader(organization_id)
    connector_config_id = uuid.uuid4()
    row = _FakeIngestionJobStatsRow(
        connector_config_id=connector_config_id,
        run_count=12,
        succeeded_count=10,
        failed_count=2,
        avg_duration_seconds=45.5,
        total_documents_processed=340,
    )
    captured: dict[str, object] = {}

    async def fake_get_ingestion_job_stats(session, *, organization_id, since=None):
        captured["organization_id"] = organization_id
        captured["since"] = since
        return [row]

    monkeypatch.setattr(
        tenancy_service.repository, "get_ingestion_job_stats", fake_get_ingestion_job_stats
    )

    result = await tenancy_service.get_ingestion_job_stats(None, actor)

    assert len(result) == 1
    assert result[0].connector_config_id == connector_config_id
    assert result[0].run_count == 12
    assert result[0].succeeded_count == 10
    assert result[0].failed_count == 2
    assert result[0].avg_duration_seconds == 45.5
    assert result[0].total_documents_processed == 340
    assert captured["organization_id"] == organization_id


@pytest.mark.asyncio
async def test_get_ingestion_job_stats_handles_null_aggregates(monkeypatch) -> None:
    """A connector with zero completed runs yet (all `queued`/`running`)
    produces `NULL` averages/sums from SQL, not zero -- must map to `None`/
    `0` correctly rather than crashing on arithmetic against `None`.
    """
    organization_id = uuid.uuid4()
    actor = _observability_reader(organization_id)
    row = _FakeIngestionJobStatsRow(
        connector_config_id=uuid.uuid4(),
        run_count=1,
        succeeded_count=None,
        failed_count=None,
        avg_duration_seconds=None,
        total_documents_processed=None,
    )

    async def fake_get_ingestion_job_stats(session, *, organization_id, since=None):
        return [row]

    monkeypatch.setattr(
        tenancy_service.repository, "get_ingestion_job_stats", fake_get_ingestion_job_stats
    )

    result = await tenancy_service.get_ingestion_job_stats(None, actor)

    assert result[0].succeeded_count == 0
    assert result[0].failed_count == 0
    assert result[0].avg_duration_seconds is None
    assert result[0].total_documents_processed == 0
