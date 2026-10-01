# Telegram GPT Reviewed Operations

## Scope

The bridge starts separate Codex CLI tasks, not messages in this ChatGPT thread.
On 2026-10-02 KST the owner additionally authorized code modification, deployment
and order execution. The model itself remains tool-disabled. A trusted runner
validates structured replacements and performs explicitly confirmed operations.
There is no free-form shell execution, real-money access or model-selected order.

The host rejects unprivileged namespace creation; OS restrictions are unchanged.
For tests only, the trusted runner uses sudo/bubblewrap to prepare read-only
mounts and separate PID/network/IPC/UTS namespaces, then setpriv drops to ubuntu
with no supplementary groups, no capabilities and no-new-privileges. Tests see
only a Git source snapshot, sanitized config and read-only dependencies. They
cannot see the runtime DB, .env, Codex login, Git token or Telegram/KIS keys.
Tests use a 512MB temporary filesystem, a 600-second timeout and the worker's
memory/process limits. No model command is interpolated into these arguments.
Model tools are disabled and a read-only minimal
permission profile denies access to the host filesystem and command network.
The CLI runs in an empty temporary directory, ignores user config/rules, does
not load project instructions or MCP servers, and receives only explicit input.
Existing ChatGPT authentication is used by the CLI, not sent in its prompt.

## Commands

- `/gpt <request>` creates a proposed job; it does not run immediately.
- `/gpt_edit <relative-path[,path]> <request>` proposes source editing plus isolated tests.
- `/gpt_diff <edit-id> [page]` shows the exact proposed diff.
- `/gpt_deploy <edit-id>` proposes deployment of the tested diff; confirm this NEW job ID.
- `/gpt_order KR|US BUY|SELL SYMBOL QTY LIMIT [NASD|NYSE|AMEX]` proposes an explicit paper order.
- `/gpt_confirm <id>` starts a job within 10 minutes; orders must START within 60 seconds of proposal.
- `/gpt_status [id]` shows the latest/selected job and worker heartbeat.
- `/gpt_result <id> [page]` retrieves the stored result in bounded pages.
- `/gpt_cancel <id>` cancels pending work; no cancellation after the irreversible execution boundary.
- `/gpt_help` explains the scope and usage.

Only the configured positive private-chat ID, with the same sender user ID,
is accepted. Bot senders, forwarded messages, group chats and stale messages
are rejected. Ordinary text and the existing `/lab_*` commands do not start GPT
jobs. Telegram update IDs are unique in the durable queue. Limits are six new
requests per hour, three outstanding jobs and one running worker.

Example workflow (each reply supplies its actual job number):

```
/gpt_edit src/kinvest_trade/indicators.py Explain and fix a demonstrated calculation error; preserve risk limits
/gpt_confirm 3
/gpt_diff 3
/gpt_deploy 3
/gpt_confirm 4
```

Edits are restricted to allowlisted strategy/indicator/market-analysis modules,
the two market policy JSONs, and selected tests. See `EDITABLE` in
`gpt_operations.py`. At most five existing files/180KB and twenty exact unique
replacements are supported. Credential/config/bridge/order/risk infrastructure,
new imports, new dynamic evaluation/file-open calls and policy `risk` changes
require manual engineering review. These checks reduce accidental scope drift;
they are not a proof that arbitrary code is safe. Review the diff before deployment.

Deployment requires full isolated pytest success, an artifact no older than one
hour, matching manifest hash/base commit, clean master and unchanged pinned
GitHub origin/master. It creates an exact commit with a WORKLOG entry, pushes
without force using git_token.txt through askpass, stops the trading service,
backs up the DB, fast-forwards and verifies the running commit. Failure attempts
a revert commit only when the checkout is still exactly our clean commit; it
never resets user changes or restores an old DB. Failures/rollback evidence stay
in the per-job folder. In-session KR acceptance locks deployment until 16:00 KST;
preopen policy identity changes require a separate acceptance rearm. Unchanged
preopen policy/account reservations have their deployment commit refreshed.

Manual orders are VPS-only, regular-session-only, limit-only and one POST.
Caps: 1-10 shares and KRW100,000 or USD500 order notional, quote deviation <=0.5%.
No short selling or manual scale-in. Existing account loss/rejection halts,
sellable balance, buying power, outstanding orders and account identity are
rechecked. Explicit manual orders do not require an automatic strategy signal;
they are labeled MANUAL_GPT/is_session_trade=0, not strategy-profit evidence.
The trading controller stops briefly to avoid concurrent automated orders, then
restarts with the existing execution reconciliation tracking the submission.
Submission does not mean a fill. Use /lab_orders for confirmed execution status.
On ambiguous POST outcomes, an fsynced marker blocks retries and automatic
trading; the controller returns paused. An engineer must reconcile broker
history and the ledger before clearing the marker. /gpt_cancel is NOT a broker
order cancellation command. This implementation does not submit an unsolicited
test order merely because order capability was enabled.

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
sharing disabled, analysis sends only the owner's submitted text. Edit jobs also
send the explicitly named source files. Local operations require the separately
approved `reviewed-code-deploy-paper-orders-v1` marker and per-capability flags.
No new credentials or raw broker responses are exported to Codex.

Snapshots are bounded and may contain historical-account records. They are not
a replacement for fresh broker reconciliation. Missing market volume remains
unknown. Prior GPT results include their job kind; analyses and edits are still
proposals until the separate deploy/order audit proves execution.

## Operation

Local settings: `state/gpt_bridge/settings.json` (not committed).
Durable queue/results/usage: `state/gpt_bridge/jobs.sqlite3`.
Per-job redacted snapshot and structured output: `state/gpt_bridge/jobs/<id>/`.
Worker: `systemd/kinvest-gpt-worker.service`, independent of the trading service.
No second Telegram update poller is started. The trading controller only queues
requests; the worker performs model calls and sends results.

Worker restarts mark abandoned running jobs `interrupted`, never automatically
repeat orders or deployments. Model calls have a maximum 900-second window;
verification is an additional bounded 600 seconds. A running verifier may finish
after cancellation but cannot authorize deployment of a cancelled job. Results persist if notification
fails; delivery retries up to five times and manual retrieval remains available.
Delivery is at-least-once: an ambiguous Telegram response can cause a duplicate.
Usage records report tokens returned by the CLI, not remaining account credits
or a proof that a more expensive model was more accurate.

Official references:
- https://learn.chatgpt.com/docs/developer-commands
- https://learn.chatgpt.com/docs/auth
- https://learn.chatgpt.com/docs/permissions
