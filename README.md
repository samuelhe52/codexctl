# codexctl

`codexctl` is a small command-line client for the local Codex app-server daemon. It starts, monitors, steers, queues, and stops Codex sessions. It also ships a Claude Code subagent, `codex-runner`, so Claude Code can hand work to Codex models.

It talks to the daemon's own JSON-RPC protocol and has no dependencies beyond Python 3.9+. The protocol version comes from your installed Codex CLI, not from this repository, so new models, reasoning efforts, and speed tiers work without an update here.

## Requirements

- Codex CLI with the app-server daemon (tested with `codex-cli 0.158.0` on macOS)
- A running daemon: `codex app-server daemon start`
- Python 3.9 or later

## Install

```sh
git clone https://github.com/samuelhe52/codexctl.git
cd codexctl
./install.sh
```

The installer symlinks `codexctl` into `~/.local/bin` and `agents/codex-runner.md` into `~/.claude/agents`. Set `CODEXCTL_BIN_DIR` or `CLAUDE_AGENTS_DIR` to change those locations. Restart Claude Code to load the agent.

## Usage

Each command prints one JSON object. Errors print `{"error": "..."}` and exit with status 1.

```sh
codexctl models                                   # models, efforts, service tiers
codexctl start "Review the diff in this repo" --model gpt-6-sol --effort high --tier priority --name review
codexctl start "Run the eval" --model gpt-6-luna --wait 3600   # block, then print a compact result
codexctl wait <threadId>                          # block until the active turn ends; compact result
codexctl watch <threadId> --seconds 120           # live events until the turn ends or time runs out
codexctl status <threadId>                        # thread state and latest persisted items
codexctl result <threadId>                        # full latest agent message
codexctl steer <threadId> "Focus on the loss code"
codexctl queue <threadId> "Now write the fix"     # waits for the running turn, then starts a new one
codexctl stop <threadId> --kill-processes
codexctl list
codexctl archive <threadId>
codexctl review --base main --model gpt-6-sol --effort high   # built-in review; waits and prints it
```

`review` runs Codex's built-in code reviewer in a new session. The target is uncommitted changes by default, or pass one of `--base BRANCH`, `--commit SHA`, or `--instructions TEXT`. It accepts the same model, effort, tier, cwd, name, sandbox, and `-c` options as `start`, but its sandbox defaults to `read-only`. Unlike `start`, it waits for the review and prints the review text, up to `--wait` seconds (default 3600). The daemon interrupts a review as soon as its client disconnects, so run long reviews in the background instead of killing `codexctl`. Under `read-only`, the reviewer can't run tools that need a writable temp directory, such as most test suites. Pass `--sandbox workspace-write` if the review should run tests.

`wait` blocks until the thread's active turn ends, then prints a compact result: turn status, the final agent message, the command count, the number of failed commands with the last five of them (output clipped), and the changed files. If no turn is active, it returns that result for the last turn at once. `start --wait SECONDS` does the same right after starting. Unlike `review`, a turn keeps running if `codexctl` exits or times out, so it's safe to run `wait` again.

### Cheap supervision from Claude Code

Waiting doesn't need a model. Run `codexctl start ... --wait 7200` (or `codexctl wait <threadId>`) with Bash `run_in_background`, and Claude Code is notified when it exits. No tokens are spent while Codex works, and only the compact result comes back. Use the `codex-runner` subagent, or `watch` and `steer` directly, only when a run needs mid-course attention.

`start` options:

| Option | Meaning |
| --- | --- |
| `--model` | Model id from `codexctl models` |
| `--effort` | Reasoning effort supported by the model |
| `--tier` | `priority` for Fast mode, `default` for standard speed |
| `--sandbox` | `read-only`, `workspace-write`, or `danger-full-access` (default) |
| `--cwd` | Working directory for the session (default: current directory) |
| `--name` | Thread name shown in Codex history |
| `-c KEY=VALUE` | Codex config override; dotted keys work, and `VALUE` is parsed as JSON, falling back to a plain string |
| `--wait SECONDS` | Block until the turn ends, up to SECONDS, and print the compact result |
| `-` as prompt | Read the prompt from stdin |

Sessions run on the shared daemon, so they keep running after `codexctl` exits. They also appear in `codex agents` and in Codex history.

## Behavior to know

- **Full access by default.** `start` uses the `danger-full-access` sandbox with approvals set to `never`, so Codex can change files, git state, and remote hosts without asking. Pass `--sandbox read-only` for review-only work.
- **Interrupting doesn't end commands.** `stop` interrupts the turn, but processes Codex already started keep running. `stop --kill-processes` ends them by archiving and then unarchiving the thread. The daemon ends a thread's processes when it is archived, and the thread stays usable afterwards.
- **In-progress items.** `status` reads persisted history, which may not include items from a turn that is still running. Use `watch` for live progress.
- **Idle threads unload.** The daemon unloads idle threads. On reload it keeps model, sandbox, effort, and tier, but drops `-c` overrides. `codexctl` saves each thread's overrides in `~/.local/state/codexctl/threads.json` and reapplies them when it reloads the thread. Other clients, such as Codex Desktop, don't reapply them. Put overrides that must always apply in a Codex profile or `config.toml`.
- **Server requests go unanswered.** With approvals set to `never`, the daemon rarely asks clients anything. If it does, for example a user-input request, `watch` reports it as a `serverRequest` event and `codexctl` doesn't answer it.
- **Socket path.** The default is `~/.codex/app-server-control/app-server-control.sock`. Override it with `CODEXCTL_SOCK`.

## Protocol reference

To see the exact protocol your Codex version speaks:

```sh
codex app-server generate-json-schema --out /tmp/codex-schema
```

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests run against a fake daemon and don't need Codex installed.

## License

MIT
