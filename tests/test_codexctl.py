import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import codexctl  # noqa: E402


def server_frame(opcode, payload, fin=True):
    n = len(payload)
    hdr = bytes([(0x80 if fin else 0) | opcode])
    if n < 126:
        hdr += bytes([n])
    elif n < 65536:
        hdr += bytes([126]) + struct.pack(">H", n)
    else:
        hdr += bytes([127]) + struct.pack(">Q", n)
    return hdr + payload


class FakeDaemon:
    """Minimal app-server stand-in: WebSocket over a unix socket, scripted JSON-RPC replies."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.lock = threading.Lock()
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "d.sock")
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(self.path)
        self.srv.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        f = conn.makefile("rb")
        while f.readline() not in (b"\r\n", b""):
            pass
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        while True:
            head = f.read(2)
            if len(head) < 2:
                return
            op, n = head[0] & 0x0F, head[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", f.read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", f.read(8))[0]
            mask = f.read(4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(f.read(n)))
            if op != 0x1:
                continue
            msg = json.loads(payload)
            self.requests.append(msg)
            if "id" in msg:
                for chunk in self.handler(msg):
                    if isinstance(chunk, tuple):
                        delay, data, on_sent = chunk
                        threading.Timer(delay, self._send_later, (conn, data, on_sent)).start()
                    else:
                        with self.lock:
                            conn.sendall(chunk)

    def _send_later(self, conn, data, on_sent):
        on_sent()
        with self.lock:
            conn.sendall(data)

    def close(self):
        self.srv.close()


def reply(msg, result):
    return server_frame(0x1, json.dumps({"id": msg["id"], "result": result}).encode())


class FramingTest(unittest.TestCase):
    def run_cli(self, daemon, *argv):
        os.environ["CODEXCTL_SOCK"] = daemon.path
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                codexctl.main(list(argv))
            except SystemExit:
                pass
        return json.loads(buf.getvalue())

    def test_fragmented_large_message_with_ping_and_interleaved_notification(self):
        big = [{"id": f"m{i}", "isDefault": i == 0, "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                "defaultReasoningEffort": "low", "serviceTiers": [{"id": "priority", "description": "x" * 900}]}
               for i in range(80)]

        def handler(msg):
            if msg["method"] == "model/list":
                body = json.dumps({"id": msg["id"], "result": {"data": big}}).encode()
                self.assertGreater(len(body), 65535)
                half = len(body) // 2
                yield server_frame(0x1, json.dumps({"method": "thread/status/changed", "params": {}}).encode())
                yield server_frame(0x1, body[:half], fin=False)
                yield server_frame(0x9, b"ping")
                yield server_frame(0x0, body[half:])
            else:
                yield reply(msg, {})

        d = FakeDaemon(handler)
        out = self.run_cli(d, "models")
        d.close()
        self.assertEqual(len(out["models"]), 80)
        self.assertEqual(out["models"][0]["serviceTiers"]["priority"], "x" * 900)

    def test_rpc_error_becomes_json_error(self):
        def handler(msg):
            if msg["method"] == "thread/read":
                yield server_frame(0x1, json.dumps({"id": msg["id"], "error": {"message": "thread not loaded"}}).encode())
            else:
                yield reply(msg, {})

        d = FakeDaemon(handler)
        out = self.run_cli(d, "status", "t1")
        d.close()
        self.assertIn("thread not loaded", out["error"])


class OverridePersistenceTest(unittest.TestCase):
    def test_start_overrides_are_reapplied_when_queue_reloads_thread(self):
        state = {"loaded": True}

        def handler(msg):
            m = msg["method"]
            if m == "thread/start":
                yield reply(msg, {"thread": {"id": "t1"}, "model": "m", "sandbox": {"type": "dangerFullAccess"}})
            elif m == "thread/resume":
                yield reply(msg, {"sandbox": {"type": "dangerFullAccess"}})
            elif m == "turn/start":
                yield reply(msg, {"turn": {"id": "u1"}})
            elif m == "thread/read":
                yield reply(msg, {"thread": {"status": {"type": "idle" if state["loaded"] else "notLoaded"}}})
            elif m == "thread/turns/list":
                yield reply(msg, {"data": []})
            else:
                yield reply(msg, {})

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_STATE_HOME"] = tmp
            d = FakeDaemon(handler)
            t = FramingTest()
            t.run_cli(d, "start", "hi", "-c", 'a.b="x"', "-c", "n=3")
            state["loaded"] = False
            t.run_cli(d, "queue", "t1", "again")
            d.close()
            resumes = [r for r in d.requests if r.get("method") == "thread/resume"]
            self.assertEqual(resumes[0]["params"]["config"], {"a.b": "x", "n": 3})
            start = next(r for r in d.requests if r.get("method") == "thread/start")
            self.assertEqual(start["params"]["sandbox"], "danger-full-access")
            self.assertEqual(start["params"]["approvalPolicy"], "never")
            self.assertEqual(resumes[0]["params"]["sandbox"], "danger-full-access")
            self.assertEqual(resumes[0]["params"]["approvalPolicy"], "never")
            turns = [r["params"] for r in d.requests if r.get("method") == "turn/start"]
            for tp in turns:
                self.assertEqual(tp["sandboxPolicy"], {"type": "dangerFullAccess"})
                self.assertEqual(tp["approvalPolicy"], "never")


class PermissionPinningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_STATE_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def daemon(self, resume_sandbox, loaded=False):
        def handler(msg):
            m = msg["method"]
            if m == "thread/start":
                yield reply(msg, {"thread": {"id": "t1"}, "sandbox": {"type": "workspaceWrite", "networkAccess": True}})
            elif m == "thread/resume":
                yield reply(msg, {"sandbox": resume_sandbox})
            elif m == "turn/start":
                yield reply(msg, {"turn": {"id": "u1"}})
            elif m == "thread/read":
                yield reply(msg, {"thread": {"status": {"type": "idle" if loaded else "notLoaded"}}})
            elif m == "thread/turns/list":
                yield reply(msg, {"data": []})
            else:
                yield reply(msg, {})
        return FakeDaemon(handler)

    def test_resume_with_wrong_sandbox_fails_closed(self):
        d = self.daemon({"type": "readOnly"})
        FramingTest().run_cli(d, "start", "hi", "--sandbox", "workspace-write")
        out = FramingTest().run_cli(d, "queue", "t1", "again")
        d.close()
        self.assertIn("expected 'workspace-write'", out["error"])
        self.assertEqual(sum(r.get("method") == "turn/start" for r in d.requests), 1)

    def test_queue_resends_exact_policy_from_start(self):
        d = self.daemon({"type": "workspaceWrite"}, loaded=True)
        FramingTest().run_cli(d, "start", "hi", "--sandbox", "workspace-write")
        out = FramingTest().run_cli(d, "queue", "t1", "again")
        d.close()
        self.assertEqual(out["sandbox"], "workspace-write")
        tp = [r["params"] for r in d.requests if r.get("method") == "turn/start"][-1]
        self.assertEqual(tp["sandboxPolicy"], {"type": "workspaceWrite", "networkAccess": True})

    def test_untracked_thread_is_flagged_and_can_be_pinned(self):
        d = self.daemon({"type": "dangerFullAccess"})
        out = FramingTest().run_cli(d, "queue", "other", "go")
        self.assertIn("no recorded sandbox", out["note"])
        tp = [r["params"] for r in d.requests if r.get("method") == "turn/start"][-1]
        self.assertNotIn("sandboxPolicy", tp)
        out = FramingTest().run_cli(d, "queue", "other", "go", "--sandbox", "danger-full-access")
        d.close()
        self.assertNotIn("note", out)
        resume = [r["params"] for r in d.requests if r.get("method") == "thread/resume"][-1]
        self.assertEqual(resume["sandbox"], "danger-full-access")
        tp = [r["params"] for r in d.requests if r.get("method") == "turn/start"][-1]
        self.assertEqual(tp["sandboxPolicy"], {"type": "dangerFullAccess"})


class ReviewTest(unittest.TestCase):
    def test_review_stays_connected_until_its_turn_completes(self):
        done = threading.Event()

        def notify(method, params):
            return server_frame(0x1, json.dumps({"method": method, "params": params}).encode())

        def handler(msg):
            m = msg["method"]
            if m == "thread/start":
                yield reply(msg, {"thread": {"id": "t1"}, "model": "m"})
            elif m == "review/start":
                yield reply(msg, {"reviewThreadId": "t1", "turn": {"id": "r1"}})
                yield notify("turn/completed", {"threadId": "t1", "turn": {"id": "inner", "status": "completed"}})
                yield (0.3, notify("turn/completed", {"threadId": "t1", "turn": {"id": "r1", "status": "completed"}}),
                       lambda: done.set())
            elif m == "thread/turns/list":
                items = [{"type": "exitedReviewMode"}, {"type": "agentMessage", "text": "no findings"}]
                yield reply(msg, {"data": [{"id": "r1", "status": "completed" if done.is_set() else "inProgress",
                                            "items": items if done.is_set() else []}]})
            else:
                yield reply(msg, {})

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_STATE_HOME"] = tmp
            d = FakeDaemon(handler)
            out = FramingTest().run_cli(d, "review", "--base", "main", "--effort", "high", "--wait", "5")
            d.close()
        self.assertTrue(out["finished"])
        self.assertEqual(out["review"], "no findings")
        start = next(r for r in d.requests if r.get("method") == "thread/start")
        self.assertEqual(start["params"]["sandbox"], "read-only")
        self.assertEqual(start["params"]["config"], {"model_reasoning_effort": "high"})
        review = next(r for r in d.requests if r.get("method") == "review/start")
        self.assertEqual(review["params"]["target"], {"type": "baseBranch", "branch": "main"})


class WaitTest(unittest.TestCase):
    def notify(self, method, params):
        return server_frame(0x1, json.dumps({"method": method, "params": params}).encode())

    def daemon(self, done, active_on_wait=False, complete=True):
        items = [{"type": "commandExecution", "command": "ls", "exitCode": 0, "aggregatedOutput": "a"},
                 {"type": "commandExecution", "command": "pytest", "exitCode": 1, "aggregatedOutput": "x" * 5000},
                 {"type": "fileChange", "status": "completed", "changes": [{"path": "b.py"}, {"path": "a.py"}]},
                 {"type": "agentMessage", "text": "interim"}, {"type": "agentMessage", "text": "done: 1 failure"}]

        def handler(msg):
            m = msg["method"]
            if m == "thread/start":
                yield reply(msg, {"thread": {"id": "t1"}, "model": "m"})
            elif m in ("turn/start", "thread/resume"):
                if m == "turn/start":
                    yield reply(msg, {"turn": {"id": "u1"}})
                else:
                    yield reply(msg, {})
                if complete and (m == "turn/start" or active_on_wait):
                    yield (0.2, self.notify("turn/completed", {"threadId": "t1", "turn": {"id": "u1", "status": "completed"}}),
                           lambda: done.set())
            elif m == "thread/turns/list":
                finished = done.is_set() or not active_on_wait
                yield reply(msg, {"data": [{"id": "u1", "status": "completed" if finished else "inProgress",
                                            "items": items if finished else []}]})
            else:
                yield reply(msg, {})
        return FakeDaemon(handler)

    def run(self, result=None):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_STATE_HOME"] = tmp
            return super().run(result)

    def check_summary(self, out):
        self.assertTrue(out["finished"])
        self.assertEqual(out["turnStatus"], "completed")
        self.assertEqual(out["text"], "done: 1 failure")
        self.assertEqual(out["commands"], 2)
        self.assertEqual([f["command"] for f in out["failedCommands"]], ["pytest"])
        self.assertEqual(out["failedCount"], 1)
        self.assertEqual(len(out["failedCommands"][0]["output"]), 500)
        self.assertEqual(out["filesChanged"], ["a.py", "b.py"])

    def test_start_wait_returns_compact_result_when_turn_ends(self):
        done = threading.Event()
        d = self.daemon(done)
        out = FramingTest().run_cli(d, "start", "go", "--wait", "5")
        d.close()
        self.assertEqual(out["threadId"], "t1")
        self.check_summary(out)

    def test_wait_on_active_turn_blocks_until_it_completes(self):
        done = threading.Event()
        d = self.daemon(done, active_on_wait=True)
        out = FramingTest().run_cli(d, "wait", "t1", "--seconds", "5")
        d.close()
        self.assertTrue(done.is_set())
        self.check_summary(out)

    def test_wait_without_active_turn_returns_last_result(self):
        d = self.daemon(threading.Event())
        out = FramingTest().run_cli(d, "wait", "t1")
        d.close()
        self.check_summary(out)

    def test_wait_timeout_reports_turn_still_running(self):
        d = self.daemon(threading.Event(), active_on_wait=True, complete=False)
        out = FramingTest().run_cli(d, "wait", "t1", "--seconds", "1")
        d.close()
        self.assertFalse(out["finished"])
        self.assertEqual(out["turnId"], "u1")


if __name__ == "__main__":
    unittest.main()
