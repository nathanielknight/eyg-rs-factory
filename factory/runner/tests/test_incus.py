import json
import shutil
import tempfile
from pathlib import Path

import httpx
from django.test import SimpleTestCase

from runner.incus import IncusClient, IncusError, NotFound, OutputRejected
from runner.types import ImageSource, SnapshotSource


def op_body(op_id="op1", status_code=200, metadata=None, err=""):
    return {
        "id": op_id,
        "status_code": status_code,
        "err": err,
        "metadata": metadata or {},
    }


def async_response(op_id="op1"):
    return httpx.Response(
        202,
        json={
            "type": "async",
            "operation": f"/1.0/operations/{op_id}",
            "metadata": op_body(op_id, 103),
        },
    )


class ClientTestCase(SimpleTestCase):
    def setUp(self):
        self.requests: list[httpx.Request] = []
        self.handler = lambda request: httpx.Response(500)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

        def handle(request):
            self.requests.append(request)
            return self.handler(request)

        http = httpx.Client(
            transport=httpx.MockTransport(handle), base_url="http://incus"
        )
        self.client = IncusClient(http, project="factory")


class RequestShapeTests(ClientTestCase):
    def test_create_from_snapshot(self):
        self.handler = lambda r: async_response("abc")
        op = self.client.create_instance(
            "run-1", SnapshotSource("golden", "snap0"), ["default", "eyg-ci"]
        )
        self.assertEqual(op, "abc")
        [req] = self.requests
        self.assertEqual((req.method, req.url.path), ("POST", "/1.0/instances"))
        self.assertEqual(req.url.params["project"], "factory")
        self.assertEqual(
            json.loads(req.content),
            {
                "name": "run-1",
                "type": "virtual-machine",
                "start": True,
                "profiles": ["default", "eyg-ci"],
                "source": {"type": "copy", "source": "golden/snap0"},
            },
        )

    def test_create_from_image(self):
        self.handler = lambda r: async_response()
        self.client.create_instance("run-1", ImageSource("f00d"), [])
        self.assertEqual(
            json.loads(self.requests[0].content)["source"],
            {"type": "image", "fingerprint": "f00d"},
        )

    def test_exec_is_detached_and_recorded(self):
        self.handler = lambda r: async_response()
        self.client.exec(
            "run-1", ["make"], env={"A": "b"}, uid=1001, gid=1002, cwd="/w"
        )
        body = json.loads(self.requests[0].content)
        self.assertEqual(
            body,
            {
                "command": ["make"],
                "environment": {"A": "b"},
                "user": 1001,
                "group": 1002,
                "cwd": "/w",
                "interactive": False,
                "wait-for-websocket": False,
                "record-output": True,
            },
        )

    def test_push_file_headers(self):
        self.handler = lambda r: httpx.Response(
            200, json={"type": "sync", "metadata": {}}
        )
        self.client.push_file(
            "run-1", "/home/ci/x", iter([b"ab", b"c"]), mode=0o600, uid=5, gid=6
        )
        [req] = self.requests
        self.assertEqual(req.url.params["path"], "/home/ci/x")
        self.assertEqual(req.headers["X-Incus-mode"], "0600")
        self.assertEqual(req.headers["X-Incus-uid"], "5")
        self.assertEqual(req.headers["X-Incus-gid"], "6")
        self.assertEqual(req.headers["X-Incus-type"], "file")
        self.assertEqual(req.headers["X-Incus-write"], "overwrite")
        self.assertEqual(req.read(), b"abc")

    def test_error_response_raises(self):
        self.handler = lambda r: httpx.Response(
            404,
            json={"type": "error", "error": "Instance not found", "error_code": 404},
        )
        with self.assertRaisesRegex(NotFound, "Instance not found"):
            self.client.get_instance("nope")

    def test_list_instance_names(self):
        self.handler = lambda r: httpx.Response(
            200,
            json={
                "type": "sync",
                "metadata": [
                    "/1.0/instances/run-1?project=factory",
                    "/1.0/instances/eyg-golden",
                ],
            },
        )
        self.assertEqual(self.client.list_instance_names(), ["run-1", "eyg-golden"])
        self.assertNotIn("recursion", self.requests[0].url.params)

    def test_transport_error_raises_incus_error(self):
        def boom(r):
            raise httpx.ConnectError("socket gone")

        self.handler = boom
        with self.assertRaises(IncusError):
            self.client.list_instance_names()


class WaitTests(ClientTestCase):
    def test_wait_returns_operation(self):
        self.handler = lambda r: httpx.Response(
            200, json={"type": "sync", "metadata": op_body(metadata={"return": 2})}
        )
        op = self.client.wait("op1", 30)
        self.assertTrue(op.succeeded)
        self.assertEqual(op.metadata["return"], 2)
        self.assertEqual(self.requests[0].url.params["timeout"], "30")

    def test_wait_rounds_fractions_up(self):
        self.handler = lambda r: httpx.Response(
            200, json={"type": "sync", "metadata": op_body(status_code=103)}
        )
        self.client.wait("op1", 0.4)
        self.assertEqual(self.requests[0].url.params["timeout"], "1")

    def test_wait_on_forgotten_operation_raises_not_found(self):
        self.handler = lambda r: httpx.Response(
            404, json={"type": "error", "error": "Operation not found"}
        )
        with self.assertRaises(NotFound):
            self.client.wait("op1", 5)

    def test_wait_error_falls_back_to_get(self):
        def handler(r):
            if r.url.path.endswith("/wait"):
                return httpx.Response(504, json={"type": "error", "error": "timeout"})
            return httpx.Response(
                200, json={"type": "sync", "metadata": op_body(status_code=103)}
            )

        self.handler = handler
        op = self.client.wait("op1", 5)
        self.assertFalse(op.finished)

    def test_wait_ok_raises_on_failed_operation(self):
        self.handler = lambda r: httpx.Response(
            200, json={"type": "sync", "metadata": op_body(status_code=400, err="boom")}
        )
        with self.assertRaisesRegex(IncusError, "boom"):
            self.client.wait_ok("op1", 5, "launch")


class TransferTests(ClientTestCase):
    def file_response(self, body: bytes, kind="file"):
        return lambda r: httpx.Response(
            200, content=body, headers={"X-Incus-type": kind}
        )

    def test_pull_file(self):
        self.handler = self.file_response(b"hello")
        size, sha = self.client.pull_file("run-1", "/out", self.tmp / "a", 10)
        self.assertEqual(size, 5)
        self.assertEqual((self.tmp / "a").read_bytes(), b"hello")
        self.assertEqual(len(sha), 64)

    def test_pull_rejects_directory_and_symlink(self):
        for kind in ("directory", "symlink"):
            self.handler = self.file_response(b"", kind)
            with self.assertRaisesRegex(OutputRejected, "not a regular file"):
                self.client.pull_file("run-1", "/out", self.tmp / "a", 10)
            self.assertFalse((self.tmp / "a").exists())

    def test_pull_rejects_oversize(self):
        self.handler = self.file_response(b"x" * 11)
        with self.assertRaisesRegex(OutputRejected, "max_bytes"):
            self.client.pull_file("run-1", "/out", self.tmp / "a", 10)
        self.assertFalse((self.tmp / "a").exists())

    def test_pull_missing(self):
        self.handler = lambda r: httpx.Response(
            404, json={"type": "error", "error": "not found"}
        )
        with self.assertRaisesRegex(OutputRejected, "not found"):
            self.client.pull_file("run-1", "/out", self.tmp / "a", 10)

    def test_download_log_caps_and_reports_truncation(self):
        self.handler = lambda r: httpx.Response(200, content=b"0123456789")
        path = "/1.0/instances/run-1/logs/exec-output/exec_op1.stdout?project=factory"
        self.assertTrue(self.client.download_log(path, self.tmp / "log", 4))
        self.assertEqual((self.tmp / "log").read_bytes(), b"0123")
        self.assertEqual(
            self.requests[0].url.path,
            "/1.0/instances/run-1/logs/exec-output/exec_op1.stdout",
        )
        self.assertFalse(self.client.download_log(path, self.tmp / "log2", 100))
