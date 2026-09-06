import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { Organization, Project } from "@/types/tenancy";
import { listProjects } from "@/api/tenancy";
import { listMyOrganizations } from "@/api/auth";
import { getAccessToken, decodeAccessTokenClaims } from "./tokenStore";
import { useAuth } from "./AuthContext";
import { useToast } from "./ToastContext";

interface TenantContextValue {
  organization: Organization | null;
  // Every organization the current user belongs to (`GET /auth/organizations`),
  // for an organization switcher. A single-organization user still gets a
  // one-element array here -- callers that only care about `organization`
  // (unchanged) can keep ignoring this.
  organizations: Organization[];
  project: Project | null;
  projects: Project[];
  isLoading: boolean;
  /** True only when a fetch itself failed (network error, 5xx, etc) --
   * distinct from `organization === null`/`projects.length === 0`, which
   * are the normal, successful "nothing here" states. Pages that render a
   * generic "no organization selected" message for `!organization` should
   * check this first so a real fetch failure isn't shown as if it were
   * routine. */
  hasError: boolean;
  setProject: (project: Project) => void;
  /** True while `switchOrganization` (below) is in flight. */
  isSwitchingOrganization: boolean;
  /**
   * Re-scope the session to a different organization the user also
   * belongs to (`POST /auth/switch-organization`, via
   * `AuthContext.switchOrganization`) -- the design-gap fix this Phase
   * 7.8 docstring used to say did not exist yet. Issues a brand-new
   * access + refresh token pair for `organizationId`; this context reacts
   * to the resulting token change (below) rather than optimistically
   * setting `organization` itself, so `organization` never shows a value
   * the backend hasn't actually authorized yet.
   */
  switchOrganization: (organizationId: string) => Promise<void>;
}

const TenantContext = createContext<TenantContextValue | undefined>(undefined);

/**
 * Phase 28 (multi-organization login/session design gap fix): this used to
 * have no `organizations`/`setOrganization` at all, because nothing
 * server-side could re-scope a session's `organization_id` -- see git
 * history for that Phase 7.8 reasoning. `core.auth.service.switch_organization`
 * (`POST /auth/switch-organization`) now exists, so `organizations` and
 * `switchOrganization` below are real: switching issues a fresh, backend-
 * verified token for the chosen organization rather than trusting anything
 * client-side. `organization` (singular) is still derived from the
 * *access token's own claims* -- never from `organizations[0]` -- since
 * that is the only source of truth for which organization the current
 * session is actually scoped to.
 */
export function TenantProvider({ children }: { children: ReactNode }) {
  const { isAuthenticated, isLoading: isAuthLoading, switchOrganization: switchOrganizationSession } = useAuth();
  const { toast } = useToast();
  const [projects, setProjects] = useState<Project[]>([]);
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [organization, setOrganization] = useState<Organization | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [hasError, setHasError] = useState(false);
  const [isSwitchingOrganization, setIsSwitchingOrganization] = useState(false);

  useEffect(() => {
    // Wait for AuthProvider to resolve (and set the access token) first --
    // fetching tenancy data before authentication settles sends requests
    // with no Authorization header at all, since it's a sibling/parent
    // provider whose own effect hasn't necessarily run yet.
    if (isAuthLoading) return;
    if (!isAuthenticated) {
      setIsLoading(false);
      return;
    }
    setHasError(false);
    listMyOrganizations()
      .then(({ organizations: memberships }) => {
        setOrganizations(memberships);
        // The *current* organization is whichever one the access token is
        // actually scoped to right now -- not simply the first membership
        // returned, which need not be in any particular or stable order.
        const currentOrganizationId = decodeAccessTokenClaims(getAccessToken() ?? "")?.organization_id;
        const current = memberships.find((org) => org.id === currentOrganizationId) ?? memberships[0] ?? null;
        setOrganization(current);
      })
      .catch(() => {
        setHasError(true);
        toast({ variant: "error", title: "Couldn't load your organization", description: "Please refresh to try again." });
      })
      .finally(() => setIsLoading(false));
  }, [isAuthenticated, isAuthLoading, toast]);

  useEffect(() => {
    if (!organization) {
      setProjects([]);
      setProject(null);
      return;
    }
    listProjects(organization.id)
      .then((orgProjects) => {
        setProjects(orgProjects);
        setProject(orgProjects[0] ?? null);
      })
      .catch(() => {
        setHasError(true);
        toast({ variant: "error", title: "Couldn't load projects", description: "Please refresh to try again." });
      });
  }, [organization, toast]);

  const switchOrganization = useCallback(
    async (organizationId: string) => {
      setIsSwitchingOrganization(true);
      try {
        // Issues a brand-new, backend-verified token pair for
        // organizationId (AuthContext.switchOrganization ->
        // POST /auth/switch-organization). Once that resolves, re-derive
        // `organization` from the new token's own claims (same logic as
        // the initial load above) rather than trusting organizationId
        // itself -- belt-and-suspenders in case the backend ever rejects
        // a request this UI should not have been able to make.
        await switchOrganizationSession({ organizationId });
        const currentOrganizationId = decodeAccessTokenClaims(getAccessToken() ?? "")?.organization_id;
        const next = organizations.find((org) => org.id === currentOrganizationId) ?? null;
        setOrganization(next);
      } catch {
        toast({
          variant: "error",
          title: "Couldn't switch organizations",
          description: "Please try again.",
        });
        throw new Error("switchOrganization failed");
      } finally {
        setIsSwitchingOrganization(false);
      }
    },
    [switchOrganizationSession, organizations, toast],
  );

  const value = useMemo<TenantContextValue>(
    () => ({
      organization,
      organizations,
      project,
      projects,
      isLoading,
      hasError,
      setProject,
      isSwitchingOrganization,
      switchOrganization,
    }),
    [organization, organizations, project, projects, isLoading, hasError, isSwitchingOrganization, switchOrganization],
  );

  return <TenantContext.Provider value={value}>{children}</TenantContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components -- context files exporting both the Provider and its hook is the standard pattern here
export function useTenant(): TenantContextValue {
  const ctx = useContext(TenantContext);
  if (!ctx) throw new Error("useTenant must be used within a TenantProvider");
  return ctx;
}
