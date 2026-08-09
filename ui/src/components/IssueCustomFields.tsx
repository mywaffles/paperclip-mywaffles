import type { IssueCustomFieldValues, IssueTypeFieldDefinition } from "@paperclipai/shared";
import { cn } from "../lib/utils";

interface IssueCustomFieldsProps {
  fields: IssueTypeFieldDefinition[];
  values: IssueCustomFieldValues;
  onChange: (values: IssueCustomFieldValues) => void;
  className?: string;
  disabled?: boolean;
}

function localDateTimeValue(value: unknown) {
  if (typeof value !== "string" || !value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

export function IssueCustomFields({
  fields,
  values,
  onChange,
  className,
  disabled = false,
}: IssueCustomFieldsProps) {
  const setValue = (key: string, value: IssueCustomFieldValues[string] | undefined) => {
    const next = { ...values };
    if (value === undefined || value === null || value === "" || (Array.isArray(value) && value.length === 0)) {
      delete next[key];
    } else {
      next[key] = value;
    }
    onChange(next);
  };

  if (fields.length === 0) return null;

  return (
    <div className={cn("space-y-3", className)}>
      {fields.map((field) => {
        const value = values[field.key];
        const inputClass = "w-full rounded-md border border-border bg-transparent px-2.5 py-1.5 text-sm outline-none focus:border-ring";
        return (
          <label key={field.key} className="block space-y-1.5">
            <span className="flex items-center gap-1 text-xs font-medium text-muted-foreground">
              {field.label}
              {field.required ? <span className="text-destructive" aria-label="required">*</span> : null}
            </span>
            {field.type === "boolean" ? (
              <select
                className={inputClass}
                value={typeof value === "boolean" ? String(value) : ""}
                disabled={disabled}
                onChange={(event) => setValue(field.key, event.target.value === "" ? undefined : event.target.value === "true")}
              >
                <option value="">Not set</option>
                <option value="true">Yes</option>
                <option value="false">No</option>
              </select>
            ) : field.type === "select" ? (
              <select
                className={inputClass}
                value={typeof value === "string" ? value : ""}
                disabled={disabled}
                onChange={(event) => setValue(field.key, event.target.value || undefined)}
              >
                <option value="">Select…</option>
                {(field.options ?? []).map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            ) : field.type === "multi_select" ? (
              <div className="flex flex-wrap gap-2 rounded-md border border-border px-2.5 py-2">
                {(field.options ?? []).map((option) => {
                  const selected = Array.isArray(value) && value.includes(option.value);
                  return (
                    <label key={option.value} className="inline-flex items-center gap-1.5 text-xs">
                      <input
                        type="checkbox"
                        checked={selected}
                        disabled={disabled}
                        onChange={() => {
                          const current = Array.isArray(value) ? value : [];
                          setValue(field.key, selected
                            ? current.filter((entry) => entry !== option.value)
                            : [...current, option.value]);
                        }}
                      />
                      {option.label}
                    </label>
                  );
                })}
              </div>
            ) : (
              <input
                className={inputClass}
                type={field.type === "number"
                  ? "number"
                  : field.type === "date"
                    ? "date"
                    : field.type === "datetime"
                      ? "datetime-local"
                      : field.type === "url"
                        ? "url"
                        : "text"}
                value={field.type === "datetime"
                  ? localDateTimeValue(value)
                  : typeof value === "string" || typeof value === "number"
                    ? value
                    : ""}
                disabled={disabled}
                placeholder={field.description ?? undefined}
                onChange={(event) => {
                  if (!event.target.value) return setValue(field.key, undefined);
                  if (field.type === "number") return setValue(field.key, Number(event.target.value));
                  if (field.type === "datetime") return setValue(field.key, new Date(event.target.value).toISOString());
                  setValue(field.key, event.target.value);
                }}
              />
            )}
            {field.description ? <span className="block text-xs text-muted-foreground">{field.description}</span> : null}
          </label>
        );
      })}
    </div>
  );
}
