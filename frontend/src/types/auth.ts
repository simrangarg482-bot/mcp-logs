export interface AuthUser {
  id: string;
  name: string;
  email: string;
  avatarUrl?: string;
  organizationId: string;
  role: "owner" | "admin" | "member" | "viewer";
  // Flattened permission codes from `GET /auth/me` (`core.users.schemas.
  // UserProfile.permissions`) -- the same set `Identity.permissions`
  // enforces server-side. The frontend uses this only to hide/disable
  // actions for UX; the backend remains authoritative and re-checks every
  // request regardless of what this array says.
  permissions: string[];
}

export interface SessionTokens {
  // Additive (core.auth.schemas.SessionTokens.status), always "complete"
  // for this shape -- present so a discriminated `LoginResponse` union can
  // tell this apart from `OrganizationSelectionRequired` below without a
  // separate type guard. Optional here (rather than a required literal)
  // so any pre-existing frontend code building a `SessionTokens` object by
  // hand (tests, mocks) doesn't need updating just to satisfy the type.
  status?: "complete";
  accessToken: string;
  refreshToken: string;
  tokenType: "bearer";
  expiresIn: number;
}

// Multi-organization login/session design gap fix: a password login no
// longer silently picks an organization for a user who belongs to more
// than one -- see core.auth.service.login_with_password's docstring.
export interface OrganizationSummary {
  id: string;
  name: string;
  slug: string;
}

export interface OrganizationSelectionRequired {
  status: "organization_selection_required";
  // Proves the password check already succeeded; short-lived (backend
  // default 10 minutes, Settings.org_selection_token_expiry_minutes).
  // Presented back to POST /auth/select-organization along with the
  // chosen organizationId -- never usable as a bearer access token
  // (core.auth.service.verify_access_token rejects it by its "type" claim).
  selectionToken: string;
  organizations: OrganizationSummary[];
}

// What POST /auth/login actually returns now (core.auth.schemas.LoginResponse):
// SessionTokens for a single-organization user (unchanged), or
// OrganizationSelectionRequired for a multi-organization one. Callers must
// branch on `.status` before treating the result as a session.
export type LoginResponse = SessionTokens | OrganizationSelectionRequired;

export interface OrganizationSelectionPayload {
  selectionToken: string;
  organizationId: string;
}

export interface SwitchOrganizationPayload {
  organizationId: string;
}

export interface OrganizationsListResponse {
  organizations: OrganizationSummary[];
}

export interface SignupPayload {
  email: string;
  password: string;
  displayName: string;
  organizationName: string;
  organizationSlug: string;
}

export interface LoginPayload {
  email: string;
  password: string;
}

export interface InvitationAcceptPayload {
  token: string;
  password: string;
  displayName?: string;
}
