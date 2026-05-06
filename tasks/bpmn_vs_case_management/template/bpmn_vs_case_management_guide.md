# How to choose: Maestro BPMN process vs. Maestro Agentic Case Management

Use this guide to decide which Maestro project type best fits an automation
requirement. Output one of two labels: `bpmn` or `case_management`.

## One-line test

> If the next step depends on what just happened and no single flowchart can
> capture every path, choose **case_management**. If the process follows the
> same path every time, choose **bpmn**.

## Decision signals

Score the requirement against the signals below. The side with more strong
signals wins. A single very strong signal (e.g. multi-week duration with
re-entry, or sub-second deterministic flow) is usually decisive.

### Signals that point to `bpmn`

- **Short-lived**: completes in seconds, minutes, or at most hours.
- **Predictable sequence**: the same ordered steps every run.
- **Deterministic branching only**: any `if/else` or `switch` is rule-based and
  fully specified at design time.
- **System-to-system**: integrations, ETL, scheduled jobs, event-triggered
  pipelines, no human judgment in the loop.
- **No persistent business record**: state is just process variables for the
  duration of the run.
- **No SLA escalations or stage-level access control** are required.
- **Failures retry or alert**, they don't reroute the work to a different team.
- Typical examples: nightly reconciliation, invoice 3-way match, lead routing,
  password reset, inventory sync, scheduled report generation, account
  provisioning with a fixed step list.

### Signals that point to `case_management`

- **Long-running**: spans days, weeks, or months.
- **Exception-heavy and non-linear**: the path depends on what just happened;
  no single flowchart captures every variant.
- **Stages with re-entry / rework**: work commonly bounces back to an earlier
  stage when new info arrives or a fix doesn't hold.
- **Multiple roles with stage-aware access**: different personas (analyst,
  underwriter, supervisor, investigator, attorney) see and act in different
  stages.
- **SLA tracking and escalations** at case and/or stage level, with at-risk
  and breach behaviors.
- **Audit trail required**: every decision, transition, and data change must
  be recorded (often for regulators).
- **Ad-hoc tasks**: case workers create new tasks at runtime that weren't
  modeled at design time.
- **Persistent business record (case entity)**: a typed record like a claim,
  application, dispute, or investigation that all stages read from and write
  to over the case lifetime.
- **Pause / resume**: the case waits on external parties (claimants, vendors,
  applicants, courts) and resumes when they respond.
- Typical examples: insurance claims, loan/mortgage origination, KYC/AML
  remediation, vendor onboarding, customer complaint escalation, chargeback
  disputes, prior authorization with appeals, public-sector investigations,
  building permits, HR misconduct investigations, order-fulfillment
  exceptions.

## Common confusions

- **A long-running scheduled job is still BPMN** if the same steps run on a
  timer with no human review and no re-entry. Duration alone is not enough —
  look for exceptions, multiple roles, and re-entry.
- **A deterministic workflow with one human approval step is still BPMN** if
  the approver is a single role on a fixed step. Case management kicks in
  when multiple roles act across multiple stages with rework loops.
- **Multiple integrations are still BPMN** when they run in a fixed pipeline.
  It's the *non-linear, judgment-driven* coordination across roles and time
  that makes something a case.
- **A BPMN process can be invoked as a task inside a case.** When in doubt
  about a sub-flow that is itself well-defined and short, that piece is
  `bpmn` even if it lives inside a larger case.

## How to answer

After reading the automation details, write exactly one lowercase label —
`bpmn` or `case_management` — to the prediction file. No quotes, no extra
text, no explanation.
