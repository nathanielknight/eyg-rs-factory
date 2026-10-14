import hashlib
import shutil
import tempfile
from pathlib import Path

from django.test import TestCase

from runner.models import Invocation
from runner.runner import IncusRunner, instance_name, new_run_id
from runner.types import ImageSource, Input, Output, RunSpec, SnapshotSource

from .fake import Clock, Command, FakeIncus


class RunnerTestCase(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.clock = Clock()
        self.incus = FakeIncus(self.clock)
        self.runner = IncusRunner(
            self.incus,
            self.tmp / "runs",
            boot_timeout_s=120,
            now=self.clock.now,
            sleep=self.clock.sleep,
            monotonic=self.clock.monotonic,
        )
        self.input_file = self.tmp / "repo.tar"
        self.input_file.write_bytes(b"tarball bytes")

    def spec(self, **kwargs) -> RunSpec:
        defaults = dict(
            name="ci attempt",
            source=SnapshotSource("eyg-golden", "base"),
            profiles=["default", "eyg-ci"],
            inputs=[
                Input(
                    self.input_file,
                    "/home/ci/repo.tar",
                    uid=1001,
                    gid=1001,
                    unpack=True,
                )
            ],
            argv=["make", "test"],
            env={"HOME": "/home/ci"},
            uid=1001,
            gid=1001,
            cwd="/home/ci",
            timeout_s=600,
            outputs=[
                Output("/home/ci/out/results.json", max_bytes=1024, required=True)
            ],
            max_log_bytes=1000,
        )
        return RunSpec(**{**defaults, **kwargs})

    def go(self, **kwargs):
        run_id = self.runner.create(self.spec(**kwargs))
        return run_id, self.runner.run(run_id)


class LifecycleTests(RunnerTestCase):
    def test_create_records_pending_without_touching_incus(self):
        run_id = self.runner.create(self.spec())
        inv = Invocation.objects.get(pk=run_id)
        self.assertEqual(inv.status, "pending")
        self.assertEqual(
            inv.source,
            {"type": "snapshot", "instance": "eyg-golden", "snapshot": "base"},
        )
        self.assertEqual(self.incus.calls, [])
        self.assertEqual(self.runner.status(run_id).status, "pending")

    def test_completed_run(self):
        self.incus.command = Command(
            exit_code=3,
            stdout=b"ran\n",
            stderr=b"warn\n",
            files={"/home/ci/out/results.json": b'{"pass": 1}'},
        )
        run_id, result = self.go()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.exit_code, 3)  # non-zero is the caller's business
        self.assertIsNone(result.error)
        self.assertEqual(result.instance_name, f"run-{run_id}")
        self.assertEqual(result.image_fingerprint, "abc123")
        self.assertEqual(result.stdout_path.read_bytes(), b"ran\n")
        self.assertEqual(result.stderr_path.read_bytes(), b"warn\n")
        self.assertFalse(result.stdout_truncated)
        artifact = result.artifacts["/home/ci/out/results.json"]
        self.assertEqual(artifact.name, "0")  # host-side name, not the guest's
        self.assertEqual(artifact.read_bytes(), b'{"pass": 1}')
        self.assertTrue((self.tmp / "runs" / run_id / "incus-op.json").exists())
        self.assertIsNotNone(result.command_started_at)
        self.assertIsNotNone(result.ended_at)

        # Command ran as the requested user; input pushed with ownership, then
        # unpacked as root; the instance is gone.
        self.assertIn(
            ("exec", result.instance_name, ("make", "test"), 1001, 1001, "/home/ci"),
            self.incus.calls,
        )
        self.assertIn(
            ("push", result.instance_name, "/home/ci/repo.tar", 0o644, 1001, 1001),
            self.incus.calls,
        )
        self.assertIn(
            (
                "exec",
                result.instance_name,
                ("tar", "-xf", "/home/ci/repo.tar", "-C", "/home/ci"),
                0,
                0,
                "/",
            ),
            self.incus.calls,
        )
        self.assertEqual(self.incus.instances, {})

        inv = Invocation.objects.get(pk=run_id)
        self.assertEqual(inv.inputs[0]["size"], len(b"tarball bytes"))
        self.assertEqual(
            inv.inputs[0]["sha256"], hashlib.sha256(b"tarball bytes").hexdigest()
        )
        self.assertEqual(
            inv.outputs[0]["sha256"], hashlib.sha256(b'{"pass": 1}').hexdigest()
        )

    def test_image_source(self):
        _, result = self.go(source=ImageSource("deadbeef"), inputs=[], outputs=[])
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.incus.calls[0][2], ImageSource("deadbeef"))

    def test_logs_are_capped(self):
        self.incus.command = Command(
            stdout=b"x" * 50, files={"/home/ci/out/results.json": b"{}"}
        )
        _, result = self.go(max_log_bytes=10)
        self.assertEqual(result.stdout_path.read_bytes(), b"x" * 10)
        self.assertTrue(result.stdout_truncated)
        self.assertFalse(result.stderr_truncated)

    def test_run_is_reentrant_once_finished(self):
        run_id, first = self.go(outputs=[])
        calls = len(self.incus.calls)
        self.assertEqual(self.runner.run(run_id), first)
        self.assertEqual(len(self.incus.calls), calls)

    def test_new_run_ids_are_uuid7_and_ordered(self):
        a, b = new_run_id(), new_run_id()
        self.assertEqual(a[14], "7")
        self.assertIn(a[19], "89ab")
        self.assertLessEqual(a[:13], b[:13])


class FailureTests(RunnerTestCase):
    def test_boot_timeout_is_infra_error(self):
        self.incus.boot_failures = 10_000
        _, result = self.go()
        self.assertEqual(result.status, "infra_error")
        self.assertIsNone(result.exit_code)
        self.assertIn("guest agent not ready", result.error)
        self.assertEqual(result.stdout_path.read_bytes(), b"")  # real, empty file
        self.assertEqual(self.incus.instances, {})  # torn down anyway

    def test_slow_boot_recovers(self):
        self.incus.boot_failures = 3
        self.incus.command = Command(files={"/home/ci/out/results.json": b"{}"})
        _, result = self.go()
        self.assertEqual(result.status, "completed")

    def test_create_failure_is_infra_error(self):
        self.incus.fail_create = True
        _, result = self.go()
        self.assertEqual(result.status, "infra_error")
        self.assertIn("create failed", result.error)

    def test_push_failure_is_infra_error(self):
        self.incus.fail_push = True
        _, result = self.go()
        self.assertEqual(result.status, "infra_error")
        self.assertEqual(self.incus.instances, {})

    def test_timeout_force_stops_and_keeps_partial_logs(self):
        self.incus.command = Command(hang=True, stdout=b"partial")
        self.incus.exec_metadata_has_output = False  # exercise the log lookup
        run_id, result = self.go(timeout_s=60)
        self.assertEqual(result.status, "timeout")
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.stdout_path.read_bytes(), b"partial")
        self.assertEqual(result.artifacts, {})
        self.assertIn(("stop", f"run-{run_id}"), self.incus.calls)
        self.assertEqual(self.incus.instances, {})

    def test_timeout_with_log_paths_in_metadata(self):
        self.incus.command = Command(hang=True, stdout=b"partial")
        _, result = self.go(timeout_s=60)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.stdout_path.read_bytes(), b"partial")

    def test_instance_dying_is_infra_error_not_exit_minus_one(self):
        self.incus.command = Command(vm_dies=True, stdout=b"partial")
        _, result = self.go()
        self.assertEqual(result.status, "infra_error")
        self.assertIsNone(result.exit_code)
        self.assertIn("instance Stopped", result.error)
        self.assertEqual(result.stdout_path.read_bytes(), b"partial")

    def test_signal_death_with_instance_running_is_completed(self):
        self.incus.command = Command(exit_code=-1)
        _, result = self.go(outputs=[])
        self.assertEqual((result.status, result.exit_code), ("completed", -1))

    def test_missing_required_output_is_artifact_error(self):
        self.incus.command = Command(exit_code=0)
        _, result = self.go()
        self.assertEqual(result.status, "artifact_error")
        self.assertEqual(result.exit_code, 0)  # kept for diagnosis
        self.assertIn("/home/ci/out/results.json: not found", result.error)

    def test_missing_optional_output_is_recorded_not_fatal(self):
        _, result = self.go(outputs=[Output("/opt/maybe", max_bytes=10)])
        self.assertEqual(result.status, "completed")
        inv = Invocation.objects.get(pk=result.run_id)
        self.assertEqual(inv.outputs[0]["missing_reason"], "not found")

    def test_oversize_output_is_artifact_error(self):
        self.incus.command = Command(files={"/home/ci/out/results.json": b"x" * 2000})
        _, result = self.go()
        self.assertEqual(result.status, "artifact_error")
        self.assertFalse(
            (self.tmp / "runs" / result.run_id / "artifacts" / "0").exists()
        )

    def test_keep_on_failure_leaves_instance_stopped(self):
        _, result = self.go(keep_on_failure=True)  # required output missing
        self.assertEqual(result.status, "artifact_error")
        self.assertEqual(
            self.incus.instances[result.instance_name]["status"], "Stopped"
        )

    def test_keep_on_failure_still_deletes_on_success(self):
        self.incus.command = Command(files={"/home/ci/out/results.json": b"{}"})
        _, result = self.go(keep_on_failure=True)
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.incus.instances, {})


class RecoveryTests(RunnerTestCase):
    def test_reattaches_to_running_command(self):
        """A crash after exec leaves a running row; run() waits on the stored op."""
        self.incus.command = Command(
            exit_code=7, stdout=b"done", files={"/home/ci/out/results.json": b"{}"}
        )
        run_id = self.runner.create(self.spec())
        name = instance_name(run_id)
        self.incus.create_instance(name, None, [])
        op_id = self.incus.exec(name, ["make", "test"])
        Invocation.objects.filter(pk=run_id).update(
            status="running",
            instance_name=name,
            operation_id=op_id,
            started_at=self.clock.now(),
            command_started_at=self.clock.now(),
        )
        result = self.runner.run(run_id)
        self.assertEqual((result.status, result.exit_code), ("completed", 7))
        self.assertEqual(result.stdout_path.read_bytes(), b"done")
        self.assertEqual(sum(1 for c in self.incus.calls if c[0] == "exec"), 1)

    def test_reattach_after_deadline_is_timeout(self):
        self.incus.command = Command(hang=True)
        run_id = self.runner.create(self.spec(timeout_s=60))
        name = instance_name(run_id)
        self.incus.create_instance(name, None, [])
        op_id = self.incus.exec(name, ["make", "test"])
        Invocation.objects.filter(pk=run_id).update(
            status="running",
            instance_name=name,
            operation_id=op_id,
            started_at=self.clock.now(),
            command_started_at=self.clock.now(),
        )
        self.clock.sleep(3600)
        result = self.runner.run(run_id)
        self.assertEqual(result.status, "timeout")

    def test_pending_with_leftover_instance_starts_over(self):
        run_id = self.runner.create(self.spec(outputs=[]))
        name = instance_name(run_id)
        self.incus.create_instance(name, None, [])
        Invocation.objects.filter(pk=run_id).update(instance_name=name)
        result = self.runner.run(run_id)
        self.assertEqual(result.status, "completed")
        self.assertIn(("delete", name), self.incus.calls)


class CancelAndSweepTests(RunnerTestCase):
    def test_cancel_before_run(self):
        run_id = self.runner.create(self.spec())
        self.runner.cancel(run_id)
        result = self.runner.run(run_id)
        self.assertEqual(result.status, "cancelled")
        self.assertIsNone(result.started_at)
        self.assertTrue(result.stderr_path.is_file())
        self.assertEqual(self.incus.calls, [])

    def test_cancel_during_command(self):
        self.incus.command = Command(hang=True)
        run_id = self.runner.create(self.spec())
        # Cancel from "another process" as soon as the command is waited on.
        wait = self.incus.wait

        def wait_then_cancel(op_id, timeout_s):
            if Invocation.objects.get(pk=run_id).status == "running":
                self.runner.cancel(run_id)
            return wait(op_id, timeout_s)

        self.incus.command = Command(hang=True, stdout=b"partial")
        self.incus.wait = wait_then_cancel
        result = self.runner.run(run_id)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.error, "cancelled")
        self.assertEqual(result.stdout_path.read_bytes(), b"partial")
        self.assertIsNone(result.exit_code)
        self.assertEqual(self.incus.instances, {})

    def test_cancel_finished_run_is_noop(self):
        run_id, _ = self.go(outputs=[])
        self.runner.cancel(run_id)
        self.assertEqual(self.runner.status(run_id).status, "completed")

    def test_sweep(self):
        live = self.runner.create(self.spec())
        kept, _ = self.go(keep_on_failure=True)  # artifact_error, kept
        finished = self.runner.create(self.spec())
        Invocation.objects.filter(pk=finished).update(status="completed")
        for run_id in (live, finished, "unknown"):
            self.incus.create_instance(instance_name(run_id), None, [])
        self.incus.create_instance("eyg-golden", None, [])

        swept = self.runner.sweep()

        self.assertEqual(
            sorted(swept), sorted([instance_name(finished), "run-unknown"])
        )
        self.assertEqual(
            set(self.incus.instances),
            {instance_name(live), instance_name(kept), "eyg-golden"},
        )


class SpecValidationTests(RunnerTestCase):
    def test_relative_guest_paths_rejected(self):
        with self.assertRaises(ValueError):
            self.spec(cwd="work")
        with self.assertRaises(ValueError):
            self.spec(outputs=[Output("out.json", 10)])

    def test_missing_input_file_rejected_at_create(self):
        with self.assertRaises(FileNotFoundError):
            self.runner.create(self.spec(inputs=[Input(self.tmp / "nope", "/x")]))
        self.assertEqual(Invocation.objects.count(), 0)
