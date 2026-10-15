"""Tests for scripts/feedback.py and scripts/serve_report.py.

Covers the feedback envelope contract (bounds, idempotency, conflicts,
staleness, path safety) and the loopback server's security posture.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_HERE = pathlib.Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent / "scripts"
for _path in (str(_SCRIPTS), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import feedback  # noqa: E402
import render_report  # noqa: E402
import serve_report  # noqa: E402
import test_interactive  # noqa: E402

TOKEN = "test-token-not-a-secret"


def _request(port, method, path, body=None, headers=None):
    url = f"http://127.0.0.1:{port}{path}"
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class FeedbackTests(unittest.TestCase):
    """Envelope validation against a real, rendered view manifest."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="herdr-feedback-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.task_dir = os.path.join(self.root, ".agent-pr", "tasks", "task-001")
        os.makedirs(self.task_dir)
        config = test_interactive.v2_config(mode="live", port=8765)
        with open(os.path.join(self.task_dir, "workflow.resolved.json"), "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        data = test_interactive.with_hunk()
        data["status"] = "REVIEWING"
        with open(os.path.join(self.task_dir, "report.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        render_report.write_report(self.task_dir)  # writes report.html + views/<id>.json
        views = os.path.join(self.task_dir, "views")
        self.view_id = [n[:-5] for n in os.listdir(views) if n.endswith(".json")][0]
        self.manifest = feedback.load_manifest(self.task_dir, self.view_id)
        self.anchor = self.manifest["line_anchors"][0]["anchor_id"]

    def envelope(self, body="What happens when the input is empty?", anchor=None,
                 comment_id="c-1", task_id="task-001"):
        return {
            "schema_version": 1,
            "task_id": task_id,
            "view_id": self.view_id,
            "batch_id": "b-1",
            "comments": [
                {"id": comment_id, "body": body,
                 "target": {"kind": "line", "anchor_id": anchor or self.anchor}}
            ],
        }

    def test_valid_envelope_normalises(self):
        result = feedback.validate_envelope(self.envelope(), self.manifest)
        self.assertEqual(result["comments"][0]["id"], "c-1")
        self.assertEqual(result["comments"][0]["target"]["kind"], "line")

    def test_renderer_accepts_a_bom_prefixed_report(self):
        """A BOM from the controller must not stop the renderer (2026-10-13 bug)."""
        report_path = os.path.join(self.task_dir, "report.json")
        with open(report_path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        with open(report_path, "w", encoding="utf-8-sig") as handle:
            json.dump(data, handle)
        written = render_report.write_report(self.task_dir)
        self.assertTrue(os.path.isfile(written))
        self.assertTrue(os.path.basename(written).startswith("report"))

    def test_unknown_anchor_rejected(self):
        with self.assertRaises(feedback.FeedbackError):
            feedback.validate_envelope(self.envelope(anchor="line-nope"), self.manifest)

    def test_thread_and_file_targets_resolve(self):
        finding_thread = self.manifest["threads"][0]["thread_id"]
        envelope = self.envelope()
        envelope["comments"][0]["target"] = {"kind": "thread", "thread_id": finding_thread}
        feedback.validate_envelope(envelope, self.manifest)

        envelope = self.envelope()
        envelope["comments"][0]["target"] = {
            "kind": "file", "file_id": self.manifest["files"][0]["file_id"]
        }
        feedback.validate_envelope(envelope, self.manifest)

        envelope = self.envelope()
        envelope["comments"][0]["target"] = {"kind": "file", "file_id": "file-nope"}
        with self.assertRaises(feedback.FeedbackError):
            feedback.validate_envelope(envelope, self.manifest)

    def test_foreign_task_id_rejected(self):
        with self.assertRaises(feedback.FeedbackError):
            feedback.validate_envelope(self.envelope(task_id="other-task"), self.manifest)

    def test_empty_batch_rejected(self):
        envelope = self.envelope()
        envelope["comments"] = []
        with self.assertRaises(feedback.FeedbackError):
            feedback.validate_envelope(envelope, self.manifest)

    def test_duplicate_comment_id_rejected(self):
        envelope = self.envelope()
        envelope["comments"].append(dict(envelope["comments"][0]))
        with self.assertRaises(feedback.FeedbackError):
            feedback.validate_envelope(envelope, self.manifest)

    def test_too_many_comments_rejected(self):
        envelope = self.envelope()
        envelope["comments"] = [
            {"id": f"c-{i}", "body": "x",
             "target": {"kind": "line", "anchor_id": self.anchor}}
            for i in range(feedback.MAX_COMMENTS + 1)
        ]
        with self.assertRaises(feedback.FeedbackError):
            feedback.validate_envelope(envelope, self.manifest)

    def test_blank_and_oversized_and_control_bodies_rejected(self):
        for body in ("   ", "x" * (feedback.MAX_BODY_BYTES + 1), "bad\x07bell"):
            with self.assertRaises(feedback.FeedbackError):
                feedback.validate_envelope(self.envelope(body=body), self.manifest)

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(feedback.FeedbackError):
            feedback.load_json('{"a": 1, "a": 2}')

    def test_non_finite_numbers_rejected(self):
        with self.assertRaises(feedback.FeedbackError):
            feedback.load_json('{"a": NaN}')

    def test_unknown_view_rejected(self):
        with self.assertRaises(feedback.FeedbackError):
            feedback.load_manifest(self.task_dir, "rv-missing")

    def test_view_id_path_traversal_rejected(self):
        for bad in ("../../etc/passwd", "..\\..\\win", "a/b"):
            with self.assertRaises(feedback.FeedbackError):
                feedback.load_manifest(self.task_dir, bad)

    def test_task_dir_layout_enforced(self):
        stray = tempfile.mkdtemp(prefix="herdr-stray-")
        self.addCleanup(shutil.rmtree, stray, ignore_errors=True)
        with self.assertRaises(feedback.FeedbackError):
            feedback.resolve_task_dir(stray)

    def test_identical_resubmission_is_a_noop_retry(self):
        envelope = self.envelope()
        index = {"c-1": feedback._canonical(envelope["comments"][0])}
        result = feedback.validate_envelope(envelope, self.manifest, index)
        self.assertEqual(result["comments"], [])
        self.assertEqual(result["retried_comment_ids"], ["c-1"])

    def test_changed_content_is_a_conflict(self):
        envelope = self.envelope(body="different text")
        index = {"c-1": feedback._canonical(self.envelope()["comments"][0])}
        with self.assertRaises(feedback.FeedbackError):
            feedback.validate_envelope(envelope, self.manifest, index)

    def test_stage_is_idempotent_and_conflict_aware(self):
        text = json.dumps(self.envelope())
        first = feedback.stage_envelope(self.task_dir, "b-1", text)
        self.assertTrue(os.path.isfile(first))
        self.assertEqual(feedback.stage_envelope(self.task_dir, "b-1", text), first)
        with self.assertRaises(feedback.FeedbackError):
            feedback.stage_envelope(self.task_dir, "b-1", json.dumps(self.envelope(body="other")))

    def test_stale_view_is_flagged_not_rejected(self):
        """A v1 report has no feedback block to cache a view, so it is not stale.

        The envelope is still validated: an unknown "current view" is a warning
        for the reviewer, never a reason to reject their comment.
        """
        self.assertEqual(feedback.current_view_id(self.task_dir), self.view_id)
        self.assertTrue(feedback.validate_envelope(self.envelope(), self.manifest))

    def _fresh_v2_task(self):
        """A task whose report.json carries an empty (un-imported) feedback cache."""
        root = tempfile.mkdtemp(prefix="herdr-fresh-v2-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        task_dir = os.path.join(root, ".agent-pr", "tasks", "task-001")
        os.makedirs(task_dir)
        with open(os.path.join(task_dir, "workflow.resolved.json"), "w", encoding="utf-8") as handle:
            json.dump(test_interactive.v2_config(mode="live", port=8765), handle)
        data = test_interactive.with_hunk()
        data["status"] = "REVIEWING"
        data["schema_version"] = 2
        data["feedback"] = {
            "revision": 0, "current_view_id": "", "views": [], "threads": [],
            "comments": [], "decisions": [], "receipts": [],
        }
        with open(os.path.join(task_dir, "report.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        render_report.write_report(task_dir)
        return task_dir

    def test_freshly_rendered_report_is_not_stale(self):
        """A current report must not report stale on its initial handoff.

        Regression: ``write_report`` names ``views/<id>.json`` and stamps
        ``data-view-id`` on the page without rewriting ``report.json`` - that
        file belongs to the controller - so on a fresh task
        ``feedback.current_view_id`` is still ``""`` while the rendered view is
        current. Comparing the two produced ``"stale": true`` on an up-to-date
        report. The projection is now recomputed from the report itself.
        """
        task_dir = self._fresh_v2_task()
        with open(os.path.join(task_dir, "report.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["feedback"]["current_view_id"], "")
        rendered = [
            name[:-5] for name in os.listdir(os.path.join(task_dir, "views"))
            if name.endswith(".json")
        ]
        self.assertEqual(len(rendered), 1)
        # The cache is empty, yet the projection is the view on the page.
        self.assertEqual(feedback.current_view_id(task_dir), rendered[0])
        with open(os.path.join(task_dir, "report.html"), encoding="utf-8") as handle:
            self.assertIn(f'data-view-id="{rendered[0]}"', handle.read())

    def test_a_cached_stale_view_is_still_flagged(self):
        """The fallback must not paper over a genuinely outdated projection.

        Once the controller has imported a batch it writes the view it projected
        into ``report.json``. That cached value wins, so a task that has moved on
        since still reads as stale.
        """
        task_dir = self._fresh_v2_task()
        current = feedback.current_view_id(task_dir)
        path = os.path.join(task_dir, "report.json")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        data["feedback"]["current_view_id"] = "rv-0000000000000000"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        self.assertEqual(feedback.current_view_id(task_dir), "rv-0000000000000000")
        self.assertNotEqual(current, "rv-0000000000000000")

    def test_projection_degrades_to_none_without_a_report(self):
        """No report.json is still `None` - the pre-existing contract."""
        empty = os.path.join(self.root, ".agent-pr", "tasks", "task-002")
        os.makedirs(empty)
        self.assertIsNone(feedback.current_view_id(empty))

    def test_projected_view_id_matches_the_written_manifest(self):
        """`projected_view_id` must agree with the manifest the renderer wrote."""
        task_dir = self._fresh_v2_task()
        with open(os.path.join(task_dir, "report.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        rendered = [
            name[:-5] for name in os.listdir(os.path.join(task_dir, "views"))
            if name.endswith(".json")
        ]
        self.assertEqual(feedback.projected_view_id(data), rendered[0])
        # An unusable report degrades to None rather than raising.
        self.assertIsNone(feedback.projected_view_id({}))
        self.assertIsNone(feedback.projected_view_id("not a report"))


class LoopbackTests(unittest.TestCase):
    def test_loopback_host_accepts_any_port(self):
        for value in ("127.0.0.1:8765", "localhost:1234", "[::1]:9000", "127.0.0.1"):
            self.assertTrue(serve_report._loopback_host(value), value)
        for value in ("example.com:80", "0.0.0.0:80", "10.0.0.5:80", ""):
            self.assertFalse(serve_report._loopback_host(value), value)

    def test_client_loopback_check(self):
        handler = serve_report._Handler.__new__(serve_report._Handler)
        handler.client_address = ("127.0.0.1", 5000)
        self.assertTrue(handler._client_is_loopback())
        handler.client_address = ("10.0.0.5", 5000)
        self.assertFalse(handler._client_is_loopback())

    def test_origin_must_be_loopback_any_port(self):
        class Fake:
            def __init__(self, headers):
                self.headers = headers

        self.assertTrue(serve_report._same_origin_ok(Fake({"Host": "127.0.0.1:8765"})))
        self.assertTrue(serve_report._same_origin_ok(
            Fake({"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:4321"})
        ))
        self.assertFalse(serve_report._same_origin_ok(
            Fake({"Host": "127.0.0.1:8765", "Origin": "http://evil.example"})
        ))
        self.assertFalse(serve_report._same_origin_ok(Fake({"Host": "evil.example"})))

    def test_resolve_port_prefers_config(self):
        root = tempfile.mkdtemp(prefix="herdr-port-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(root, "workflow.resolved.json"), "w", encoding="utf-8") as handle:
            json.dump(test_interactive.v2_config(mode="live", port=9123), handle)
        self.assertEqual(serve_report.resolve_port(root), 9123)
        self.assertEqual(serve_report.resolve_port(root, 7000), 7000)

    def test_require_live_mode_accepts_live_and_absent_config(self):
        root = tempfile.mkdtemp(prefix="herdr-live-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        serve_report.require_live_mode(root)  # an absent config is allowed
        with open(os.path.join(root, "workflow.resolved.json"), "w", encoding="utf-8") as handle:
            json.dump(test_interactive.v2_config(mode="live", port=9123), handle)
        serve_report.require_live_mode(root)  # live is the only mode it serves

    def test_require_live_mode_rejects_non_live_modes(self):
        for mode in ("static", "offline-feedback"):
            with self.subTest(mode=mode):
                root = tempfile.mkdtemp(prefix="herdr-live-")
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                with open(os.path.join(root, "workflow.resolved.json"), "w", encoding="utf-8") as handle:
                    json.dump(test_interactive.v2_config(mode=mode, port=9123), handle)
                with self.assertRaises(serve_report.ServerError) as ctx:
                    serve_report.require_live_mode(root)
                self.assertIn(mode, str(ctx.exception))

    def test_policy_reads_tolerate_a_utf8_bom(self):
        """The controller writes BOM'd JSON; every reader must accept it."""
        root = tempfile.mkdtemp(prefix="herdr-bom-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        config = test_interactive.v2_config(mode="live", port=9123)
        with open(os.path.join(root, "workflow.resolved.json"), "w", encoding="utf-8-sig") as handle:
            json.dump(config, handle)
        serve_report.require_live_mode(root)  # raises on a BOM-intolerant read
        self.assertEqual(serve_report.resolve_port(root), 9123)

    def test_busy_fixed_port_is_a_clear_error(self):
        root = tempfile.mkdtemp(prefix="herdr-port-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        first = serve_report.make_server(root, 0, "t")
        self.addCleanup(first.server_close)
        port = first.server_address[1]
        with self.assertRaises(serve_report.ServerError):
            serve_report.make_server(root, port, "t")

    def test_stop_without_a_pidfile_is_clean(self):
        root = tempfile.mkdtemp(prefix="herdr-stop-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        payload, code = serve_report.stop_server(root)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "NOT_RUNNING")


class ServerTests(unittest.TestCase):
    def setUp(self):
        original_log = serve_report._Handler.log_message
        self.addCleanup(setattr, serve_report._Handler, "log_message", original_log)
        serve_report._Handler.log_message = lambda self, *a, **k: None

        self.root = tempfile.mkdtemp(prefix="herdr-server-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.task_dir = os.path.join(self.root, ".agent-pr", "tasks", "task-001")
        os.makedirs(self.task_dir)
        with open(os.path.join(self.task_dir, "workflow.resolved.json"), "w", encoding="utf-8") as handle:
            json.dump(test_interactive.v2_config(mode="live", port=8765), handle)
        data = test_interactive.with_hunk()
        data["status"] = "REVIEWING"
        with open(os.path.join(self.task_dir, "report.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        render_report.write_report(self.task_dir)
        views = os.path.join(self.task_dir, "views")
        self.view_id = [n[:-5] for n in os.listdir(views) if n.endswith(".json")][0]
        self.manifest = feedback.load_manifest(self.task_dir, self.view_id)
        self.anchor = self.manifest["line_anchors"][0]["anchor_id"]

        self.server = serve_report.make_server(self.task_dir, 0, TOKEN)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def envelope(self, body="Is this reachable when the list is empty?",
                 anchor=None, batch_id="b-1", comment_id="c-1"):
        return {
            "schema_version": 1,
            "task_id": "task-001",
            "view_id": self.view_id,
            "batch_id": batch_id,
            "comments": [
                {"id": comment_id, "body": body,
                 "target": {"kind": "line", "anchor_id": anchor or self.anchor}}
            ],
        }

    def post(self, envelope, token=TOKEN, content_type="application/json", origin=None):
        headers = {}
        if token is not None:
            headers["X-Herdr-Token"] = token
        if content_type is not None:
            headers["Content-Type"] = content_type
        if origin is not None:
            headers["Origin"] = origin
        body = json.dumps(envelope).encode("utf-8")
        return _request(self.port, "POST", "/api/feedback", body, headers)

    def test_serves_the_report_at_root(self):
        status, body = _request(self.port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Add the widget feature", body)

    def test_health_endpoint(self):
        status, body = _request(self.port, "GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_unknown_path_is_404(self):
        status, _ = _request(self.port, "GET", "/nope")
        self.assertEqual(status, 404)

    def test_valid_submission_is_queued_and_staged(self):
        status, body = self.post(self.envelope())
        self.assertEqual(status, 202)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "queued")
        staged = os.path.join(self.task_dir, "inbox", "human", "b-1.json")
        self.assertTrue(os.path.isfile(staged))

    def test_wrong_or_missing_token_is_403(self):
        for token in ("wrong", None):
            status, _ = self.post(self.envelope(), token=token)
            self.assertEqual(status, 403)

    def test_foreign_origin_is_403(self):
        status, _ = self.post(self.envelope(), origin="http://evil.example")
        self.assertEqual(status, 403)

    def test_forwarded_loopback_origin_is_accepted(self):
        status, _ = self.post(self.envelope(), origin="http://127.0.0.1:59999")
        self.assertEqual(status, 202)

    def test_bad_content_type_is_415(self):
        status, _ = self.post(self.envelope(), content_type="text/plain")
        self.assertEqual(status, 415)

    def test_oversized_body_is_413(self):
        headers = {"X-Herdr-Token": TOKEN, "Content-Type": "application/json"}
        big = b"x" * (feedback.MAX_ENVELOPE_BYTES + 10)
        status, _ = _request(self.port, "POST", "/api/feedback", big, headers)
        self.assertEqual(status, 413)

    def test_unknown_anchor_is_422(self):
        status, body = self.post(self.envelope(anchor="line-nope"))
        self.assertEqual(status, 422)
        self.assertIn("unknown line anchor", json.loads(body)["message"])

    def test_identical_resubmission_is_a_noop(self):
        first = self.post(self.envelope())
        self.assertEqual(first[0], 202)
        second = self.post(self.envelope())
        self.assertEqual(second[0], 202)
        self.assertEqual(json.loads(second[1])["retried"], 1)

    def test_conflicting_resubmission_is_rejected(self):
        self.assertEqual(self.post(self.envelope())[0], 202)
        status, _ = self.post(self.envelope(body="changed my mind"))
        self.assertEqual(status, 422)

    def test_server_never_writes_canonical_records(self):
        with open(os.path.join(self.task_dir, "report.json"), "rb") as handle:
            before = handle.read()
        self.post(self.envelope())
        with open(os.path.join(self.task_dir, "report.json"), "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(os.path.join(self.task_dir, "events.jsonl")))

    def test_queued_note_does_not_claim_import(self):
        _, body = self.post(self.envelope())
        payload = json.loads(body)
        self.assertIn("not yet imported", payload["note"])


if __name__ == "__main__":
    unittest.main()
