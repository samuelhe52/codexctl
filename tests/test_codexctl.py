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
                yield reply(msg, {"thread": {"id": "t1"}, "model": "m"})
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


if __name__ == "__main__":
    unittest.main()
