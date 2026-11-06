# Product Lifecycle Management and Quality Modernization

## Executive summary

The meeting aligned AWS, World Link, Databricks, and Deere participants on an iterative assessment to define a practical future state for product lifecycle management (PLM), quality, data integration, and workflow modernization.

The group agreed on the following direction:

* The next phase is an assessment and discovery effort, tentatively sized at 12–16 weeks. The scope and duration will be refined as the team understands the proof-of-value (POV) effort required.
* Two POV efforts will be framed around a common connected architecture. The quality-side PFMEA use case was identified as the immediate candidate, while the second use case remains to be fully confirmed.
* The architecture should validate shared enablement across future use cases rather than optimize only one scenario. The two use cases are expected to represent approximately 80%–90% of the illustrative needs of other functional groups.
* The modernization should preserve effective legacy and modern capabilities, reduce architectural complexity, improve delivery speed, and avoid disrupting committed FY27 and FY28 roadmap outcomes.
* SAP PDM is expected to be retired by the end of Q4 FY28, while SAP ERP remains in place. Future-state discussions must clearly distinguish SAP PDM from SAP ERP.
* The target direction is a common data and integration foundation with appropriate access patterns for operational, transactional, orchestration, and analytical use cases. The conceptual architecture is not yet a final design.
* The assessment must address business value, process redesign opportunities, data ownership, freshness, security, governance, connector ownership, and operating-model requirements—not only technology implementation.

The principal risks are unclear cross-system ownership, inconsistent data quality, insufficiently defined data-freshness requirements, AI governance gaps, and the possibility of automating inefficient processes without addressing their root causes.

## Background from prior PLM discussion

A prior discussion helped explain why the workshop and broader assessment were needed. The group struggled to identify a sufficiently small PLM MVP because even a seemingly minor change tends to cross multiple objects, systems, and teams. The MLPA attribute was offered as one of the smallest representative examples.

MLPA, or material level plan assignment, is maintained on an engineering change notice (ECN). The change-management team owns the value in the SAP PDM environment and uses it to identify the plants where a change will become effective. When a design engineer creates or revises a part, or moves a part between ECNs, the part-development process exposes the applicable plant choices from the ECN and allows the engineer to select the plants expected to adopt the material.

That selection then drives downstream behavior:

* Part development performs material synchronization to the applicable local SAP landscapes, including downstream consumers such as RT and WG.
* Local adoption of the material starts the relevant unit-level workflow and helps determine when the part will be introduced into the factory.
* Change management, part development, and workflow each need different portions of the same information and must remain synchronized when the ECN-level or part-level value changes.

The example demonstrated that one attribute on one object can create dependencies across at least three teams and multiple systems. No team can change the value independently without potentially affecting downstream synchronization, local workflows, plant adoption, and part processing. The issue is therefore not only the attribute itself; it is the lack of a robust, shared way to manage cross-team ownership, propagation, consistency, and process triggers.

The discussion also showed why selecting a single narrow MVP is difficult in this environment. Most meaningful PLM changes touch an ECN, part, BOM, or another connected object, causing the scope to expand quickly. The MLPA example may not represent the largest direct business-value opportunity, but it is a useful technical candidate because it exposes dependency, synchronization, and architectural challenges in a contained way.

The prior discussion ended with a choice between holding another detailed working session before starting or treating the additional interviews, current-state mapping, business-gap analysis, value-gap analysis, and future-architecture work as part of the initial assessment. This led into the workshop’s focus on a structured three- to four-month assessment and POV engagement rather than attempting to define or build a narrowly isolated solution immediately.

## Decisions and alignment

### Confirmed or strongly aligned

* The assessment is the immediate next phase.
* The assessment will be iterative and collaborative. Stakeholders will review and refine the architecture and implementation roadmap throughout the work rather than receive a finished recommendation at the end.
* The POV should demonstrate the common architecture and its ability to support multiple use cases.
* The quality-side PFMEA use case is the immediate POV candidate because the required people and capacity are believed to be available.
* Security requirements will be included in the POV and tested through the selected use cases.
* The study will define the future-state architecture and transition roadmap without changing existing business processes or committed roadmaps by default.
* SAP PDM and SAP ERP must be treated as separate components in future-state documentation.
* The modernization will use incremental migration. Business units should be able to move progressively while maintaining necessary functionality rather than waiting for a full replacement to be completed.
* The proposed architecture does not currently have an identified blocker requiring a fundamentally different approach.
* Data access should be matched to business need. Not every use case requires second-by-second freshness; approximately one-minute consistency may be sufficient for the stated real-time use cases, while some orchestration use cases may tolerate longer delays.
* Commonality should increase over time without requiring every business area to adopt identical capabilities immediately.

### Direction requiring validation

* A common physical data layer appears preferable to continued siloed optimization or a fully virtual, just-in-time warehouse, but the physical-versus-virtual choice remains use-case and performance dependent.
* Databricks may provide the common operational and data foundation, leveraging existing Lakebridge, Lakeflow, Unity Catalog, operational-data-store, and API capabilities.
* Existing EDL and data-product work may be consolidated, extended, or repositioned. The assessment must determine whether those efforts duplicate, complement, or should feed the proposed framework.
* Existing investments—potentially beginning with quality capabilities—should be reused where practical rather than rebuilt from a greenfield starting point.

## Action items

| Action | Owner | Timing or status |
|---|---|---|
| Frame the scope, success criteria, and architecture hypotheses for both POV efforts | Assessment team with participating business and technical groups | Next assessment phase |
| Confirm the PFMEA use case and connect it to the agreed operating structure and framework | Quality POV lead; named owner TBD | Immediate candidate; validate during kickoff |
| Confirm the second POV use case and document its unique requirements | Assessment team and business sponsor; owner TBD | During initial discovery |
| Identify FY27 and FY28 commitments that cannot be affected by modernization | Business and roadmap stakeholders; owner TBD | Early assessment activity |
| Define a common set of business-value and technical-value criteria for prioritizing use cases | Assessment team with business stakeholders | During discovery |
| Validate whether the common architecture supports both POVs and identify any add-ons or gaps | Architecture and POV teams | Iteratively during assessment |
| Add the enterprise part-creation application, front end, part master, and data-flow arrows to the architecture diagram | Architecture documentation team; owner TBD | Before the target-state architecture is baselined |
| Assess the EDL and data-product work performed by the relevant embedded teams, including Adam and Ryan’s teams, for duplication or consolidation opportunities | Discovery team with Adam and Ryan’s teams | During discovery |
| Document source-system ownership, APIs, connector responsibilities, schema/version-change handling, and repair procedures | Domain product teams and integration leads; owner TBD | During target-state design |
| Classify use cases by freshness and consistency requirement, including transactional approval flows versus orchestration | Business process and architecture teams | During discovery |
| Define security, role mapping, SSO, Unity Catalog controls, and any custom authorization translation required by the POV | Security, data-governance, and POV teams | Validate within POV |
| Establish AI governance parameters, monitoring, evaluation, compliance, and litigation controls | AI governance and legal/compliance stakeholders; owner TBD | Before scaling AI-native capabilities |
| Develop the needs assessment, business-value case, detailed deployment architecture, and transition roadmap | Assessment team | Assessment deliverables |
| Use the following working session to refine the scope rather than treating the meeting scope as final | All participating groups | Next scheduled working session |

## Open questions

### Scope and outcomes

* What is the confirmed second POV use case, and what specific business outcome will it demonstrate?
* What exact criteria will determine whether the POV is successful: SAP reduction, reduced digital friction, cycle-time improvement, data freshness, adoption, or another measure?
* Will a single 12-week bucket be sufficient, or will the assessment require a shorter or longer effort to close the remaining architecture gaps?
* Which use cases are illustrative only, and which should be modeled deeply enough to prove operational or transactional behavior?

### Roadmap and business commitments

* Which FY27 and FY28 roadmap items are non-negotiable?
* What modernization changes can be introduced without affecting those commitments?
* Which early capabilities can be delivered before the full target architecture is complete?

### Architecture and data

* Which capabilities should remain in SAP ERP, which should move out of SAP PDM, and which should be implemented in middleware or custom applications?
* How should existing EDL, data-product, and embedded-team capabilities be consolidated or connected?
* Which data should be physically harmonized, and where is virtual access sufficient?
* What is the authoritative source for each key object, including parts, ECNs, MLPA, products, lifecycle data, quality events, and simulation data?
* How will the architecture support both high-volume retrieval, such as approximately 50,000 parts, and low-latency transactional interactions?
* What is the required freshness for each use case, and when is direct source-system access preferable to a common data layer?
* How should simulation, verification, validation, and simulation-data management be represented in the PLM/PDM scope?

### Operating model and governance

* Which resources will be assigned to the POV, at what level of availability, and who will provide timely subject-matter expertise?
* How will federated data ownership work within a shared environment?
* Which governance standards must be enterprise-wide, and which may remain business-specific?
* How will connector ownership be managed when Windchill, SAP, or other source systems change versions or schemas?
* How will AI capabilities built by multiple teams be governed, monitored, and evaluated?

### Process and value

* Which existing processes should be redesigned rather than automated as-is?
* How will improvements be measured across the full process so that removing one queue does not simply move the bottleneck downstream?
* How will the team verify that a corrective action fully resolved a quality problem rather than merely replacing the affected part or changing the record?
* What process-reliability factors are the weakest and therefore offer the highest-value improvement opportunity?

## Cleaned-up meeting minutes

### Meeting purpose and working approach

The meeting was scheduled for 2.5 hours, with an intended wrap-up at 11:00 Central and a hard stop at 11:30. The agenda covered current-state understanding, remaining gaps, roadmaps, pain points, business value, and review of a conceptual target architecture.

Participants from AWS, World Link, Databricks, and Deere were asked to fill in missing context before assessing the architecture. The group emphasized an iterative working model: participants would continuously review and refine the target state, implementation steps, and business rationale so that the final recommendation would be understood and supported by the teams responsible for implementation.

### Modernization objectives and boundaries

The modernization discussion focused first on defining what must change and how the organization could reach the future state, before revisiting the business case. Rami emphasized preserving legacy and modern solutions that work well, reducing architectural complexity, and improving delivery speed without disrupting FY27 and FY28 commitments.

The environment includes SAP, AWS, Databricks, and distributed cloud environments. The group viewed proof-of-value efforts that reduce reliance on SAP as strategically important. Adam clarified that PLM intends to shut down the SAP PDM landscape because its capabilities are covered by Windchill or can be handled in SAP ERP, which will remain. The planned transition away from the relevant SAP PDM landscape is targeted for the end of Q4 FY28; the fiscal year runs from November 1 through October 31.

The existing SAP workflow uses hard-coded templates and requirements, which limits flexibility. The replacement direction supports risk-based process choices, optional activities, and the ability to add tasks during a workflow rather than enforcing one fixed sequence. Migration will be incremental, with custom front ends connecting to Windchill and SAP so business units can transition while preserving needed functionality. This is important because the current environment has evolved over approximately 25 years and roughly 30 units have not yet moved to the existing solutions.

### Delivery improvement and business value

The current delivery environment was characterized by slow delivery, organizational silos, and dependencies. An automation that sets the quality-plan level removed a manual step averaging 8.6 days across approximately 17,500 cases per month. Participants cautioned that optimizing one queue could shift the bottleneck downstream, so cycle time must be evaluated across the complete process.

The product-development initiative targets cycle time and first-pass yield, with the broader purpose of reducing the time required to resolve defects and deliver parts to depots and dealers. A part change after root-cause identification takes approximately 125 days on average and may require coordination across multiple suppliers and sub-tier suppliers. Approximately 40% of changed parts are changed again within 12 months, indicating a need to improve the quality of the initial change.

The design first-pass-yield target is to increase from 60% to 90%. The group connected this goal to fewer repeated design iterations, improved DPAR execution, and faster problem identification and resolution.

### Assessment and proof of value

The proposed assessment will define the recommended future-state architecture and create a small POV to test whether the architecture works in practice. The team will size the MVP or POV during discovery rather than begin development on day one. A working assumption of 12 weeks may be used as a starting point, with a possible 12–16-week assessment depending on the number and size of the POV efforts.

The assessment is expected to produce:

* A comprehensive needs assessment across PLM, quality, and related areas.
* A description of the business problems and value opportunities.
* A detailed target-state deployment architecture.
* A roadmap from the current environment to the target state.
* A rationalized sequence of additional use cases based on business and technical value.

Participants agreed that the POV should validate a shared connected architecture rather than optimize only one scenario. Alex noted that the two use cases have separate business and technical scopes but may share architectural enablement. The group therefore discussed demonstrating the common architecture and using subsequent iterations to test unique requirements.

The quality-side PFMEA use case was identified as the immediate candidate, with capacity believed to be available. Security will be included in the POV. The group also noted that one use case may not demonstrate every success criterion, in which case additional use cases may be needed.

### Conceptual target architecture

The architecture diagram was presented as a discussion aid, not a final solution. Green boxes represented custom applications running on AWS; blue boxes represented purchased Windchill and SAP products. The conceptual direction includes a common data and architecture foundation, an alternate business-facing access layer, workflow orchestration, and connectors to source systems.

The target experience could give project and program managers relevant information—such as nearby-car activity, part details, and part-release status—within the project context rather than requiring searches across multiple systems. Configurable modules could bring together information needed by different business areas.

The future state could also use virtual teammates to build quality plans, route them for human review and approval, and trigger notifications without relying on Teams or email. Auto QPL was cited as an existing capability that monitors design fidelity, supports better decisions, reduces processing time, and may eventually allow the quality-plan step to be skipped when appropriate. Its rollout was scheduled for November 2.

The architecture should separate shared data from the ways applications use that data. Delta tables and a common data foundation could be surfaced through Lakehouse or other access layers while allowing the back end to evolve. The group did not identify an architectural blocker requiring a fundamentally different approach, but agreed that the POV must test the appropriate operational and transactional implementation patterns.

### Process dependencies and operating model

PLM and quality span many processes, systems, and organizations. Key challenges include fragmented data access, limited visibility into updates, opaque dependencies, and long sequential workflows. The current workflow assigns activities based on part type and risk level and collects data across Windchill, SAP, and other systems. The modernization direction is to retain required information while enabling more parallel execution, risk-based task selection, and avoidance of unnecessary activities.

Design, supply chain, manufacturing, and quality were identified as the core connected functions required to move a product into production. Aftermarket and finance are also relevant but secondary to the primary product-delivery flow.

The operating model requires dedicated, though not necessarily full-time, resources and timely access to subject-matter experts. The team discussed federated ownership, in which individual owners remain accountable for their data while collaborating through a common layer. Enterprise governance standards will need to be defined even where operating practices remain tailored to individual business needs.

### PLM, change management, and integration

The PLM environment includes multiple product teams working across Windchill, SAP, and separately provisioned ABES instances. Each product has its own account, illustrating the distributed nature of the current landscape. Simulation and related verification and validation capabilities are associated with design and PVMV rather than being fully owned by PLM.

SAP and Windchill currently synchronize ECNs and ECRs bidirectionally. The main remaining disconnect is between project/program management and change management: project information is manually entered into SAP before an ECN can be created. The team is working toward connecting project.com directly to SAP so project milestones, dates, structure, and related information can drive change-management activity.

Relevant product teams are expected to own and maintain their connectors, including repair when system upgrades disrupt synchronization. The group noted that avoiding duplication requires applications to retrieve information from the owning system, but teams often do not know which question to ask or which API to use.

MLPA was used as an example of a small attribute with broad process impact. Change management owns the attribute because it is stored on the ECN, while engineers use it to determine which local factory should receive a part and which factory workflow must be completed.

### Data access, freshness, and legacy foundations

The proposed front end would retrieve data from multiple source systems through APIs. The group distinguished between single-part lookups and high-volume requests involving approximately 50,000 parts. Not every informational use case requires sub-second performance or second-by-second freshness.

The team differentiated orchestration use cases, which may tolerate a delay of up to roughly two hours, from transactional use cases such as approvals and sign-offs, which require a more immediate status update. The proposed access layer may use direct source-system bypasses for time-sensitive interactions and eventual consistency for less time-sensitive integrations. Approximately one-minute consistency was viewed as sufficient for the stated real-time needs.

Windchill data is currently extracted approximately every four hours, while workflow data has a best-case refresh time of approximately two hours because of its volume and element-level change processing. Windchill cloud migration and newer data technologies may improve these patterns.

The part-data foundation dates to the 1970s, with core engineering attributes still sourced from a COBOL mainframe that is being replaced. The future direction is a single source of truth in the parts master. New parts and foundational attributes should be created there first and then distributed to Windchill, SAP, and other systems. The architecture diagram must be updated to show the enterprise part-creation application, front end, part master, and associated flows.

### Quality modernization and data products

The quality team has prioritized global process standardization before changing application layers. Quality activities and workflows are moving into Horizon outside C Projects PDM. Auto QPL is an early automation iteration. GDSN Colab is being evaluated for decommissioning, while PFMEA remains an internal capability in MES MP Planner for manufactured components and assemblies.

Quality metrics include the D0-to-D6 CAPA lifecycle, currently averaging approximately 275 days. The target is less than 365 days so significant product issues do not remain unresolved across seasons. The time from identifying a problem to protecting the customer has improved from 335 days to 206 days, with a next target below 180 days.

The team discussed claim clustering as an enterprise data product that curates bronze, silver, and gold data, applies AI across multiple data streams, and prioritizes customer complaints and other signals. Dealer work orders, warranty claims, and defect or CCMS records have been linked to support collective module-team review. A separate effort reduced a timeline from 151 days to 49 days through cloud data, infrastructure, and reporting work involving AWS, Databricks, and data products.

Data quality remains a major constraint. Inconsistent primary-part assignment, multiple parts and subsystems, and records arriving at different times limit correlation accuracy. The group also identified a need for complete event lineage covering origin, cause, cost, supplier recovery, corrective action, and proof of effectiveness.

### Governance, security, and cost

Participants raised compliance and litigation concerns because multiple groups are building AI capabilities directly on shared data. A sustainable model will require defined parameters, monitoring, and evaluation before those capabilities scale.

Unity Catalog was discussed as the primary mechanism for governing access to data from systems such as Windchill. Identity-provider integrations, including Azure AD and Okta, may transfer access relatively easily. Custom application authorization stored in databases will require translation. The proposed model includes infrastructure-as-code management and fine-grained row-, column-, and tag-based controls, supported by common role-based access and single sign-on across application layers.

The team also discussed whether bronze, harmonized, and data-product layers could be implemented virtually to control cost. The answer depends on use case and performance requirements; the layers are currently mostly physical because harmonization and data products serve different purposes. The future design must preserve savings from the Tiger team’s Databricks optimization.

## Systems and organizational context from uploaded diagram

The uploaded high-level PLM diagram provides additional context for the current landscape. It groups the PLM capability set into several connected areas:

* Program management: CV/PDP programs, milestones, build dates, and program metrics.
* Change management: project setup, change management, and scheduling.
* Part development: part management, part reuse and classification, early costing, and part-level development.
* Product structure: BOM management across EBOM, CBOM, and PBOM, product configuration, and related structure capabilities.
* Build management: experimental-part ordering orchestration and build management.

These capabilities are shown above a PLM shared-services layer that includes functions such as common segmentation, project assistance, product viewing, build issue tracking, and part release. The diagram also indicates that some capabilities are not available today and that the landscape spans Windchill, SAP PDM, ECN/ECR, material and BOM integration, and other supporting applications.

The diagram reinforces the modernization themes discussed in the meeting:

* PLM capabilities are distributed across multiple products and organizational areas rather than implemented as one unified application.
* Shared services and integration flows are important architectural dependencies across program, part, structure, change, and build processes.
* Some capabilities are current-state gaps or future opportunities, so the diagram should be treated as a high-level landscape view rather than a finalized target architecture.
* The assessment should map the capability areas to system ownership, data ownership, integration patterns, roadmap commitments, and POV scope.

## Engagement planning and funding context

The immediate business-development step is for Mahesh to meet with Rami the following week to review the proposed assessment and POV engagement in greater detail. The discussion will cover:

* Scope and assumptions.
* Expected deliverables.
* Required roles, skills, and staffing model.
* Approximate financial requirements.
* Business value and business justification.
* Funding process and timing.

The current planning estimate is a 12–16-week engagement. The funding discussion indicated approximately $300K–$400K of partner funding, potentially split evenly between AWS and Databricks at roughly $150K–$200K each. World Link is considering an additional approximately $100K in-kind contribution represented by delivery hours. These figures are preliminary planning estimates and remain subject to confirmation through the funding process.

The delivery model is expected to include Mahesh’s participation and a Databricks architect who can be paired with him to support successful execution of the PLM and quality POVs. The required skills and level of allocation will be refined during the engagement-planning discussion.

Rami is expected to obtain the AWS funding template. The team will complete it with the engagement basics, business value, and business justification, then reuse substantially the same content to initiate the Databricks funding process.

The working timeline is:

* Next week: Mahesh and Rami review the assessment and POV details.
* By Thanksgiving 2026: Target to have the scope, funding story, staffing assumptions, and related planning details substantially finalized.
* Early December 2026: Potential soft start for the engagement, subject to funding and planning completion.
* April 2027: Target for delivery and validation, consistent with the stated end-of-Q2 objective and the proposed three- to four-month execution window.

## Updated immediate next steps

The initiative is prepared to move forward: a working session is scheduled for the following week, the purchase order has been cut, and staffing has begun. The next actions are:

* Mahesh to meet with Rami and refine the 12–16-week assessment and POV proposal.
* Confirm scope, assumptions, deliverables, financial estimates, and required roles and skills.
* Obtain and complete the AWS funding template, including business value and business justification.
* Adapt the funding narrative for the Databricks funding process.
* Define the World Link in-kind contribution and confirm the need for a Databricks architect.
* Confirm the soft-start timing and work backward from the April 2027 delivery and validation target.
* Incorporate Alex’s input when available.
* Confirm the two POV scopes, define success criteria, identify non-negotiable roadmap constraints, and update the conceptual architecture to include the enterprise part-creation flow.
