import hashlib
import itertools
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.db import IntegrityError
from django.test import TestCase

from runner.models import Invocation
from runner.runner import (
    EXIT_FILE,
    WRAPPER,
    ClaimLost,
    IncusRunner,
    instance_name,
    new_run_id,
)
from runner.types import (
    ImageSource,
    Input,
    Output,
    RunInProgress,
    RunSpec,
    SnapshotSource,
)

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

        # Command ran wrapped, as the requested user, with a state dir it owns;
        # input pushed with ownership, then unpacked as root; the instance is
        # gone.
        self.assertIn(
            (
                "exec",
                result.instance_name,
                (*WRAPPER, "make", "test"),
                1001,
                1001,
                "/home/ci",
            ),
            self.incus.calls,
        )
        self.assertIn(
            (
                "exec",
                result.instance_name,
                ("install", "-d", "-o", "1001", "-g", "1001", "/run/eyg-runner"),
                0,
                0,
                "/",
            ),
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
    def crashed_while_running(self, **spec) -> tuple[str, str, str]:
        "A row left running by a worker that died after starting the command."
        run_id = self.runner.create(self.spec(**spec))
        name = instance_name(run_id)
        self.incus.create_instance(name, None, [])
        op_id = self.incus.exec(name, [*WRAPPER, "make", "test"])
        Invocation.objects.filter(pk=run_id).update(
            status="running",
            instance_name=name,
            operation_id=op_id,
            started_at=self.clock.now(),
            command_started_at=self.clock.now(),
        )
        return run_id, name, op_id

    def test_reattaches_to_running_command(self):
        """A crash after exec leaves a running row; run() waits on the stored op."""
        self.incus.command = Command(
            exit_code=7, stdout=b"done", files={"/home/ci/out/results.json": b"{}"}
        )
        run_id, _, _ = self.crashed_while_running()
        result = self.runner.run(run_id)
        self.assertEqual((result.status, result.exit_code), ("completed", 7))
        self.assertEqual(result.stdout_path.read_bytes(), b"done")
        self.assertEqual(sum(1 for c in self.incus.calls if c[0] == "exec"), 1)

    def test_reattach_after_deadline_is_timeout(self):
        self.incus.command = Command(hang=True)
        run_id, _, _ = self.crashed_while_running(timeout_s=60)
        self.clock.sleep(3600)
        result = self.runner.run(run_id)
        self.assertEqual(result.status, "timeout")

    def test_forgotten_op_resumes_from_exit_file(self):
        """Incus dropped the finished op; the wrapper's exit file has the code."""
        self.incus.command = Command(
            exit_code=7, stdout=b"done", files={"/home/ci/out/results.json": b"{}"}
        )
        run_id, _, op_id = self.crashed_while_running()
        self.incus.forget(op_id)
        result = self.runner.run(run_id)
        self.assertEqual((result.status, result.exit_code), ("completed", 7))
        self.assertEqual(result.stdout_path.read_bytes(), b"done")  # found by op ID
        self.assertIn("/home/ci/out/results.json", result.artifacts)
        self.assertFalse((self.tmp / "runs" / run_id / "incus-op.json").exists())
        self.assertEqual(self.incus.instances, {})

    def test_forgotten_op_follows_live_command_until_it_exits(self):
        self.incus.command = Command(hang=True, stdout=b"partial")
        run_id, name, op_id = self.crashed_while_running(outputs=[])
        self.incus.forget(op_id)

        def sleep(s):
            self.clock.sleep(s)
            self.incus.finish_hung_command(name, 4)

        self.runner.sleep = sleep
        result = self.runner.run(run_id)
        self.assertEqual((result.status, result.exit_code), ("completed", 4))
        self.assertEqual(result.stdout_path.read_bytes(), b"partial")

    def test_forgotten_op_with_live_command_times_out_at_deadline(self):
        self.incus.command = Command(hang=True, stdout=b"partial")
        run_id, name, op_id = self.crashed_while_running(timeout_s=60)
        self.incus.forget(op_id)
        result = self.runner.run(run_id)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.stdout_path.read_bytes(), b"partial")
        self.assertIn(("stop", name), self.incus.calls)

    def test_forgotten_op_with_no_process_or_exit_code_is_infra_error(self):
        self.incus.command = Command(hang=True)
        run_id, name, op_id = self.crashed_while_running()
        self.incus.forget(op_id)
        self.incus.instances[name]["command_running"] = False  # e.g. guest rebooted
        result = self.runner.run(run_id)
        self.assertEqual(result.status, "infra_error")
        self.assertIn("left no exit code", result.error)
        self.assertNotIn(EXIT_FILE, self.incus.instances.get(name, {}).get("files", {}))

    def test_pending_with_leftover_instance_starts_over(self):
        run_id = self.runner.create(self.spec(outputs=[]))
        name = instance_name(run_id)
        self.incus.create_instance(name, None, [])
        Invocation.objects.filter(pk=run_id).update(instance_name=name)
        result = self.runner.run(run_id)
        self.assertEqual(result.status, "completed")
        self.assertIn(("delete", name), self.incus.calls)


class ClaimTests(RunnerTestCase):
    def claim(self, run_id, token="other", age_s=0):
        Invocation.objects.filter(pk=run_id).update(
            claim_token=token,
            heartbeat_at=self.clock.now() - timedelta(seconds=age_s),
        )

    def test_second_worker_is_refused_while_claim_is_fresh(self):
        run_id = self.runner.create(self.spec())
        self.claim(run_id, age_s=60)
        with self.assertRaises(RunInProgress):
            self.runner.run(run_id)
        self.assertEqual(self.incus.calls, [])
        self.assertEqual(self.runner.status(run_id).status, "pending")

    def test_lapsed_claim_is_taken_over(self):
        self.incus.command = Command(files={"/home/ci/out/results.json": b"{}"})
        run_id = self.runner.create(self.spec())
        self.claim(run_id, age_s=901)
        self.assertEqual(self.runner.run(run_id).status, "completed")

    def test_finished_run_returns_result_whatever_the_claim(self):
        run_id, first = self.go(outputs=[])
        self.claim(run_id)
        self.assertEqual(self.runner.run(run_id), first)

    def test_heartbeat_is_renewed_while_waiting(self):
        self.incus.command = Command(hang=True)
        run_id = self.runner.create(self.spec(timeout_s=3600, outputs=[]))
        beats = []
        wait = self.incus.wait

        def wait_and_watch(op_id, timeout_s):
            beats.append(Invocation.objects.get(pk=run_id).heartbeat_at)
            return wait(op_id, timeout_s)

        self.incus.wait = wait_and_watch
        self.runner.run(run_id)
        # Never more than one wait chunk between heartbeats.
        gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(beats)]
        self.assertTrue(gaps)
        self.assertLessEqual(max(gaps), 60)

    def test_claim_lost_mid_run_leaves_run_and_instance_alone(self):
        self.incus.command = Command(hang=True)
        run_id = self.runner.create(self.spec())
        wait = self.incus.wait

        def wait_then_steal(op_id, timeout_s):
            if Invocation.objects.get(pk=run_id).status == "running":
                self.claim(run_id, token="thief")
            return wait(op_id, timeout_s)

        self.incus.wait = wait_then_steal
        with self.assertRaises(ClaimLost):
            self.runner.run(run_id)
        inv = Invocation.objects.get(pk=run_id)
        self.assertEqual((inv.status, inv.claim_token), ("running", "thief"))
        self.assertEqual(self.incus.instances[inv.instance_name]["status"], "Running")

    def test_lease_must_outlast_the_longest_wait(self):
        with self.assertRaises(ValueError):
            IncusRunner(self.incus, self.tmp, step_timeout_s=300, lease_s=300)


class FinaliseTests(RunnerTestCase):
    def test_finalise_failure_still_ends_the_run(self):
        self.incus.command = Command(files={"/home/ci/out/results.json": b"{}"})
        with mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
            _, result = self.go()
        self.assertEqual(result.status, "infra_error")
        self.assertIn("finalising: OSError: disk full", result.error)
        self.assertIsNotNone(result.ended_at)
        self.assertEqual(self.incus.instances, {})

    def test_row_left_live_by_a_failed_finalise_is_reaped_by_sweep(self):
        self.incus.command = Command(files={"/home/ci/out/results.json": b"{}"})
        run_id = self.runner.create(self.spec())
        with (
            mock.patch.object(Path, "write_text", side_effect=OSError("disk full")),
            mock.patch.object(
                Invocation.objects, "filter", side_effect=self.failing_terminal_write
            ),
            self.assertRaises(IntegrityError),
        ):
            self.runner.run(run_id)
        self.assertEqual(self.runner.status(run_id).status, "running")
        self.assertEqual(self.incus.instances, {})  # torn down anyway

        self.clock.sleep(901)
        self.runner.sweep()
        inv = Invocation.objects.get(pk=run_id)
        self.assertEqual(inv.status, "infra_error")
        self.assertIn("abandoned", inv.error)

    def failing_terminal_write(self, *args, **kwargs):
        "Invocation.objects.filter, but writing a terminal status fails."
        qs = Invocation.objects.all().filter(*args, **kwargs)
        update = qs.update

        def guarded(**fields):
            if fields.get("status") == "infra_error":
                raise IntegrityError("database is gone")
            return update(**fields)

        qs.update = guarded
        qs.filter = lambda *a, **k: self.failing_terminal_write(
            *args, *a, **kwargs, **k
        )
        return qs


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

    def abandoned(self, age_s, **spec):
        "A running row whose worker last renewed its claim age_s ago."
        run_id = self.runner.create(self.spec(**spec))
        name = instance_name(run_id)
        self.incus.create_instance(name, None, [])
        Invocation.objects.filter(pk=run_id).update(
            status="running",
            instance_name=name,
            claim_token="gone",
            heartbeat_at=self.clock.now() - timedelta(seconds=age_s),
        )
        return run_id, name

    def test_sweep_reaps_abandoned_runs(self):
        stale, stale_name = self.abandoned(age_s=901)
        fresh, fresh_name = self.abandoned(age_s=60)
        queued = self.runner.create(self.spec())  # never started: not abandoned

        swept = self.runner.sweep()

        self.assertEqual(swept, [stale_name])
        inv = Invocation.objects.get(pk=stale)
        self.assertEqual(inv.status, "infra_error")
        self.assertIn("abandoned: no heartbeat", inv.error)
        self.assertIsNotNone(inv.ended_at)
        self.assertEqual(set(self.incus.instances), {fresh_name})
        self.assertEqual(self.runner.status(fresh).status, "running")
        self.assertEqual(self.runner.status(queued).status, "pending")

    def test_sweep_stops_but_keeps_abandoned_keep_on_failure_instance(self):
        _, name = self.abandoned(age_s=901, keep_on_failure=True)
        self.runner.sweep()
        self.assertEqual(self.incus.instances[name]["status"], "Stopped")


class SpecValidationTests(RunnerTestCase):
    def test_relative_guest_paths_rejected(self):
        with self.assertRaises(ValueError):
            self.spec(cwd="work")
        with self.assertRaises(ValueError):
            self.spec(outputs=[Output("out.json", 10)])

    def test_duplicate_outputs_rejected(self):
        with self.assertRaises(ValueError):
            self.spec(outputs=[Output("/out/a", 10), Output("/out/a", 20)])

    def test_failed_insert_leaves_no_run_dir(self):
        with (
            mock.patch.object(
                Invocation.objects, "create", side_effect=IntegrityError("locked")
            ),
            self.assertRaises(IntegrityError),
        ):
            self.runner.create(self.spec())
        self.assertEqual(list((self.tmp / "runs").iterdir()), [])

    def test_missing_input_file_rejected_at_create(self):
        with self.assertRaises(FileNotFoundError):
            self.runner.create(self.spec(inputs=[Input(self.tmp / "nope", "/x")]))
        self.assertEqual(Invocation.objects.count(), 0)
