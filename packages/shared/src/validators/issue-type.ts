import { z } from "zod";

export const ISSUE_TYPE_FIELD_KINDS = [
  "text",
  "number",
  "boolean",
  "select",
  "multi_select",
  "date",
  "datetime",
  "url",
] as const;

export const issueTypeFieldKindSchema = z.enum(ISSUE_TYPE_FIELD_KINDS);

const issueTypeFieldKeySchema = z
  .string()
  .trim()
  .min(1)
  .max(48)
  .regex(/^[a-z][a-z0-9_]*$/, "Field keys must start with a letter and contain only lowercase letters, numbers, and underscores");

const issueTypeFieldOptionSchema = z.object({
  value: z.string().trim().min(1).max(80),
  label: z.string().trim().min(1).max(80),
}).strict();

export const issueTypeFieldDefinitionSchema = z.object({
  key: issueTypeFieldKeySchema,
  label: z.string().trim().min(1).max(80),
  type: issueTypeFieldKindSchema,
  required: z.boolean().optional().default(false),
  description: z.string().trim().max(240).optional().nullable(),
  options: z.array(issueTypeFieldOptionSchema).max(50).optional(),
}).strict().superRefine((field, ctx) => {
  const requiresOptions = field.type === "select" || field.type === "multi_select";
  if (requiresOptions && (!field.options || field.options.length === 0)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["options"],
      message: `${field.type} fields require at least one option`,
    });
  }
  if (!requiresOptions && field.options !== undefined) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["options"],
      message: `${field.type} fields cannot define options`,
    });
  }
  if (field.options) {
    const seen = new Set<string>();
    field.options.forEach((option, index) => {
      if (seen.has(option.value)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["options", index, "value"],
          message: "Option values must be unique",
        });
      }
      seen.add(option.value);
    });
  }
});

export const issueTypeFieldDefinitionsSchema = z
  .array(issueTypeFieldDefinitionSchema)
  .max(50)
  .superRefine((fields, ctx) => {
    const seen = new Set<string>();
    fields.forEach((field, index) => {
      if (seen.has(field.key)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: [index, "key"],
          message: "Field keys must be unique within an issue type",
        });
      }
      seen.add(field.key);
    });
  });

export const createIssueTypeSchema = z.object({
  key: z.string().trim().min(1).max(48).regex(/^[a-z][a-z0-9-]*$/, "Type keys must use lowercase letters, numbers, and hyphens"),
  name: z.string().trim().min(1).max(80),
  description: z.string().trim().max(500).optional().nullable(),
  color: z.string().regex(/^#[0-9a-fA-F]{6}$/, "Color must be a 6-digit hex value"),
  icon: z.string().trim().max(48).optional().nullable(),
  fieldDefinitions: issueTypeFieldDefinitionsSchema.optional().default([]),
  isDefault: z.boolean().optional().default(false),
}).strict();

export const updateIssueTypeSchema = createIssueTypeSchema.partial().extend({
  archivedAt: z.string().datetime().optional().nullable(),
}).strict();

export const issueCustomFieldValueSchema = z.union([
  z.string().max(10_000),
  z.number().finite(),
  z.boolean(),
  z.array(z.string().max(80)).max(50),
  z.null(),
]);

export const issueCustomFieldValuesSchema = z.record(issueTypeFieldKeySchema, issueCustomFieldValueSchema);

function fieldValueSchema(field: z.infer<typeof issueTypeFieldDefinitionSchema>) {
  const schema = (() => {
    switch (field.type) {
      case "number":
        return z.number().finite();
      case "boolean":
        return z.boolean();
      case "select":
        return z.enum(field.options!.map((option) => option.value) as [string, ...string[]]);
      case "multi_select":
        return z.array(z.enum(field.options!.map((option) => option.value) as [string, ...string[]]))
          .max(50)
          .superRefine((values, ctx) => {
            if (field.required && values.length === 0) {
              ctx.addIssue({ code: z.ZodIssueCode.custom, message: "Select at least one option" });
            }
          });
      case "date":
        return z.string().date();
      case "datetime":
        return z.string().datetime({ offset: true });
      case "url":
        return z.string().url().max(2_000);
      case "text":
        return z.string().max(10_000).superRefine((value, ctx) => {
          if (field.required && value.trim().length === 0) {
            ctx.addIssue({ code: z.ZodIssueCode.custom, message: "Required text cannot be empty" });
          }
        });
    }
  })();
  return field.required ? schema : schema.nullable().optional();
}

export function buildIssueCustomFieldValuesSchema(
  definitions: z.infer<typeof issueTypeFieldDefinitionsSchema>,
) {
  const shape: Record<string, z.ZodTypeAny> = {};
  for (const field of definitions) shape[field.key] = fieldValueSchema(field);
  return z.object(shape).strict();
}

export type IssueTypeFieldKind = z.infer<typeof issueTypeFieldKindSchema>;
export type IssueTypeFieldDefinition = z.infer<typeof issueTypeFieldDefinitionSchema>;
export type IssueCustomFieldValue = z.infer<typeof issueCustomFieldValueSchema>;
export type IssueCustomFieldValues = z.infer<typeof issueCustomFieldValuesSchema>;
export type CreateIssueType = z.infer<typeof createIssueTypeSchema>;
export type UpdateIssueType = z.infer<typeof updateIssueTypeSchema>;
