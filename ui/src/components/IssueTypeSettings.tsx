import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ISSUE_TYPE_FIELD_KINDS,
  type CreateIssueType,
  type IssueType,
  type IssueTypeFieldDefinition,
  type IssueTypeFieldKind,
} from "@paperclipai/shared";
import { Check, Pencil, Plus, Trash2, X } from "lucide-react";
import { issuesApi } from "../api/issues";
import { queryKeys } from "../lib/queryKeys";
import { Button } from "./ui/button";

type EditableField = Omit<IssueTypeFieldDefinition, "options"> & { optionsText: string };

interface IssueTypeDraft {
  id: string | null;
  key: string;
  name: string;
  description: string;
  color: string;
  isDefault: boolean;
  fields: EditableField[];
}

function slug(input: string, separator: "-" | "_") {
  return input
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, separator)
    .replace(new RegExp(`^\\${separator}+|\\${separator}+$`, "g"), "")
    .replace(/^[^a-z]+/, "");
}

function emptyDraft(): IssueTypeDraft {
  return {
    id: null,
    key: "",
    name: "",
    description: "",
    // token-extraction: allowlisted — persisted color field requires a concrete hex value.
    color: "#2563eb",
    isDefault: false,
    fields: [],
  };
}

function draftFromType(issueType: IssueType): IssueTypeDraft {
  return {
    id: issueType.id,
    key: issueType.key,
    name: issueType.name,
    description: issueType.description ?? "",
    color: issueType.color,
    isDefault: issueType.isDefault,
    fields: issueType.fieldDefinitions.map((field) => ({
      ...field,
      description: field.description ?? null,
      optionsText: (field.options ?? []).map((option) => `${option.value}:${option.label}`).join(", "),
    })),
  };
}

function parseOptions(input: string) {
  return input
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => {
      const [rawValue, ...rawLabel] = entry.split(":");
      const value = slug(rawValue ?? "", "_");
      return { value, label: rawLabel.join(":").trim() || rawValue?.trim() || value };
    });
}

function payloadFromDraft(draft: IssueTypeDraft): CreateIssueType {
  return {
    key: draft.key,
    name: draft.name,
    description: draft.description.trim() || null,
    color: draft.color,
    icon: null,
    isDefault: draft.isDefault,
    fieldDefinitions: draft.fields.map((field) => ({
      key: field.key,
      label: field.label,
      type: field.type,
      required: field.required,
      description: field.description?.trim() || null,
      ...(field.type === "select" || field.type === "multi_select"
        ? { options: parseOptions(field.optionsText) }
        : {}),
    })),
  };
}

const FIELD_KIND_LABELS: Record<IssueTypeFieldKind, string> = {
  text: "Text",
  number: "Number",
  boolean: "Yes / no",
  select: "Select one",
  multi_select: "Select many",
  date: "Date",
  datetime: "Date and time",
  url: "URL",
};

export function IssueTypeSettings({ companyId }: { companyId: string }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<IssueTypeDraft | null>(null);
  const issueTypesQuery = useQuery({
    queryKey: queryKeys.issues.issueTypes(companyId),
    queryFn: () => issuesApi.listIssueTypes(companyId),
  });
  const issueTypes = issueTypesQuery.data ?? [];

  const saveMutation = useMutation({
    mutationFn: (current: IssueTypeDraft) => {
      const payload = payloadFromDraft(current);
      return current.id
        ? issuesApi.updateIssueType(current.id, payload)
        : issuesApi.createIssueType(companyId, payload);
    },
    onSuccess: () => {
      setDraft(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.issues.issueTypes(companyId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.issues.issueTypes(companyId, true) });
    },
  });
  const archiveMutation = useMutation({
    mutationFn: (id: string) => issuesApi.archiveIssueType(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.issues.issueTypes(companyId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.issues.issueTypes(companyId, true) });
    },
  });

  const canSave = useMemo(() => Boolean(
    draft?.name.trim()
    && draft.key.match(/^[a-z][a-z0-9-]*$/)
    && draft.fields.every((field) => field.label.trim() && field.key.match(/^[a-z][a-z0-9_]*$/)),
  ), [draft]);

  const updateField = (index: number, patch: Partial<EditableField>) => {
    setDraft((current) => current ? {
      ...current,
      fields: current.fields.map((field, fieldIndex) => fieldIndex === index ? { ...field, ...patch } : field),
    } : current);
  };

  return (
    <div className="space-y-3 rounded-md border border-border px-4 py-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-medium">Custom issue types</div>
          <p className="mt-1 text-xs text-muted-foreground">
            Define reusable issue categories with their own typed fields.
          </p>
        </div>
        {!draft ? (
          <Button size="sm" variant="outline" onClick={() => setDraft(emptyDraft())}>
            <Plus className="mr-1.5 h-3.5 w-3.5" />
            Add type
          </Button>
        ) : null}
      </div>

      {!draft ? (
        <div className="space-y-2">
          {issueTypes.map((issueType) => (
            <div key={issueType.id} className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2">
              <div className="flex min-w-0 items-center gap-2.5">
                <span className="h-3 w-3 shrink-0 rounded-full" style={{ backgroundColor: issueType.color }} />
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium">{issueType.name}</span>
                    {issueType.isDefault ? (
                      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground"><Check className="h-3 w-3" /> Default</span>
                    ) : null}
                  </div>
                  <div className="text-xs text-muted-foreground">{issueType.fieldDefinitions.length} fields · {issueType.key}</div>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <Button size="icon-xs" variant="ghost" title="Edit issue type" onClick={() => setDraft(draftFromType(issueType))}>
                  <Pencil className="h-3.5 w-3.5" />
                </Button>
                <Button
                  size="icon-xs"
                  variant="ghost"
                  title="Archive issue type"
                  disabled={archiveMutation.isPending}
                  onClick={() => {
                    if (window.confirm(`Archive issue type “${issueType.name}”? Existing issues keep their values.`)) {
                      archiveMutation.mutate(issueType.id);
                    }
                  }}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          ))}
          {issueTypesQuery.isLoading ? <p className="text-xs text-muted-foreground">Loading issue types…</p> : null}
          {!issueTypesQuery.isLoading && issueTypes.length === 0 ? (
            <p className="text-xs text-muted-foreground">No custom issue types yet. Existing issues remain ordinary tasks.</p>
          ) : null}
        </div>
      ) : (
        <div className="space-y-4 border-t border-border pt-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="space-y-1.5">
              <span className="text-xs font-medium text-muted-foreground">Name</span>
              <input
                className="w-full rounded-md border border-border bg-transparent px-2.5 py-1.5 text-sm outline-none"
                value={draft.name}
                onChange={(event) => setDraft({
                  ...draft,
                  name: event.target.value,
                  key: draft.key || slug(event.target.value, "-"),
                })}
              />
            </label>
            <label className="space-y-1.5">
              <span className="text-xs font-medium text-muted-foreground">Key</span>
              <input
                className="w-full rounded-md border border-border bg-transparent px-2.5 py-1.5 font-mono text-sm outline-none"
                value={draft.key}
                onChange={(event) => setDraft({ ...draft, key: slug(event.target.value, "-") })}
              />
            </label>
          </div>
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-muted-foreground">Description</span>
            <input
              className="w-full rounded-md border border-border bg-transparent px-2.5 py-1.5 text-sm outline-none"
              value={draft.description}
              onChange={(event) => setDraft({ ...draft, description: event.target.value })}
            />
          </label>
          <div className="flex flex-wrap items-center gap-4">
            <label className="inline-flex items-center gap-2 text-xs text-muted-foreground">
              {/* token-extraction: allowlisted — persisted color field requires a concrete hex value. */}
              <input type="color" value={draft.color} onChange={(event) => setDraft({ ...draft, color: event.target.value })} />
              Color
            </label>
            <label className="inline-flex items-center gap-2 text-xs text-muted-foreground">
              <input type="checkbox" checked={draft.isDefault} onChange={(event) => setDraft({ ...draft, isDefault: event.target.checked })} />
              Default for new issues
            </label>
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-medium text-muted-foreground">Fields</span>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setDraft({
                  ...draft,
                  fields: [...draft.fields, {
                    key: "",
                    label: "",
                    type: "text",
                    required: false,
                    description: null,
                    optionsText: "",
                  }],
                })}
              >
                <Plus className="mr-1.5 h-3.5 w-3.5" /> Add field
              </Button>
            </div>
            {draft.fields.map((field, index) => (
              <div key={index} className="space-y-2 rounded-md border border-border p-3">
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <input
                    className="rounded-md border border-border bg-transparent px-2.5 py-1.5 text-sm outline-none"
                    placeholder="Label"
                    value={field.label}
                    onChange={(event) => updateField(index, {
                      label: event.target.value,
                      key: field.key || slug(event.target.value, "_"),
                    })}
                  />
                  <input
                    className="rounded-md border border-border bg-transparent px-2.5 py-1.5 font-mono text-sm outline-none"
                    placeholder="field_key"
                    value={field.key}
                    onChange={(event) => updateField(index, { key: slug(event.target.value, "_") })}
                  />
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <select
                    className="rounded-md border border-border bg-transparent px-2.5 py-1.5 text-sm outline-none"
                    value={field.type}
                    onChange={(event) => updateField(index, { type: event.target.value as IssueTypeFieldKind })}
                  >
                    {ISSUE_TYPE_FIELD_KINDS.map((kind) => <option key={kind} value={kind}>{FIELD_KIND_LABELS[kind]}</option>)}
                  </select>
                  <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                    <input type="checkbox" checked={field.required} onChange={(event) => updateField(index, { required: event.target.checked })} />
                    Required
                  </label>
                  <Button
                    size="icon-xs"
                    variant="ghost"
                    className="ml-auto"
                    title="Remove field"
                    onClick={() => setDraft({ ...draft, fields: draft.fields.filter((_, fieldIndex) => fieldIndex !== index) })}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
                {field.type === "select" || field.type === "multi_select" ? (
                  <input
                    className="w-full rounded-md border border-border bg-transparent px-2.5 py-1.5 text-sm outline-none"
                    placeholder="Options: value:Label, another:Another label"
                    value={field.optionsText}
                    onChange={(event) => updateField(index, { optionsText: event.target.value })}
                  />
                ) : null}
              </div>
            ))}
          </div>

          {saveMutation.isError ? (
            <p className="text-xs text-destructive">{saveMutation.error instanceof Error ? saveMutation.error.message : "Could not save issue type"}</p>
          ) : null}
          <div className="flex items-center gap-2">
            <Button size="sm" disabled={!canSave || saveMutation.isPending} onClick={() => saveMutation.mutate(draft)}>
              {saveMutation.isPending ? "Saving…" : "Save issue type"}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setDraft(null)}>
              <X className="mr-1.5 h-3.5 w-3.5" /> Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
