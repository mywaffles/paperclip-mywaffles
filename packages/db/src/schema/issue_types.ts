import { sql } from "drizzle-orm";
import {
  boolean,
  index,
  jsonb,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
  uuid,
} from "drizzle-orm/pg-core";
import type { IssueTypeFieldDefinition } from "@paperclipai/shared";
import { companies } from "./companies.js";

export const issueTypes = pgTable(
  "issue_types",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    companyId: uuid("company_id").notNull().references(() => companies.id, { onDelete: "cascade" }),
    key: text("key").notNull(),
    name: text("name").notNull(),
    description: text("description"),
    color: text("color").notNull(),
    icon: text("icon"),
    fieldDefinitions: jsonb("field_definitions").$type<IssueTypeFieldDefinition[]>().notNull().default([]),
    isDefault: boolean("is_default").notNull().default(false),
    archivedAt: timestamp("archived_at", { withTimezone: true }),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (table) => ({
    companyIdx: index("issue_types_company_idx").on(table.companyId),
    companyKeyIdx: uniqueIndex("issue_types_company_key_idx").on(table.companyId, table.key),
    companyDefaultIdx: uniqueIndex("issue_types_company_default_idx")
      .on(table.companyId)
      .where(sql`${table.isDefault} = true and ${table.archivedAt} is null`),
  }),
);
