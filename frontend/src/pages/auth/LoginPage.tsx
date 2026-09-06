import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { useToast } from "@/context/ToastContext";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import type { OrganizationSummary } from "@/types/auth";

export function LoginPage() {
  const { login, selectOrganization } = useAuth();
  const { toast } = useToast();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Set only for a multi-organization user (core.auth.schemas.
  // OrganizationSelectionRequired) -- the password check already
  // succeeded, but no session exists yet until one of these is chosen.
  // Non-null is what switches this page from the password form to the
  // organization picker below.
  const [pendingSelection, setPendingSelection] = useState<{
    selectionToken: string;
    organizations: OrganizationSummary[];
  } | null>(null);
  const [selectingOrganizationId, setSelectingOrganizationId] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setIsSubmitting(true);
    try {
      const outcome = await login({ email, password });
      if (outcome?.status === "organization_selection_required") {
        setPendingSelection({
          selectionToken: outcome.selectionToken,
          organizations: outcome.organizations,
        });
        return;
      }
      navigate("/ask");
    } catch {
      toast({
        variant: "error",
        title: "Sign in failed",
        description: "Check your email and password and try again.",
      });
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleSelectOrganization(organizationId: string) {
    if (!pendingSelection) return;
    setSelectingOrganizationId(organizationId);
    try {
      await selectOrganization({
        selectionToken: pendingSelection.selectionToken,
        organizationId,
      });
      navigate("/ask");
    } catch {
      toast({
        variant: "error",
        title: "Couldn't sign in to that organization",
        description: "Please try again.",
      });
    } finally {
      setSelectingOrganizationId(null);
    }
  }

  if (pendingSelection) {
    return (
      <div className="rounded-lg border border-border bg-surface px-6 py-6 shadow-panel">
        <h1 className="mb-1 text-center text-lg font-semibold text-ink">Choose an organization</h1>
        <p className="mb-4 text-center text-xs text-ink-subtle">
          Your account belongs to more than one organization. Pick one to continue.
        </p>
        <div className="flex flex-col gap-2">
          {pendingSelection.organizations.map((org) => (
            <Button
              key={org.id}
              type="button"
              variant="secondary"
              className="w-full justify-start"
              isLoading={selectingOrganizationId === org.id}
              disabled={selectingOrganizationId !== null && selectingOrganizationId !== org.id}
              onClick={() => handleSelectOrganization(org.id)}
            >
              {org.name}
            </Button>
          ))}
        </div>
        <button
          type="button"
          className="mt-4 block w-full text-center text-xs font-medium text-ink-subtle hover:text-ink"
          onClick={() => setPendingSelection(null)}
        >
          Back to sign in
        </button>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-border bg-surface px-6 py-6 shadow-panel">
      <h1 className="mb-4 text-center text-lg font-semibold text-ink">Sign in to EKIP</h1>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div>
          <label htmlFor="email" className="mb-1.5 block text-xs font-medium text-ink-muted">
            Work email
          </label>
          <Input
            id="email"
            type="email"
            required
            autoFocus
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="password" className="mb-1.5 block text-xs font-medium text-ink-muted">
            Password
          </label>
          <Input
            id="password"
            type="password"
            required
            placeholder="••••••••"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        <Button type="submit" variant="primary" className="w-full" isLoading={isSubmitting}>
          Sign in
        </Button>
      </form>

      <p className="mt-6 text-center text-xs text-ink-subtle">
        Don't have an account?{" "}
        <Link to="/signup" className="font-medium text-accent hover:underline">
          Create one
        </Link>
      </p>
    </div>
  );
}
