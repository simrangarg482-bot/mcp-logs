import { ChevronsUpDown, Building2 } from "lucide-react";
import { useTenant } from "@/context/TenantContext";
import { DropdownMenu } from "@/components/ui/DropdownMenu";

export function TenantSwitcher() {
  const { organization, organizations, project, projects, setProject, switchOrganization, isSwitchingOrganization } =
    useTenant();

  if (!organization) return null;

  // Only render an organization switcher at all for a multi-organization
  // user (Phase 28: core.auth.service.switch_organization) -- a
  // single-organization user (organizations.length <= 1) sees exactly the
  // project switcher this component always rendered, unchanged.
  const otherOrganizations = organizations.filter((org) => org.id !== organization.id);

  return (
    <div className="flex items-center gap-1">
      {otherOrganizations.length > 0 && (
        <DropdownMenu
          align="left"
          trigger={
            <button
              type="button"
              disabled={isSwitchingOrganization}
              title="Switch organization"
              className="flex items-center gap-1 rounded-md px-1.5 py-1.5 text-left hover:bg-slate-100 disabled:opacity-50"
            >
              <Building2 className="h-3.5 w-3.5 text-ink-subtle" />
              <ChevronsUpDown className="h-3 w-3 text-ink-subtle" />
            </button>
          }
          items={otherOrganizations.map((org) => ({
            label: org.name,
            onSelect: () => {
              void switchOrganization(org.id);
            },
          }))}
        />
      )}
      <DropdownMenu
        align="left"
        trigger={
          <button
            type="button"
            className="flex items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-slate-100"
          >
            <div className="flex flex-col leading-tight">
              <span className="text-xs font-semibold text-ink">{organization.name}</span>
              <span className="flex items-center gap-1 text-xs text-ink-muted">
                {project?.name ?? "Select project"}
              </span>
            </div>
            <ChevronsUpDown className="h-3.5 w-3.5 text-ink-subtle" />
          </button>
        }
        items={projects.map((p) => ({
          label: p.name,
          onSelect: () => setProject(p),
        }))}
      />
    </div>
  );
}
