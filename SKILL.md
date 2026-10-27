---
name: codex-conversation-sentinel
description: Configure paired fast and slow Codex inspection heartbeats that watch local conversation compactions, task turns, and transcript size, then prepare same-project conversation handoffs with customizable title prefixes.
---

# Codex Conversation Sentinel

Use this skill when a user wants to install or configure recurring Codex conversation inspections and automatic handoff rules.

## Setup

Read [references/setup-and-runtime.md](references/setup-and-runtime.md) before creating or changing automation.

Present one compact setup form. The user may accept all recommended values or change any field:

- Timezone: the connected Codex host's local timezone.
- Daily split: **11:30**. Slow inspection runs **00:00–11:30**, hourly at **:30**; its 11:30 scan is the final slow scan. Fast inspection then runs until midnight, every **10 minutes**; its midnight scan is the final fast scan.
- Handoff triggers, using OR logic: **3** actual compaction events, **20** task turns, or **50 MB** of transcript log (decimal MB).
- Old/new conversation title prefixes: **OLD·** and **NEW·**.
- When a threshold is reached, whether to create a local successor in the same project automatically. Recommend **yes**; make the choice explicit in the setup summary.

Use the local timezone reported by the connected Codex host. If it cannot be determined, ask for it. Do not claim that a different timezone is supported unless the heartbeat scheduler can honor it.

Ask only for missing or ambiguous values. Once the user supplies or accepts the settings, proceed without an extra confirmation prompt. Create or update the two heartbeat automations in the user's connected Codex using `automation_update`; do not claim success until both are present, the runtime files are installed, and the correct heartbeat is active.

The heartbeat tasks must share the same monitoring rules and state. They differ only in cadence and which local-time window they own. Keep automatic skill invocation enabled.

