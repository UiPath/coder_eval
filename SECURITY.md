# Security Policy

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Instead, use one of the following private channels:

1. **GitHub private vulnerability reporting (preferred for this repo).** Go to the
   [**Security** tab](https://github.com/UiPath/coder_eval/security/advisories/new)
   of this repository and click **"Report a vulnerability"**. This opens a private
   advisory visible only to you and the maintainers.
2. **UiPath Bug Bounty / HackerOne.** For UiPath's coordinated disclosure program,
   report through [HackerOne](https://hackerone.com/uipath) or email
   **hackerone@uipath.com**.
3. **UiPath Trust.** For other security or trust queries, use
   [trust.uipath.com](https://trust.uipath.com/) or email **trust@uipath.com**.

Please include as much of the following as you can:

- A description of the vulnerability and its impact.
- Steps to reproduce (proof-of-concept, affected version/commit, configuration).
- The component involved (e.g. sandbox/agent execution, `agent_judge` criterion,
  Docker isolation layer, telemetry).
- Any suggested remediation.

## What to Expect

- We will acknowledge your report within **5 business days**.
- We will provide an assessment and expected timeline for a fix, and keep you
  informed of progress.
- We will credit you in the advisory once a fix is released, unless you prefer
  to remain anonymous.

Please give us a reasonable opportunity to remediate before any public
disclosure (coordinated disclosure).

## Scope Notes

`coder_eval` executes AI-coding-agent output and evaluator-authored commands. A
few areas are security-sensitive by design and are the most valuable to review:

- **Sandbox / agent execution** — agents run shell commands; isolation is
  provided by the Docker driver and the sandbox layer.
- **`agent_judge` criterion** — spawns a Claude Code SDK agent with tool access
  **using evaluator credentials**; treat its inputs as a trust boundary.
- **Telemetry** — anonymous usage telemetry is emitted to an Application Insights
  resource; it is on by default and can be disabled with `TELEMETRY_ENABLED=false`
  (see the [User Guide](docs/USER_GUIDE.md#usage-telemetry)).

## Supported Versions

This project is pre-1.0; security fixes are applied to the latest release on the
`main` branch. There is no long-term support for older versions.
