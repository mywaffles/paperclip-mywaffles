// @vitest-environment jsdom

import { flushSync } from "react-dom";
import { createRoot, type Root } from "react-dom/client";
import type { IssueTypeFieldDefinition } from "@paperclipai/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IssueCustomFields } from "./IssueCustomFields";

describe("IssueCustomFields", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    flushSync(() => root.unmount());
    container.remove();
  });

  it("renders typed fields and returns the updated custom field object", () => {
    const fields: IssueTypeFieldDefinition[] = [
      {
        key: "channel",
        label: "Channel",
        type: "select",
        required: true,
        options: [
          { value: "gmail", label: "Gmail" },
          { value: "text", label: "Text" },
        ],
      },
      { key: "amount", label: "Amount", type: "number", required: false },
    ];
    const onChange = vi.fn();

    flushSync(() => root.render(
      <IssueCustomFields fields={fields} values={{ amount: 12 }} onChange={onChange} />,
    ));

    const select = container.querySelector("select");
    expect(select).not.toBeNull();
    expect(container.textContent).toContain("Channel");
    expect(container.textContent).toContain("Amount");
    flushSync(() => {
      select!.value = "gmail";
      select!.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(onChange).toHaveBeenCalledWith({ amount: 12, channel: "gmail" });
  });
});
