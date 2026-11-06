# Open questions

Priority 1 questions change what we build in week 1. The rest can be answered as we go. Where the meeting record or brief already gives a partial answer, it is quoted in plain words.

## Priority 1

1. **Process steps.** What are the roughly 100 process steps? Do you have the real list (names, order, owner role, and which source system each step lives in), or should we invent a plausible one? Are they a fixed sequence or a graph with parallel and optional steps? (The meeting says the replacement allows risk-based, optional and added tasks, so a graph is more realistic.)
2. **Quality use case.** Is the POC quality use case PFMEA, or generic quality checks? If PFMEA, which objects matter (failure modes, causes, controls, severity, occurrence, detection, action priority)?
3. **Freshness targets.** The brief says near real time within an hour. The meeting says about one minute for transactional approvals and up to two hours for orchestration. Which do we prove? Or both, as two use cases?
4. **Sync path.** Is the Gold to Lakebase synced-table path the only path to test, or should we keep the direct-apply harness as a comparison baseline? (I recommend keeping both.)
5. **Workspace state.** In `plm-ods-poc`: is Lakehouse RT enabled or requested? Is Unity Catalog set up with a catalog and schema for us? Do you have permission to create pipelines and sync tables? Is the cost guard (auto-shutoff, alerts) already on?
6. **Source of truth for parts.** The meeting says a new part master (replacing the COBOL mainframe) will be the single source. Do we simulate it as a source, or keep the five sources in the diagram?

## Priority 2: architecture and scope

7. **Diagram changes.** Should the diagram be updated now with the part master, the part-creation application and front end, CAD/CAM, and a separate SAP ERP, as the meeting's action item says? Or kept as given for the POC?
8. **Lakebridge.** The brief gives it the role of initial historical load. Is this the client's stated plan, or our assumption? We should check what Lakebridge does before building the bulk-load step on it.
9. **Kafka and event mesh.** Does the client have an event mesh? If we simulate it, do we use files, or a real broker (Kafka, or Zerobus ingest)?
10. **GraphQL and REST serving.** Do we build a thin API over Lakebase to test the serverless read and write-back layer, or only test at the database?
11. **Genie.** Genie works only on Unity Catalog tables, not on Lakebase or RT. Do we test it on Silver or Gold, and what questions do the users actually ask?
12. **IWOL and the AI layer.** What are they, what interface do they expose, and where do you expect them to sit relative to Lakebase? Any documentation we can read?

## Priority 2: security

13. **Identity.** Which identity provider and role model does the client use (Azure AD, Okta)? The meeting mentions custom authorization stored in databases that needs translation. Do we have an example?
14. **Policies to test.** Which policy rules should the drift test cover: plant, export-control class, supplier, program, role? We already generate plant and export-control class.

## Priority 2: data

15. **Volume.** The meeting mentions looking up about 50,000 parts at once. Do we generate 50,000 parts with 100 steps each (5 million step rows), or keep the small profile and prove the pattern only?
16. **Realism.** Are real client schemas or sample extracts available (even redacted column lists from SAP PDM and Windchill)? Real column names would save rework.
17. **ECN and MLPA.** The meeting uses MLPA (material level plan assignment, set on an ECN, selecting plants) as the smallest representative change. Should we model it? It would make a good end to end test of cross-team propagation.
18. **History versus current state.** For process steps, do apps need only current state, or the full history of each step (who, when, why)?

## Priority 3: delivery and governance

19. **Audience.** Who reviews the internal architecture review at the end of week 3, and what do they want to see?
20. **Decision log.** Do you want findings in this repo only, or also as slides or a document for the client?
21. **AI governance.** Is an AI-governance finding (monitoring, evaluation, compliance, litigation controls) in scope for us, or only noted as a risk?
22. **Non-Databricks option.** The brief says to consider integration with a non-Databricks platform. Which one is realistic, and what comparison do you need?
