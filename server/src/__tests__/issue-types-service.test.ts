import { randomUUID } from "node:crypto";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { companies, createDb, issues, issueTypes } from "@paperclipai/db";
import {
  getEmbeddedPostgresTestSupport,
  startEmbeddedPostgresTestDatabase,
} from "./helpers/embedded-postgres.js";
import { issueService } from "../services/issues.js";
import { issueTypeService } from "../services/issue-types.js";

const embeddedPostgresSupport = await getEmbeddedPostgresTestSupport();
const describeEmbeddedPostgres = embeddedPostgresSupport.supported ? describe : describe.skip;

if (!embeddedPostgresSupport.supported) {
  console.warn(
    `Skipping embedded Postgres issue type tests on this host: ${embeddedPostgresSupport.reason ?? "unsupported environment"}`,
  );
}

describeEmbeddedPostgres("custom issue types", () => {
  let db!: ReturnType<typeof createDb>;
  let tempDb: Awaited<ReturnType<typeof startEmbeddedPostgresTestDatabase>> | null = null;

  beforeAll(async () => {
    tempDb = await startEmbeddedPostgresTestDatabase("paperclip-issue-types-");
    db = createDb(tempDb.connectionString);
  }, 20_000);

  afterEach(async () => {
    await db.delete(issues);
    await db.delete(issueTypes);
    await db.delete(companies);
  });

  afterAll(async () => {
    await tempDb?.cleanup();
  });

  async function seedCompany() {
    const companyId = randomUUID();
    await db.insert(companies).values({
      id: companyId,
      name: "Custom Types Co",
      issuePrefix: `CT${companyId.replace(/-/g, "").slice(0, 4).toUpperCase()}`,
      requireBoardApprovalForNewAgents: false,
    });
    return companyId;
  }

  it("applies a default type and validates its fields when creating issues", async () => {
    const companyId = await seedCompany();
    const issueType = await issueTypeService(db).create(companyId, {
      key: "communication",
      name: "Communication",
      color: "#2563eb",
      isDefault: true,
      fieldDefinitions: [{
        key: "channel",
        label: "Channel",
        type: "select",
        required: true,
        options: [
          { value: "gmail", label: "Gmail" },
          { value: "text", label: "Text" },
        ],
      }],
    });

    const created = await issueService(db).create(companyId, {
      title: "Reply to a message",
      status: "todo",
      priority: "medium",
      customFields: { channel: "gmail" },
    });

    expect(created).toMatchObject({
      issueTypeId: issueType.id,
      customFields: { channel: "gmail" },
    });
    await expect(issueService(db).create(companyId, {
      title: "Invalid channel",
      status: "todo",
      priority: "medium",
      customFields: { channel: "discord" },
    })).rejects.toMatchObject({ status: 422 });
  });

  it("preserves existing issues when a type is archived and rejects new assignments", async () => {
    const companyId = await seedCompany();
    const typeService = issueTypeService(db);
    const issueType = await typeService.create(companyId, {
      key: "billing",
      name: "Billing",
      color: "#16a34a",
      fieldDefinitions: [],
      isDefault: false,
    });
    const created = await issueService(db).create(companyId, {
      title: "Invoice review",
      status: "todo",
      priority: "medium",
      issueTypeId: issueType.id,
    });

    await typeService.archive(issueType.id);

    const [persisted] = await db.select().from(issues);
    expect(persisted).toMatchObject({ id: created.id, issueTypeId: issueType.id });
    await expect(issueService(db).create(companyId, {
      title: "Another invoice",
      status: "todo",
      priority: "medium",
      issueTypeId: issueType.id,
    })).rejects.toMatchObject({ status: 422 });
  });
});
