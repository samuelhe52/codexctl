#!/usr/bin/env python3
"""Start, monitor, steer, queue, and stop Codex sessions on the local Codex app-server daemon.

Talks JSON-RPC over WebSocket to the shared daemon socket. Standard library only.
Every command prints one JSON object on stdout; failures print {"error": ...} and exit 1.
"""
import argparse
import base64
import json
import os
import socket
import struct
import sys
import time

DEFAULT_SOCK = "~/.codex/app-server-control/app-server-control.sock"
SANDBOXES = ["read-only", "workspace-write", "danger-full-access"]


class CodexError(Exception):
    pass


class Client:
    def __init__(self, sock_path=None, timeout=60):
        path = os.path.expanduser(sock_path or os.environ.get("CODEXCTL_SOCK") or DEFAULT_SOCK)
        self.s = socket.socket(socket.AF_UNIX)
        self.s.settimeout(timeout)
        try:
            self.s.connect(path)
        except OSError as e:
            raise CodexError(f"cannot reach Codex daemon at {path} ({e}); "
                             "start it with `codex app-server daemon start`") from e
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                        f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise CodexError("daemon closed the connection during the WebSocket handshake")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise CodexError(f"WebSocket upgrade failed: {head[:200]!r}")
        self.next_id = 1
        self.pending = []
        self.call("initialize", {"clientInfo": {"name": "codexctl", "version": "1"}})
        self.send({"method": "initialized"})

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise CodexError("daemon closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.s.sendall(hdr + mask + masked)

    def send(self, obj):
        self._frame(0x1, json.dumps(obj).encode())

    def read_message(self):
        data = b""
        while True:
            b0, b1 = self._recv_exact(2)
            fin, op, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            if b1 & 0x80:
                mask = self._recv_exact(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._recv_exact(n)))
            else:
                payload = self._recv_exact(n)
            if op == 0x8:
                raise CodexError("daemon closed the connection")
            if op == 0x9:
                self._frame(0xA, payload)
                continue
            if op == 0xA:
                continue
            data += payload
            if fin:
                return json.loads(data.decode())

    def call(self, method, params=None):
        rid = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        while True:
            m = self.read_message()
            if m.get("id") == rid and "method" not in m:
                if "error" in m:
                    raise CodexError(f"{method}: {m['error'].get('message', m['error'])}")
                return m.get("result")
            self.pending.append(m)


def clip(v, limit):
    return v[-limit:] if isinstance(v, str) and len(v) > limit else v


def summarize_item(item, limit):
    t = item.get("type")
    out = {"type": t}
    if t in ("agentMessage", "userMessage"):
        text = item.get("text")
        if text is None:
            text = " ".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
        out["text"] = clip(text, limit)
        return out
    if t == "fileChange":
        out["status"] = item.get("status")
        out["paths"] = [c.get("path") for c in item.get("changes") or []]
        return out
    for k in ("command", "status", "exitCode", "durationMs", "aggregatedOutput",
              "server", "tool", "query", "error"):
        if item.get(k) is not None:
            out[k] = clip(item[k], limit)
    return out


def read_thread(c, thread_id):
    return c.call("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]


def latest_turns(c, thread_id, n, items=True):
    r = c.call("thread/turns/list", {"threadId": thread_id, "limit": n,
                                    "itemsView": "full" if items else "notLoaded"})
    return list(reversed(r["data"]))


def active_turn_id(c, thread_id):
    turns = latest_turns(c, thread_id, 1, items=False)
    if turns and turns[-1].get("status") == "inProgress":
        return turns[-1]["id"]
    return None


def state_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "codexctl", "threads.json")


def load_state():
    try:
        with open(state_path()) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_overrides(thread_id, overrides):
    state = load_state()
    state[thread_id] = {"config": overrides}
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def resume(c, thread_id):
    # The daemon keeps model/sandbox/effort/tier across reloads but not `config` overrides.
    params = {"threadId": thread_id, "excludeTurns": True}
    saved = load_state().get(thread_id, {}).get("config")
    if saved:
        params["config"] = saved
    c.call("thread/resume", params)


def ensure_loaded(c, thread_id):
    if read_thread(c, thread_id).get("status", {}).get("type") == "notLoaded":
        resume(c, thread_id)


def text_input(text):
    return [{"type": "text", "text": text}]


def parse_value(v):
    try:
        return json.loads(v)
    except ValueError:
        return v


def turn_overrides(a):
    tp = {}
    if a.model:
        tp["model"] = a.model
    if a.effort:
        tp["effort"] = a.effort
    if a.tier:
        tp["serviceTier"] = a.tier
    return tp


def new_thread(c, a, extra_config=None):
    params = {"approvalPolicy": "never", "sandbox": a.sandbox, "cwd": os.path.abspath(a.cwd)}
    if a.model:
        params["model"] = a.model
    if a.tier:
        params["serviceTier"] = a.tier
    overrides = dict(extra_config or {})
    for kv in a.config or []:
        if "=" not in kv:
            raise CodexError(f"-c expects key=value, got {kv!r}")
        k, v = kv.split("=", 1)
        overrides[k] = parse_value(v)
    if overrides:
        params["config"] = overrides
    r = c.call("thread/start", params)
    tid = r["thread"]["id"]
    if overrides:
        save_overrides(tid, overrides)
    if a.name:
        c.call("thread/name/set", {"threadId": tid, "name": a.name})
    return r, tid


def thread_info(r, tid, effort):
    return {"threadId": tid, "model": r.get("model"), "effort": effort or r.get("reasoningEffort"),
            "serviceTier": r.get("serviceTier"), "sandbox": r.get("sandbox"),
            "approvalPolicy": r.get("approvalPolicy"), "cwd": r.get("cwd")}


def cmd_review(a):
    if a.base:
        target = {"type": "baseBranch", "branch": a.base}
    elif a.commit:
        target = {"type": "commit", "sha": a.commit}
    elif a.instructions:
        target = {"type": "custom", "instructions": a.instructions}
    else:
        target = {"type": "uncommittedChanges"}
    c = Client()
    # review/start has no effort parameter, so effort is set on the thread's config.
    r, tid = new_thread(c, a, {"model_reasoning_effort": a.effort} if a.effort else None)
    rr = c.call("review/start", {"threadId": tid, "target": target})
    turn_id = rr["turn"]["id"]
    # The daemon interrupts an inline review as soon as its client disconnects, so stay connected.
    events, done = stream(c, tid, a.wait, 400, turn_id)
    out = {**thread_info(r, tid, a.effort), "turnId": turn_id, "target": target, "finished": done}
    if not done:
        out["note"] = f"review did not finish within {a.wait}s and was interrupted when codexctl exited"
        return out
    turn = latest_turns(c, tid, 1)[-1]
    msgs = [i.get("text") for i in turn.get("items") or [] if i.get("type") == "agentMessage"]
    errors = [e for e in events if e["event"] in ("error", "serverRequest")]
    return {**out, "turnStatus": turn.get("status"), "review": msgs[-1] if msgs else None,
            **({"errors": errors} if errors else {})}


def cmd_start(a):
    c = Client()
    r, tid = new_thread(c, a)
    prompt = sys.stdin.read() if a.prompt == "-" else a.prompt
    tp = {"threadId": tid, "input": text_input(prompt), **turn_overrides(a)}
    if a.summary:
        tp["summary"] = a.summary
    tr = c.call("turn/start", tp)
    return {**thread_info(r, tid, a.effort), "turnId": tr["turn"]["id"]}


def cmd_status(a):
    c = Client()
    th = read_thread(c, a.thread)
    turns = latest_turns(c, a.thread, a.turns)
    items = [(t["id"], i) for t in turns for i in t.get("items") or []][-a.last:]
    last = turns[-1] if turns else {}
    return {"threadId": a.thread, "name": th.get("name"), "threadStatus": th.get("status"),
            "model": th.get("model"), "cwd": th.get("cwd"),
            "activeTurnId": last.get("id") if last.get("status") == "inProgress" else None,
            "lastTurn": {k: last.get(k) for k in ("id", "status", "error", "durationMs")} if last else None,
            "recent": [dict(turn=tid, **summarize_item(i, a.chars)) for tid, i in items],
            "note": "items of an in-progress turn may not appear until it ends; use `watch` for live events"}


def cmd_result(a):
    c = Client()
    for t in reversed(latest_turns(c, a.thread, 5)):
        msgs = [i for i in t.get("items") or [] if i.get("type") == "agentMessage"]
        if msgs:
            return {"threadId": a.thread, "turnId": t["id"], "turnStatus": t.get("status"),
                    "text": msgs[-1].get("text")}
    return {"threadId": a.thread, "turnId": None, "text": None}


WATCH_SKIP = {"item/agentMessage/delta", "item/reasoning/summaryTextDelta", "item/reasoning/textDelta",
              "item/reasoning/summaryPartAdded", "item/commandExecution/outputDelta", "item/plan/delta",
              "item/fileChange/outputDelta", "process/outputDelta", "command/exec/outputDelta",
              "thread/tokenUsage/updated", "account/rateLimits/updated", "turn/diff/updated",
              "item/mcpToolCall/progress", "model/safetyBuffering/updated"}


def event_of(m, thread_id, limit):
    method, p = m.get("method"), m.get("params") or {}
    if p.get("threadId") not in (None, thread_id):
        return None
    if "id" in m:
        return {"event": "serverRequest", "method": method,
                "note": "the daemon is waiting on a client for this; codexctl does not answer it"}
    if method in WATCH_SKIP:
        return None
    if method in ("item/started", "item/completed"):
        item = p.get("item") or {}
        if item.get("type") in ("reasoning", "userMessage") and method == "item/started":
            return None
        ev = summarize_item(item, limit)
        if method == "item/started":
            ev = {k: v for k, v in ev.items() if k in ("type", "command", "server", "tool", "query", "paths")}
        return {"event": method.split("/")[1], **ev}
    if method == "turn/completed":
        t = p.get("turn") or {}
        return {"event": "turnCompleted", "turnId": t.get("id"), "status": t.get("status"), "error": t.get("error")}
    if method == "turn/started":
        return {"event": "turnStarted", "turnId": (p.get("turn") or {}).get("id")}
    if method in ("error", "warning", "thread/status/changed", "model/rerouted", "thread/compacted"):
        return {"event": method, **{k: clip(v, limit) for k, v in p.items() if k != "threadId"}}
    return None


def stream(c, thread_id, seconds, chars, turn_id=None):
    """Collect events until the (given) turn completes or `seconds` pass."""
    deadline = time.time() + seconds
    backlog, c.pending = c.pending, []
    events = []
    while True:
        if backlog:
            m = backlog.pop(0)
        else:
            remaining = deadline - time.time()
            if remaining <= 0:
                return events, False
            c.s.settimeout(remaining)
            try:
                m = c.read_message()
            except (socket.timeout, TimeoutError):
                return events, False
        ev = event_of(m, thread_id, chars)
        if ev:
            events.append(ev)
            if ev["event"] == "turnCompleted" and turn_id in (None, ev["turnId"]):
                return events, True


def cmd_watch(a):
    c = Client()
    th = read_thread(c, a.thread)
    resume(c, a.thread)
    active = active_turn_id(c, a.thread)
    if active is None and not a.wait_start:
        return {"threadId": a.thread, "activeTurnId": None, "finished": True, "events": [],
                "note": "no active turn; see `status` or `result`"}
    events, done = stream(c, a.thread, a.seconds, a.chars)
    return {"threadId": a.thread, "name": th.get("name"), "finished": done,
            "activeTurnId": None if done else active_turn_id(c, a.thread),
            "droppedEvents": max(0, len(events) - a.max_events), "events": events[-a.max_events:]}


def cmd_steer(a):
    c = Client()
    active = active_turn_id(c, a.thread)
    if not active:
        raise CodexError("no active turn to steer; use `queue` to start a new turn")
    c.call("turn/steer", {"threadId": a.thread, "expectedTurnId": active, "input": text_input(a.message)})
    return {"steered": True, "turnId": active}


def cmd_queue(a):
    c = Client()
    ensure_loaded(c, a.thread)
    deadline = time.time() + a.wait
    while active_turn_id(c, a.thread):
        if time.time() > deadline:
            raise CodexError(f"a turn is still active after {a.wait}s; use `steer`, `stop`, or retry")
        time.sleep(5)
    tr = c.call("turn/start", {"threadId": a.thread, "input": text_input(a.message), **turn_overrides(a)})
    return {"queued": True, "turnId": tr["turn"]["id"]}


def cmd_stop(a):
    c = Client()
    active = active_turn_id(c, a.thread)
    if active:
        c.call("turn/interrupt", {"threadId": a.thread, "turnId": active})
    out = {"interrupted": bool(active), "turnId": active}
    if a.kill_processes:
        c.call("thread/archive", {"threadId": a.thread})
        c.call("thread/unarchive", {"threadId": a.thread})
        out["processesKilled"] = True
    else:
        out["note"] = "commands the turn started may still be running; pass --kill-processes to end them"
    return out


def cmd_archive(a):
    c = Client()
    c.call("thread/archive", {"threadId": a.thread})
    return {"archived": True, "threadId": a.thread}


def cmd_list(a):
    c = Client()
    r = c.call("thread/list", {"limit": a.limit})
    keep = ("id", "name", "status", "model", "cwd", "updatedAt", "preview")
    return {"threads": [{k: clip(t.get(k), 200) for k in keep} for t in r.get("data", [])]}


def cmd_models(_a):
    c = Client()
    out = []
    for m in c.call("model/list", {}).get("data", []):
        out.append({"id": m.get("id"), "default": m.get("isDefault"),
                    "efforts": [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or []],
                    "defaultEffort": m.get("defaultReasoningEffort"),
                    "serviceTiers": {t.get("id"): t.get("description") for t in m.get("serviceTiers") or []}})
    return {"models": out, "note": "serviceTier id `priority` is Fast mode; `default` is standard speed"}


def build_parser():
    p = argparse.ArgumentParser(prog="codexctl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_, description=help_)
        s.set_defaults(fn=fn)
        return s

    def add_turn_flags(s):
        s.add_argument("--model", help="model id, see `models`")
        s.add_argument("--effort", help="reasoning effort, see `models`")
        s.add_argument("--tier", help="service tier: `priority` (Fast) or `default`")

    def add_thread_flags(s, sandbox):
        add_turn_flags(s)
        s.add_argument("--cwd", default=os.getcwd(), help="working directory (default: current)")
        s.add_argument("--name", help="thread name shown in Codex history")
        s.add_argument("--sandbox", default=sandbox, choices=SANDBOXES,
                       help=f"default: {sandbox} (approvals are always `never`)")
        s.add_argument("-c", "--config", action="append", metavar="KEY=VALUE",
                       help="Codex config override, dotted keys allowed; VALUE is parsed as JSON, else string")

    s = add("start", cmd_start, "start a new session and its first turn; returns immediately")
    s.add_argument("prompt", help="prompt text, or - to read it from stdin")
    add_thread_flags(s, "danger-full-access")
    s.add_argument("--summary", help="reasoning summary mode, e.g. auto, concise, detailed, none")

    s = add("review", cmd_review,
            "run Codex's built-in code review in a new session and wait for the result "
            "(default target: uncommitted changes); the review is interrupted if codexctl exits early")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--base", metavar="BRANCH", help="review the current branch against BRANCH")
    g.add_argument("--commit", metavar="SHA", help="review the changes introduced by one commit")
    g.add_argument("--instructions", help="custom review instructions")
    s.add_argument("--wait", type=int, default=3600, help="max seconds to wait for the review (default 3600)")
    add_thread_flags(s, "read-only")

    s = add("status", cmd_status, "thread state and the latest persisted items")
    s.add_argument("thread")
    s.add_argument("--last", type=int, default=8, help="number of recent items (default 8)")
    s.add_argument("--turns", type=int, default=2, help="number of recent turns to scan (default 2)")
    s.add_argument("--chars", type=int, default=1500, help="max chars per text field (keeps the tail)")

    s = add("watch", cmd_watch, "stream live events of the active turn for up to --seconds")
    s.add_argument("thread")
    s.add_argument("--seconds", type=float, default=60)
    s.add_argument("--max-events", type=int, default=40)
    s.add_argument("--chars", type=int, default=800)
    s.add_argument("--wait-start", action="store_true", help="keep listening even if no turn is active yet")

    s = add("result", cmd_result, "full text of the latest agent message")
    s.add_argument("thread")

    s = add("steer", cmd_steer, "add guidance to the running turn")
    s.add_argument("thread")
    s.add_argument("message")

    s = add("queue", cmd_queue, "wait for the active turn to end, then start a new turn (blocks)")
    s.add_argument("thread")
    s.add_argument("message")
    s.add_argument("--wait", type=int, default=3600, help="max seconds to wait (default 3600)")
    add_turn_flags(s)

    s = add("stop", cmd_stop, "interrupt the active turn")
    s.add_argument("thread")
    s.add_argument("--kill-processes", action="store_true",
                   help="also end commands the session started, by archiving and unarchiving the thread")

    s = add("archive", cmd_archive, "archive a thread and end its processes")
    s.add_argument("thread")

    s = add("list", cmd_list, "recent threads")
    s.add_argument("--limit", type=int, default=10)

    add("models", cmd_models, "available models, reasoning efforts, and service tiers")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        out = a.fn(a)
    except (CodexError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
