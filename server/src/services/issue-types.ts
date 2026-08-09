import { and, asc, eq, isNull } from "drizzle-orm";
import type { Db } from "@paperclipai/db";
import { issues, issueTypes } from "@paperclipai/db";
import {
  buildIssueCustomFieldValuesSchema,
  issueTypeFieldDefinitionsSchema,
  type CreateIssueType,
  type IssueCustomFieldValues,
  type UpdateIssueType,
} from "@paperclipai/shared";
import { unprocessable } from "../errors.js";

type IssueTypeReader = Pick<Db, "select">;

function validationDetails(error: { issues: Array<{ path: PropertyKey[]; message: string }> }) {
  return {
    fieldErrors: error.issues.map((issue) => ({
      path: issue.path.map(String).join("."),
      message: issue.message,
    })),
  };
}

export async function validateIssueCustomFieldsForType(
  dbOrTx: IssueTypeReader,
  input: {
    companyId: string;
    issueTypeId: string | null;
    customFields: IssueCustomFieldValues;
    allowArchived?: boolean;
  },
) {
  if (!input.issueTypeId) {
    if (Object.keys(input.customFields).length > 0) {
      throw unprocessable("Custom fields require an issue type");
    }
    return { issueType: null, customFields: {} as IssueCustomFieldValues };
  }

  const issueType = await dbOrTx
    .select()
    .from(issueTypes)
    .where(and(eq(issueTypes.id, input.issueTypeId), eq(issueTypes.companyId, input.companyId)))
    .then((rows) => rows[0] ?? null);
  if (!issueType) throw unprocessable("Issue type is invalid for this company");
  if (issueType.archivedAt && !input.allowArchived) {
    throw unprocessable("Archived issue types cannot be assigned to new issues");
  }

  const definitions = issueTypeFieldDefinitionsSchema.parse(issueType.fieldDefinitions);
  const parsed = buildIssueCustomFieldValuesSchema(definitions).safeParse(input.customFields);
  if (!parsed.success) {
    throw unprocessable("Custom fields do not match the selected issue type", validationDetails(parsed.error));
  }
  return { issueType, customFields: parsed.data as IssueCustomFieldValues };
}

export function issueTypeService(db: Db) {
  return {
    list: (companyId: string, options: { includeArchived?: boolean } = {}) =>
      db
        .select()
        .from(issueTypes)
        .where(and(
          eq(issueTypes.companyId, companyId),
          options.includeArchived ? undefined : isNull(issueTypes.archivedAt),
        ))
        .orderBy(asc(issueTypes.name), asc(issueTypes.id)),

    getById: (id: string) =>
      db.select().from(issueTypes).where(eq(issueTypes.id, id)).then((rows) => rows[0] ?? null),

    getDefault: (companyId: string, dbOrTx: IssueTypeReader = db) =>
      dbOrTx
        .select()
        .from(issueTypes)
        .where(and(
          eq(issueTypes.companyId, companyId),
          eq(issueTypes.isDefault, true),
          isNull(issueTypes.archivedAt),
        ))
        .then((rows) => rows[0] ?? null),

    create: (companyId: string, data: CreateIssueType) =>
      db.transaction(async (tx) => {
        if (data.isDefault) {
          await tx.update(issueTypes).set({ isDefault: false, updatedAt: new Date() }).where(eq(issueTypes.companyId, companyId));
        }
        return tx.insert(issueTypes).values({
          companyId,
          key: data.key,
          name: data.name,
          description: data.description ?? null,
          color: data.color,
          icon: data.icon ?? null,
          fieldDefinitions: data.fieldDefinitions,
          isDefault: data.isDefault,
        }).returning().then((rows) => rows[0]);
      }),

    update: async (id: string, data: UpdateIssueType) =>
      db.transaction(async (tx) => {
        const existing = await tx.select().from(issueTypes).where(eq(issueTypes.id, id)).then((rows) => rows[0] ?? null);
        if (!existing) return null;
        const archivedAt = data.archivedAt === undefined
          ? existing.archivedAt
          : data.archivedAt === null
            ? null
            : new Date(data.archivedAt);
        const nextIsDefault = archivedAt ? false : data.isDefault ?? existing.isDefault;
        const nextDefinitions = data.fieldDefinitions ?? existing.fieldDefinitions;
        if (data.fieldDefinitions) {
          const valueRows = await tx
            .select({ id: issues.id, customFields: issues.customFields })
            .from(issues)
            .where(and(eq(issues.companyId, existing.companyId), eq(issues.issueTypeId, id)));
          const schema = buildIssueCustomFieldValuesSchema(issueTypeFieldDefinitionsSchema.parse(nextDefinitions));
          const invalid = valueRows.find((row) => !schema.safeParse(row.customFields).success);
          if (invalid) {
            throw unprocessable("Field changes would invalidate existing issues", { issueId: invalid.id });
          }
        }
        if (nextIsDefault) {
          await tx
            .update(issueTypes)
            .set({ isDefault: false, updatedAt: new Date() })
            .where(and(eq(issueTypes.companyId, existing.companyId), isNull(issueTypes.archivedAt)));
        }
        return tx.update(issueTypes).set({
          ...(data.key !== undefined ? { key: data.key } : {}),
          ...(data.name !== undefined ? { name: data.name } : {}),
          ...(data.description !== undefined ? { description: data.description ?? null } : {}),
          ...(data.color !== undefined ? { color: data.color } : {}),
          ...(data.icon !== undefined ? { icon: data.icon ?? null } : {}),
          ...(data.fieldDefinitions !== undefined ? { fieldDefinitions: data.fieldDefinitions } : {}),
          isDefault: nextIsDefault,
          archivedAt,
          updatedAt: new Date(),
        }).where(eq(issueTypes.id, id)).returning().then((rows) => rows[0] ?? null);
      }),

    archive: (id: string) =>
      db.update(issueTypes).set({
        isDefault: false,
        archivedAt: new Date(),
        updatedAt: new Date(),
      }).where(eq(issueTypes.id, id)).returning().then((rows) => rows[0] ?? null),
  };
}
