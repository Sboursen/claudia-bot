# 96 · Local relay queue

The relay stores messages, replies and proposed jobs for local review. Setup saves the engine and model preferences in the relay configuration. Automatic production turns, external job dispatch and wake requests are disabled pending an isolated execution broker.

## Messages and job records

Initialize the queue once, then use the inbox and outbox to review the saved records.

```sh
slop --json relay setup --engine codex
slop --json relay send -- "Review the narration"
slop --json relay inbox
slop --json relay say "The narration draft is ready"
slop --json relay read
slop --json relay status
```

Put queue options before `--` and put the message after it. Invoke the relay with an argument list when integrating with another app. Pass message text as a single argument after `--`. Stage attachments beneath the configured projects directory and supply their paths with `--attach` before `--`. Staged file intake and job destinations require regular files or directories with no symbolic links and an operating system that supports protected path traversal.

Use `relay job` to record a proposed job. Open jobs and their recorded inputs appear in `relay read`. The `claim` and `copy` commands return an error for browser, Midjourney, Suno and Grok Imagine jobs, including jobs with an old claim token. `relay done`, `relay fail` and `relay cancel` let the operator record a result or close a job locally.

Keep the relay state directory and staged projects under a trusted local account. The queue stores message text and attachment paths in local files. Review historic attachments and job records as untrusted data before opening files or visiting listed URLs.

## Moving from an earlier installation

If an earlier relay supervisor is still running, stop it with `slop relay stop` before queuing messages. `slop relay wake` reports whether legacy wake state exists. Use `slop relay wake off` to clear the stored destination. Remove any previous routine at its provider separately.

Setup no longer installs skills, registers MCP servers, writes production briefs or edits vendor and shell configuration. Existing installations may still contain those registrations and files. Disable the prior agent registrations and remove legacy skills and generated briefs from agent discovery before using the local queue with an agent.

The archived Grok profile and recipe, the `claudia-setup`, `claudia-relay` and `claudia-toolkit` skills, and the production brief describe an earlier automatic workflow. Keep them out of active agent discovery. The documented local relay interface replaces their launch, job claim, wake and chat command instructions.
