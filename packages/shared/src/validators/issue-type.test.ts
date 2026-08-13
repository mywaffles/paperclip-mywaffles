import { describe, expect, it } from "vitest";
import {
  buildIssueCustomFieldValuesSchema,
  createIssueTypeSchema,
  issueTypeFieldDefinitionsSchema,
} from "./issue-type.js";

describe("custom issue types", () => {
  const fields = issueTypeFieldDefinitionsSchema.parse([
    { key: "channel", label: "Channel", type: "select", required: true, options: [
      { value: "gmail", label: "Gmail" },
      { value: "text", label: "Text" },
    ] },
    { key: "source_url", label: "Source URL", type: "url" },
    { key: "needs_approval", label: "Needs approval", type: "boolean" },
    { key: "participants", label: "Participants", type: "multi_select", options: [
      { value: "matt", label: "Matt" },
      { value: "gay", label: "Gay" },
    ] },
    { key: "summary", label: "Summary", type: "text", required: true },
  ]);

  it("accepts a reusable type definition with typed fields", () => {
    const parsed = createIssueTypeSchema.parse({
      key: "communication",
      name: "Communication",
      color: "#2563eb",
      fieldDefinitions: fields,
    });

    expect(parsed.fieldDefinitions).toHaveLength(5);
    expect(parsed.isDefault).toBe(false);
  });

  it("rejects duplicate field keys and select fields without options", () => {
    expect(issueTypeFieldDefinitionsSchema.safeParse([
      { key: "channel", label: "Channel", type: "text" },
      { key: "channel", label: "Again", type: "text" },
    ]).success).toBe(false);
    expect(issueTypeFieldDefinitionsSchema.safeParse([
      { key: "channel", label: "Channel", type: "select" },
    ]).success).toBe(false);
  });

  it("validates values against the selected type and rejects unknown fields", () => {
    const schema = buildIssueCustomFieldValuesSchema(fields);
    expect(schema.parse({
      channel: "gmail",
      source_url: "https://mail.google.com/mail/u/0/#inbox/example",
      needs_approval: true,
      participants: ["matt", "gay"],
      summary: "Follow up",
    })).toEqual(expect.objectContaining({ channel: "gmail" }));
    expect(schema.safeParse({ channel: "discord", summary: "Follow up" }).success).toBe(false);
    expect(schema.safeParse({ channel: "gmail", summary: "Follow up", unexpected: "value" }).success).toBe(false);
    expect(schema.safeParse({ channel: "gmail", summary: "   " }).success).toBe(false);
  });
});
