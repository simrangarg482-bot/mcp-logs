import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type {
  AuthUser,
  InvitationAcceptPayload,
  LoginPayload,
  OrganizationSelectionPayload,
  OrganizationSelectionRequired,
  SessionTokens,
  SignupPayload,
  SwitchOrganizationPayload,
} from "@/types/auth";
import * as authApi from "@/api/auth";
import {
  clearSession,
  decodeAccessTokenClaims,
  dedupedRefresh,
  getRefreshToken,
  SESSION_EXPIRED_EVENT,
  setAccessToken,
  setRefreshToken,
} from "./tokenStore";

// login's return value: undefined for the unchanged single-organization
// case (a session was already applied, same as before this fix existed),
// or the OrganizationSelectionRequired payload when the caller must show
// an organization picker before a session exists. Callers that only ever
// dealt with single-organization users can keep ignoring the return value
// entirely -- it stays undefined for them, exactly as before.
type LoginOutcome = OrganizationSelectionRequired | undefined;

interface AuthContextValue {
  user: AuthUser | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  signup: (payload: SignupPayload) => Promise<void>;
  login: (payload: LoginPayload) => Promise<LoginOutcome>;
  // Completes a multi-organization login after the caller shows the user
  // OrganizationSelectionRequired.organizations and they pick one.
  selectOrganization: (payload: OrganizationSelectionPayload) => Promise<void>;
  // Re-scopes an already-authenticated session to a different organization
  // the user also belongs to (POST /auth/switch-organization).
  switchOrganization: (payload: SwitchOrganizationPayload) => Promise<void>;
  acceptInvitation: (invitationId: string, payload: InvitationAcceptPayload) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const loadUserFromAccessToken = useCallback(async (accessToken: string) => {
    const claims = decodeAccessTokenClaims(accessToken);
    const organizationId = claims?.organization_id ?? "";
    const currentUser = await authApi.getCurrentUser(organizationId);
    setUser(currentUser);
  }, []);

  useEffect(() => {
    const storedRefreshToken = getRefreshToken();
    if (!storedRefreshToken) {
      setIsLoading(false);
      return;
    }
    dedupedRefresh(storedRefreshToken, authApi.refreshSession)
      .then(async (tokens) => {
        setAccessToken(tokens.accessToken);
        setRefreshToken(tokens.refreshToken);
        await loadUserFromAccessToken(tokens.accessToken);
      })
      .catch(() => {
        clearSession();
        setUser(null);
      })
      .finally(() => setIsLoading(false));
  }, [loadUserFromAccessToken]);

  useEffect(() => {
    // Fired by api/client.ts on a 401 from an authenticated request --
    // tokenStore.clearSessionAndNotifyExpired already cleared the stored
    // tokens; clearing `user` here is what actually makes `isAuthenticated`
    // false, which is what ProtectedRoute's existing redirect already keys
    // off of. No new redirect logic needed -- this reuses the ordinary
    // "not authenticated" path a fresh unauthenticated visitor already hits.
    const handleSessionExpired = () => setUser(null);
    window.addEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
  }, []);

  const applySession = useCallback(
    async (tokens: SessionTokens) => {
      setAccessToken(tokens.accessToken);
      setRefreshToken(tokens.refreshToken);
      await loadUserFromAccessToken(tokens.accessToken);
    },
    [loadUserFromAccessToken],
  );

  const handleSignup = useCallback(
    async (payload: SignupPayload) => {
      await applySession(await authApi.signup(payload));
    },
    [applySession],
  );

  const handleLogin = useCallback(
    async (payload: LoginPayload): Promise<LoginOutcome> => {
      const result = await authApi.login(payload);
      if (result.status === "organization_selection_required") {
        // Do NOT touch stored tokens/user state here -- login succeeding
        // for a multi-organization user does not mean a session exists yet.
        // The caller must show `result.organizations` and call
        // `selectOrganization` with the user's choice.
        return result;
      }
      await applySession(result);
      return undefined;
    },
    [applySession],
  );

  const handleSelectOrganization = useCallback(
    async (payload: OrganizationSelectionPayload) => {
      await applySession(await authApi.selectOrganization(payload));
    },
    [applySession],
  );

  const handleSwitchOrganization = useCallback(
    async (payload: SwitchOrganizationPayload) => {
      // A fresh access + refresh token pair for the target organization --
      // applySession replaces the stored session outright (never merges),
      // matching switch_organization's "new token, not a mutated old one"
      // model server-side.
      await applySession(await authApi.switchOrganization(payload));
    },
    [applySession],
  );

  const handleAcceptInvitation = useCallback(
    async (invitationId: string, payload: InvitationAcceptPayload) => {
      await applySession(await authApi.acceptInvitation(invitationId, payload));
    },
    [applySession],
  );

  const handleLogout = useCallback(async () => {
    const storedRefreshToken = getRefreshToken();
    try {
      if (storedRefreshToken) {
        await authApi.logout(storedRefreshToken);
      }
    } finally {
      clearSession();
      setUser(null);
    }
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      isAuthenticated: Boolean(user),
      isLoading,
      signup: handleSignup,
      login: handleLogin,
      selectOrganization: handleSelectOrganization,
      switchOrganization: handleSwitchOrganization,
      acceptInvitation: handleAcceptInvitation,
      logout: handleLogout,
    }),
    [
      user,
      isLoading,
      handleSignup,
      handleLogin,
      handleSelectOrganization,
      handleSwitchOrganization,
      handleAcceptInvitation,
      handleLogout,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components -- context files exporting both the Provider and its hook is the standard pattern here
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
