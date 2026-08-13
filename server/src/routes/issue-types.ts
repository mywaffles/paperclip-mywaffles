import { Router } from "express";
import type { Db } from "@paperclipai/db";
import { createIssueTypeSchema, updateIssueTypeSchema } from "@paperclipai/shared";
import { validate } from "../middleware/validate.js";
import { issueTypeService, logActivity } from "../services/index.js";
import { assertBoard, assertCompanyAccess, getAccessibleResource, getActorInfo } from "./authz.js";

export function issueTypeRoutes(db: Db) {
  const router = Router();
  const svc = issueTypeService(db);

  router.get("/companies/:companyId/issue-types", async (req, res) => {
    const companyId = req.params.companyId as string;
    assertCompanyAccess(req, companyId);
    res.json(await svc.list(companyId, { includeArchived: req.query.includeArchived === "true" }));
  });

  router.post("/companies/:companyId/issue-types", validate(createIssueTypeSchema), async (req, res) => {
    const companyId = req.params.companyId as string;
    assertBoard(req);
    assertCompanyAccess(req, companyId);
    const created = await svc.create(companyId, req.body);
    const actor = getActorInfo(req);
    await logActivity(db, {
      companyId,
      actorType: actor.actorType,
      actorId: actor.actorId,
      agentId: actor.agentId,
      runId: actor.runId,
      agentApiKeyId: actor.agentApiKeyId,
      action: "issue_type.created",
      entityType: "issue_type",
      entityId: created.id,
      details: { key: created.key, name: created.name },
    });
    res.status(201).json(created);
  });

  router.patch("/issue-types/:id", validate(updateIssueTypeSchema), async (req, res) => {
    assertBoard(req);
    const existing = await getAccessibleResource(req, res, svc.getById(req.params.id as string), "Issue type not found");
    if (!existing) return;
    const updated = await svc.update(existing.id, req.body);
    if (!updated) return res.status(404).json({ error: "Issue type not found" });
    const actor = getActorInfo(req);
    await logActivity(db, {
      companyId: updated.companyId,
      actorType: actor.actorType,
      actorId: actor.actorId,
      agentId: actor.agentId,
      runId: actor.runId,
      agentApiKeyId: actor.agentApiKeyId,
      action: "issue_type.updated",
      entityType: "issue_type",
      entityId: updated.id,
      details: { key: updated.key, name: updated.name, archivedAt: updated.archivedAt },
    });
    res.json(updated);
  });

  router.delete("/issue-types/:id", async (req, res) => {
    assertBoard(req);
    const existing = await getAccessibleResource(req, res, svc.getById(req.params.id as string), "Issue type not found");
    if (!existing) return;
    const archived = await svc.archive(existing.id);
    if (!archived) return res.status(404).json({ error: "Issue type not found" });
    const actor = getActorInfo(req);
    await logActivity(db, {
      companyId: archived.companyId,
      actorType: actor.actorType,
      actorId: actor.actorId,
      agentId: actor.agentId,
      runId: actor.runId,
      agentApiKeyId: actor.agentApiKeyId,
      action: "issue_type.archived",
      entityType: "issue_type",
      entityId: archived.id,
      details: { key: archived.key, name: archived.name },
    });
    res.json(archived);
  });

  return router;
}
