import { apiRequest, mockDelay } from "./client";
import { USE_MOCK_DATA } from "./config";
import type {
  AuthUser,
  InvitationAcceptPayload,
  LoginPayload,
  LoginResponse,
  OrganizationSelectionPayload,
  OrganizationsListResponse,
  SessionTokens,
  SignupPayload,
  SwitchOrganizationPayload,
} from "@/types/auth";

interface UserProfileResponse {
  id: string;
  email: string;
  displayName: string;
  isActive: boolean;
  roles: string[];
  permissions: string[];
}

const MOCK_USER: AuthUser = {
  id: "user-5",
  name: "Bhawna Relhan",
  email: "bhawna.relhan@navikenz.com",
  organizationId: "org-1",
  role: "owner",
  permissions: ["tenancy:manage", "knowledge:review", "observability:read", "incident:write", "postmortem:write", "postmortem:approve", "audit:read"],
};

const MOCK_TOKENS: SessionTokens = {
  accessToken: "mock-access-token",
  refreshToken: "mock-refresh-token",
  tokenType: "bearer",
  expiresIn: 3600,
};

export async function signup(payload: SignupPayload): Promise<SessionTokens> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_TOKENS, 400);
  }
  return apiRequest<SessionTokens>("/auth/signup", { method: "POST", body: payload });
}

export async function login(payload: LoginPayload): Promise<LoginResponse> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_TOKENS, 400);
  }
  // core.auth.schemas.LoginResponse: SessionTokens (status: "complete") for
  // a single-organization user, or OrganizationSelectionRequired (status:
  // "organization_selection_required") for a multi-organization one -- the
  // caller (AuthContext.login) must branch on `.status` before treating
  // this as a real session.
  return apiRequest<LoginResponse>("/auth/login", { method: "POST", body: payload });
}

/**
 * Second step of a multi-organization login: exchange the `selectionToken`
 * an `OrganizationSelectionRequired` login response carried, plus the
 * organization the user picked, for real `SessionTokens`. The backend
 * independently re-verifies membership -- `organizationId` here is a
 * request, not a trusted assertion.
 */
export async function selectOrganization(
  payload: OrganizationSelectionPayload,
): Promise<SessionTokens> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_TOKENS, 300);
  }
  return apiRequest<SessionTokens>("/auth/select-organization", { method: "POST", body: payload });
}

/**
 * Re-scope the current session to a different organization the user also
 * belongs to (`POST /auth/switch-organization`, bearer-token authenticated).
 * Returns a brand-new access + refresh token pair for the target
 * organization -- callers should replace their stored session with this
 * response, not merge it into the old one.
 */
export async function switchOrganization(
  payload: SwitchOrganizationPayload,
): Promise<SessionTokens> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_TOKENS, 300);
  }
  return apiRequest<SessionTokens>("/auth/switch-organization", { method: "POST", body: payload });
}

/**
 * Every organization the current caller belongs to (`GET /auth/organizations`),
 * for building an organization switcher. Derived from the caller's own
 * verified identity server-side -- there is no way to ask for another
 * user's memberships through this endpoint.
 */
export async function listMyOrganizations(): Promise<OrganizationsListResponse> {
  if (USE_MOCK_DATA) {
    return mockDelay(
      { organizations: [{ id: "org-1", name: "Navikenz", slug: "navikenz" }] },
      200,
    );
  }
  return apiRequest<OrganizationsListResponse>("/auth/organizations");
}

/**
 * Backend: `POST /invitations/{invitation_id}/accept` (Phase 7.5). Deliberately
 * unauthenticated -- there is no session yet -- so `payload.token` (the
 * single-use secret from the invitation link, distinct from `invitationId`
 * itself) is what proves the caller controls the invited email address.
 */
export async function acceptInvitation(
  invitationId: string,
  payload: InvitationAcceptPayload,
): Promise<SessionTokens> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_TOKENS, 400);
  }
  return apiRequest<SessionTokens>(`/invitations/${invitationId}/accept`, {
    method: "POST",
    body: payload,
  });
}

export async function refreshSession(refreshToken: string): Promise<SessionTokens> {
  return apiRequest<SessionTokens>("/auth/refresh", {
    method: "POST",
    body: { refreshToken },
  });
}

/**
 * `organizationId` comes from the caller (decoded off the access token's own
 * claims, `tokenStore.decodeAccessTokenClaims`) -- `GET /auth/me` describes
 * the user, not which organization their current session is scoped to.
 */
export async function getCurrentUser(organizationId: string): Promise<AuthUser> {
  if (USE_MOCK_DATA) {
    return mockDelay(MOCK_USER, 150);
  }
  const profile = await apiRequest<UserProfileResponse>("/auth/me");
  return {
    id: profile.id,
    name: profile.displayName,
    email: profile.email,
    organizationId,
    role: (profile.roles[0] as AuthUser["role"]) ?? "member",
    permissions: profile.permissions,
  };
}

export async function logout(refreshToken: string): Promise<void> {
  if (USE_MOCK_DATA) {
    return mockDelay(undefined, 100);
  }
  return apiRequest<void>("/auth/logout", { method: "POST", body: { refreshToken } });
}
