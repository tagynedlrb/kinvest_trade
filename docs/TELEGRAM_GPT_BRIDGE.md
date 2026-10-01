# Telegram GPT Analysis Bridge

## Scope

The bridge starts a separate Codex CLI analysis task. It does not inject messages
into the current ChatGPT conversation. It can analyze approved snapshots and
write proposed changes and test plans, but cannot execute commands, modify
production code, place orders, commit, push, or deploy. These are deliberate
boundaries, not capabilities implied by a successful analysis result.

The host rejected unprivileged bubblewrap namespace creation. That restriction
was not removed or bypassed. Model tools are disabled and a read-only minimal
permission profile denies access to the host filesystem and command network.
The CLI runs in an empty temporary directory, ignores user config/rules, does
not load project instructions or MCP servers, and receives only explicit input.
Existing ChatGPT authentication is used by the CLI, not sent in its prompt.

## Commands

- `/gpt <request>` creates a proposed job; it does not run immediately.
- `/gpt_confirm <id>` starts the job if confirmed within 10 minutes.
- `/gpt_status [id]` shows the latest/selected job and worker heartbeat.
- `/gpt_result <id> [page]` retrieves the stored result in bounded pages.
- `/gpt_cancel <id>` cancels queued work or requests process-group termination.
- `/gpt_help` explains the scope and usage.

Only the configured positive private-chat ID, with the same sender user ID,
is accepted. Bot senders, forwarded messages, group chats and stale messages
are rejected. Ordinary text and the existing `/lab_*` commands do not start GPT
jobs. Telegram update IDs are unique in the durable queue. Limits are six new
requests per hour, three outstanding jobs and one running worker.

## Consent and Data

The user explicitly approved `trade-market-policy-worklog-v1` in this chat on
2026-10-02 KST after the initial automatic export was blocked by security review.
This permits transmission to OpenAI Codex of recent order/fill summaries, market
indicators, daily performance, policy settings/source excerpts and work-history
excerpts, and replies to the existing private Telegram chat.

Account numbers, KIS keys, Git/Telegram tokens and raw broker payloads are
excluded. Queries project allowlisted columns; configured secret values, account
formats and token-like strings are redacted. A scope marker in local settings is
required before export. Without it, context export fails closed. With context
sharing disabled, only the owner's submitted text is supplied.

Snapshots are bounded and may contain historical-account records. They are not
a replacement for fresh broker reconciliation. Missing market volume remains
unknown. Prior GPT results are explicitly labeled proposals, not completed work.

## Operation

Local settings: `state/gpt_bridge/settings.json` (not committed).
Durable queue/results/usage: `state/gpt_bridge/jobs.sqlite3`.
Per-job redacted snapshot and structured output: `state/gpt_bridge/jobs/<id>/`.
Worker: `systemd/kinvest-gpt-worker.service`, independent of the trading service.
No second Telegram update poller is started. The trading controller only queues
requests; the worker performs model calls and sends results.

Worker restarts mark abandoned running jobs `interrupted`, never automatically
repeat them. Jobs have a maximum 900-second execution window. Whole process
groups are terminated on cancellation/timeouts. Results persist if notification
fails; delivery retries up to five times and manual retrieval remains available.
Delivery is at-least-once: an ambiguous Telegram response can cause a duplicate.
Usage records report tokens returned by the CLI, not remaining account credits
or a proof that a more expensive model was more accurate.

Official references:
- https://learn.chatgpt.com/docs/developer-commands
- https://learn.chatgpt.com/docs/auth
- https://learn.chatgpt.com/docs/permissions
