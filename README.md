# Codex Conversation Sentinel

[English](README.md) · [简体中文](README.zh-CN.md)

An installable Codex skill that configures two mutually exclusive local conversation inspection heartbeats and their handoff thresholds.

## Project files

The skill source is at the repository root. `VERSION` is the package version; build the installable ZIP with `./scripts/package.sh`. The script archives an older root package into `dist/history/`, refuses to reuse a version already packaged, and prints the new ZIP's SHA-256.

## Technology and structure

- `SKILL.md` defines the setup flow and user choices.
- `references/setup-and-runtime.md` defines heartbeat scheduling, local state, and handoff verification.
- `scripts/check_compactions.py` reads local Codex session logs and computes the configured thresholds.
- `scripts/package.sh` creates the versioned installable ZIP; runtime setup uses the connected Codex automation and thread tools.

## Install

Extract `dist/codex-conversation-sentinel-<version>.zip` and copy its `codex-conversation-sentinel` folder into your Codex skills directory, usually `~/.codex/skills/`, then reload Codex. Invoke `$codex-conversation-sentinel` to choose or accept the recommended schedule, thresholds, title prefixes, and successor behavior. The skill creates the paired heartbeat tasks in the connected Codex after setup.

## Recommended defaults

- Slow: 00:00–11:30, hourly at :30; 11:30 is the final slow scan.
- Fast: after 11:30 through midnight, every 10 minutes; midnight is the final fast scan.
- Handoff when any threshold is reached: 3 actual compactions, 20 task turns, or 50 decimal MB of transcript log.
- Titles: `OLD·` for the source and `NEW·` for its successor.

The connected Codex app must provide its automation and thread tools. The monitoring script reads local Codex session logs to calculate exact counts.

If the local goal-status database cannot be read, the checker fails closed and suppresses automatic handoff for that scan; the heartbeat should report this limitation instead of treating the status as safe.
