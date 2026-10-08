---
name: codexctl
description: Start, inspect, steer, and stop Codex sessions through the local codexctl CLI, or run its built-in code review. Use when the user asks to delegate work to Codex or manage codexctl threads.
---

# codexctl

Use `codexctl` to operate sessions on the local Codex app-server daemon. Each command returns one JSON object; command failures return an `error` and exit 1. `watch` collects live events and returns them in that object rather than printing a continuous stream.

## Prepare the task

- Check `codexctl --help` and the relevant subcommand's `--help` when options are uncertain. The CLI requires Python 3.9+ and a running Codex app-server daemon. If the daemon is unavailable, report the connection error; `codex app-server daemon start` starts it when needed for the requested work. `CODEXCTL_SOCK` overrides the default socket.
- Preserve the user's model, effort, tier, cwd, sandbox, and configuration choices. Use `codexctl models` to resolve model IDs and supported efforts instead of hardcoding a preferred model. Omit unspecified model, effort, and tier flags to use defaults.
- Set `--cwd` to the intended repository. Put the user's constraints at the top of the delegated prompt, including edit boundaries and restrictions on commits, pushes, PRs, publishing, and remote actions. Delegation does not grant additional authorization.
- `start` defaults to `danger-full-access` with approvals set to `never`. Choose a sandbox appropriate to the authorized work: `read-only` for inspection, `workspace-write` for repository edits or tests. Never widen an explicitly requested sandbox to work around a failure.
- Only steer, queue work on, stop, or archive a thread the user identified or that you started for this task. Use `list` to resolve an ambiguous target before changing it.

## Start and collect results

For a substantial prompt, use stdin with a quoted heredoc so shell substitutions cannot alter it:

```sh
codexctl start - --cwd /path/to/repo --sandbox workspace-write --name scoped-fix <<'PROMPT'
Constraints: Only edit the requested files. Do not commit, push, or modify remote state.
Task: Implement the requested fix and run the relevant checks. Report changed files and validation results.
PROMPT
```

Save the returned `threadId`. `start` returns immediately; add `--wait SECONDS` to wait and receive a compact result instead. Use the host's background execution facility for long waits so the supervising conversation remains responsive.

```sh
codexctl wait THREAD_ID --seconds 60
codexctl result THREAD_ID
```

`wait` returns the last turn's result immediately if no turn is active. Its compact result includes `finished`, `turnStatus`, final message `text`, command count `commands`, `failedCount`, the last five `failedCommands` with clipped output, and `filesChanged`.

If `finished` is false after `start --wait` or `wait`, the turn keeps running. Wait again on that thread; do not start a duplicate task. A finished turn can have failed or been interrupted, so inspect `turnStatus` and errors before claiming completion. Report the thread ID, actual outcome, relevant validation, and any remaining work.

## Inspect and guide a session

| Intent | Command | Behavior |
| --- | --- | --- |
| Locate sessions | `codexctl list --limit 10` | Returns thread IDs, names, cwd, and status. |
| Read saved progress | `codexctl status THREAD_ID` | Persisted history may omit in-progress items. |
| See live progress | `codexctl watch THREAD_ID --seconds 30` | Collects events until completion or timeout; use for mid-course attention. |
| Read the latest reply | `codexctl result THREAD_ID` | Returns the latest agent message; this alone does not prove the turn ended. |
| Guide active work | `codexctl steer THREAD_ID "Focus on the failing check"` | Requires an active turn. |
| Start a follow-up | `codexctl queue THREAD_ID "Run the remaining checks" --wait 60` | Waits for the active turn to end, then starts a new one; it does not wait for the new turn to finish. |

Prefer `wait` for completion instead of repeated progress polling. A `queue` timeout means the existing turn was still active; inspect that thread before retrying. If a command fails ambiguously after submission, inspect the known thread or list sessions before resubmitting work.

`watch` can report a `serverRequest`: the daemon is waiting for a client response that `codexctl` cannot supply. Report the blocked request instead of assuming more waiting will resolve it.

## Built-in review

```sh
codexctl review --cwd /path/to/repo --base main --wait 3600
```

The default target is uncommitted changes. Choose at most one of `--base BRANCH`, `--commit SHA`, or `--instructions TEXT`. Review accepts the same model, effort, tier, cwd, name, sandbox, and `-c KEY=VALUE` options as `start`.

Review defaults to `read-only`; use `workspace-write` when authorized tests need writable temporary files. Run long reviews through background execution and keep the command alive: unlike ordinary turns, a review is interrupted when its client disconnects, including when `--wait` expires. Do not describe a timed-out review as continuing in the background. A completed review returns its text in `review`.

## Stop and configuration behavior

- `codexctl stop THREAD_ID` interrupts the turn, but commands it started may keep running.
- `codexctl stop THREAD_ID --kill-processes` archives and unarchives the thread to end its daemon-managed processes while keeping the thread usable. Use when stopping those processes is within the user's request; do not infer that detached or remote jobs have also ended.
- `codexctl archive THREAD_ID` archives the thread and ends its daemon-managed processes. Treat it as a lifecycle change, not harmless history cleanup.
- Repeated `-c KEY=VALUE` options support dotted keys; values are parsed as JSON, falling back to strings. `codexctl` stores overrides in `~/.local/state/codexctl/threads.json` and reapplies them when it reloads idle threads. Other clients do not reapply that state; use a Codex profile or `config.toml` for settings that must persist across clients, only when changing those settings is in scope.
