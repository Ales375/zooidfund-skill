# Contributor instructions

This repository publishes the zooidfund agent skill. This file guides maintainers; it is not runtime skill content. Keep it out of ClawHub bundles.

Changes to payment, authorization, or recovery behavior, including changes made only in `SKILL.md` prose, need a fresh independent GPT-6 Sol/high reviewer who did not design or implement the change. A coordinator who shaped the solution cannot count as independent. Verify the reviewer's actual model and effort from runtime metadata rather than its request or self-report.

Give the reviewer the objective, exact current commit, relevant source, observable provider responses, and the tools the consuming agent actually has, including absent optional capabilities. Check ordinary success, ambiguous failure, and successful recovery. Executable checks must exercise the changed path; written scenarios are manual evaluation, not proof of runtime enforcement. Record current-head review evidence and resolve material findings before marking the PR ready.

Keep ordinary work proportionate. Existing approval boundaries still apply; this file grants no authority to transfer funds, accept terms, change live agents, deploy, merge, or publish.

Canonical maintainer policy: [zooidfund consequential-behavior review](https://github.com/Ales375/zooidfund/blob/main/docs/operations/codex-workflow.md#consequential-behavior-review) (private repository). The rule above stands on its own. Governance companion: [zooidfund PR #118](https://github.com/Ales375/zooidfund/pull/118) under [Issue #104](https://github.com/Ales375/zooidfund/issues/104).
