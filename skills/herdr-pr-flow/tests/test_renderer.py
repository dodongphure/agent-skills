"""Unit tests for herdr-pr-flow scripts/render_report.py.

Run from the skill directory:

    python -m unittest discover -s tests -v

Fixtures are built in memory inside this module; no external report files are
read. Temporary directories are created only for the writer tests.
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_HERE = pathlib.Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import render_report  # noqa: E402
from render_report import (  # noqa: E402
    PathSecurityError,
    ReportError,
    ValidationError,
    render_html,
    validate_report,
    write_report,
)

_HEX_A = "a" * 64
_HEX_B = "b" * 64


def make_report() -> dict:
    """A minimal, fully valid READY_FOR_HUMAN report (schema v1)."""
    return {
        "schema_version": 1,
        "task_id": "task-001",
        "title": "Add the widget feature",
        "status": "READY_FOR_HUMAN",
        "requirements": ["Implement the widget", "Cover it with tests"],
        "plan": "1. design\n2. implement\n3. verify",
        "participants": [
            {
                "id": "ctrl",
                "role": "controller",
                "cli": "claude",
                "requested_model": "model-ctrl",
                "observed_model": "model-ctrl",
                "model_provenance": "reported",
            },
            {
                "id": "dev",
                "role": "developer",
                "cli": "codex",
                "requested_model": "model-dev",
                "observed_model": None,
                "model_provenance": "unverified",
            },
            {
                "id": "rev1",
                "role": "reviewer",
                "cli": "claude",
                "requested_model": "model-rev",
                "observed_model": "model-rev",
                "model_provenance": "reported",
            },
        ],
        "snapshots": [
            {"id": "s1", "manifest_sha256": _HEX_A},
            {"id": "s2", "manifest_sha256": _HEX_B},
        ],
        "final_snapshot_id": "s2",
        "findings": [
            {
                "id": "f1",
                "owner": "rev1",
                "severity": "high",
                "path": "src/widget.py",
                "line": 42,
                "snapshot_id": "s1",
                "body": "Off-by-one in the loop bound.",
                "acceptance": "Loop bound fixed and covered by a test.",
                "status": "resolved",
                "history": [
                    {
                        "actor": "rev1",
                        "snapshot_id": "s1",
                        "kind": "comment",
                        "body": "Please fix the loop bound.",
                        "verdict": None,
                    },
                    {
                        "actor": "dev",
                        "snapshot_id": "s2",
                        "kind": "fix",
                        "body": "Adjusted the bound.",
                        "verdict": None,
                        "diff": "--- a/src/widget.py\n+++ b/src/widget.py\n- for i in range(n+1):\n+ for i in range(n):\n",
                    },
                    {
                        "actor": "rev1",
                        "snapshot_id": "s2",
                        "kind": "verification",
                        "body": "Confirmed the fix.",
                        "verdict": "addressed",
                    },
                ],
            }
        ],
        "approvals": [
            {"reviewer": "rev1", "snapshot_id": "s2", "verdict": "approved", "body": "LGTM"}
        ],
        "checks": [
            {
                "id": "c1",
                "snapshot_id": "s2",
                "argv": ["pytest", "-q"],
                "exit_code": 0,
                "output": "3 passed",
                "required": True,
            }
        ],
        "changes": [
            {"path": "src/widget.py", "diff": "--- a/src/widget.py\n+++ b/src/widget.py\n+pass\n"}
        ],
        "timeline": [
            {"actor": "ctrl", "kind": "plan", "body": "Planned the work.", "snapshot_id": None},
            {"actor": "dev", "kind": "implement", "body": "Wrote code.", "snapshot_id": "s1"},
            {"actor": "rev1", "kind": "review", "body": "Reviewed s1.", "snapshot_id": "s1"},
        ],
    }


class ValidationTests(unittest.TestCase):
    def test_valid_ready_report_passes(self):
        validate_report(make_report())  # must not raise

    def test_render_returns_self_contained_html(self):
        out = render_html(make_report())
        self.assertTrue(out.startswith("<!DOCTYPE html>"))
        self.assertIn("Add the widget feature", out)
        self.assertIn("task-001", out)
        self.assertIn("READY_FOR_HUMAN", out)

    def test_model_labels_show_unverified_for_null_observed(self):
        out = render_html(make_report())
        # developer has observed_model = null -> shown as "unverified", never invented
        self.assertIn("unverified", out)
        self.assertIn("model-ctrl", out)
        self.assertIn("model-rev", out)
        row = out[out.index('>dev</td>') : out.index('>dev</td>') + 400]
        self.assertIn('<span class="unverified">unverified</span>', row)

    def test_no_script_or_remote_assets(self):
        out = render_html(make_report())
        for needle in ("<script", "</script", "<img", "http://", "https://", "fetch("):
            self.assertNotIn(needle, out, msg=f"unexpected {needle!r} in output")

    def test_full_diff_is_not_truncated(self):
        data = make_report()
        big = "\n".join(f"+line {i}" for i in range(400))
        data["changes"][0]["diff"] = big
        out = render_html(data)
        self.assertIn("+line 0", out)
        self.assertIn("+line 399", out)  # last line survives in full

    def test_prose_blocks_wrap_instead_of_scrolling(self):
        """The plan and check output must wrap, not get a horizontal scroller."""
        data = make_report()
        data["plan"] = "Step 1: " + ("a deliberately long plan line " * 30) + "\nStep 2: done."
        data["checks"][0]["output"] = "a single very long output line " * 12
        out = render_html(data)
        self.assertIn('<pre class="plan">', out)
        self.assertIn('<pre class="out">', out)
        self.assertIn("Step 2: done.", out)  # newlines survive the escaping
        rule = out[out.index("pre.plan, pre.out {"): out.index("pre.plan, pre.out {") + 160]
        self.assertIn("white-space: pre-wrap", rule)
        self.assertIn("overflow-x: visible", rule)
        # The shared rule still scrolls, so diffs keep their line alignment.
        self.assertIn("white-space: pre;", out)

    def test_timeline_entries_are_tone_classified(self):
        out = render_html(make_report())
        # The rail item and its kind chip share one tone, so they can never drift.
        self.assertIn('<li class="tl-item tl-yellow">', out)
        self.assertIn('<li class="tl-item tl-accent">', out)
        self.assertIn('<span class="kind k-yellow">plan</span>', out)
        self.assertIn('<span class="kind k-accent">review</span>', out)
        self.assertIn(".tl-item::before", out)  # the rail/dot rule ships

    def test_timeline_uses_role_badges_not_raw_ids(self):
        out = render_html(make_report())
        start = out.index("<h2>Conversation timeline</h2>")
        section = out[start:start + 2000]
        self.assertIn('<span class="badge role-controller">Controller</span>', section)
        self.assertIn('<span class="badge role-reviewer">Reviewer 1</span>', section)
        self.assertNotIn('class="mono">ctrl<', section)

    def test_unknown_timeline_kind_is_still_shown(self):
        data = make_report()
        data["timeline"][0]["kind"] = "teleported"
        out = render_html(data)
        self.assertIn('<li class="tl-item tl-muted">', out)
        self.assertIn('<span class="kind k-muted">teleported</span>', out)

    def test_kind_tone_table_covers_the_documented_kinds(self):
        cases = {
            "snapshot": "accent", "review": "accent", "dispatch": "purple",
            "plan": "yellow", "implement": "yellow", "fix": "yellow",
            "verification": "green", "approved": "green",
            "blocked": "red", "error": "red",
            "comment": "muted", "check": "muted", "teleported": "muted",
        }
        for kind, tone in cases.items():
            with self.subTest(kind=kind):
                self.assertEqual(render_report._kind_tone(kind), tone)
        # Unknown actors fall back to the raw id rather than a wrong badge.
        self.assertIn("ghost", render_report._actor_html({}, "ghost"))

    def test_xss_payloads_are_escaped(self):
        data = make_report()
        data["title"] = '<script>alert("xss")</script>'
        data["timeline"][0]["body"] = "<img src=x onerror=alert(1)>"
        out = render_html(data)
        self.assertNotIn("<script>", out)
        self.assertNotIn("<img", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("&lt;img", out)
        self.assertIn("&quot;xss&quot;", out)

    def test_stale_approval_rejected(self):
        data = make_report()
        # A later approval lands on the older snapshot -> the reviewer's latest is stale.
        data["approvals"].append(
            {"reviewer": "rev1", "snapshot_id": "s1", "verdict": "approved", "body": "old"}
        )
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_stale_check_rejected(self):
        data = make_report()
        # Latest occurrence of the required check id is not on the final snapshot.
        data["checks"].append(
            {
                "id": "c1",
                "snapshot_id": "s1",
                "argv": ["pytest"],
                "exit_code": 0,
                "output": "old run",
                "required": True,
            }
        )
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_missing_reviewer_rejected(self):
        data = make_report()
        data["participants"] = [p for p in data["participants"] if p["role"] != "reviewer"]
        data["findings"] = []
        data["approvals"] = []
        data["timeline"] = [t for t in data["timeline"] if t["actor"] != "rev1"]
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_wrong_owner_verification_cannot_resolve(self):
        data = make_report()
        # Last verification is by a non-owner -> cannot resolve the finding.
        data["findings"][0]["history"][-1]["actor"] = "dev"
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_resolved_without_verification_rejected(self):
        data = make_report()
        data["findings"][0]["history"] = data["findings"][0]["history"][:2]
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_open_finding_rejected_for_ready(self):
        data = make_report()
        data["findings"][0]["status"] = "open"
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_open_finding_allowed_when_not_ready(self):
        data = make_report()
        data["status"] = "REVIEWING"
        data["findings"][0]["status"] = "open"
        validate_report(data)  # must not raise

    def test_duplicate_participant_ids_rejected(self):
        data = make_report()
        data["participants"].append(copy.deepcopy(data["participants"][0]))
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_duplicate_snapshot_ids_rejected(self):
        data = make_report()
        data["snapshots"].append(copy.deepcopy(data["snapshots"][1]))
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_duplicate_check_within_snapshot_rejected(self):
        data = make_report()
        data["checks"].append(copy.deepcopy(data["checks"][0]))
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_unknown_reference_rejected(self):
        for mutate in (
            lambda d: d["findings"][0].__setitem__("owner", "ghost"),
            lambda d: d["findings"][0].__setitem__("snapshot_id", "s9"),
            lambda d: d["timeline"][0].__setitem__("actor", "ghost"),
            lambda d: d["approvals"][0].__setitem__("snapshot_id", "s9"),
            lambda d: d.__setitem__("final_snapshot_id", "s9"),
        ):
            data = make_report()
            mutate(data)
            with self.assertRaises(ValidationError):
                validate_report(data)

    def test_incomplete_required_check_rejected(self):
        data = make_report()
        data["checks"][0]["exit_code"] = 1
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_required_check_not_run_rejected(self):
        data = make_report()
        data["checks"][0]["exit_code"] = None
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_no_required_check_rejected(self):
        data = make_report()
        data["checks"][0]["required"] = False
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_bad_hash_rejected(self):
        data = make_report()
        data["snapshots"][0]["manifest_sha256"] = "xyz"
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_bad_status_rejected(self):
        data = make_report()
        data["status"] = "DONE"
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_wrong_types_rejected(self):
        data = make_report()
        data["requirements"] = "not a list"
        with self.assertRaises(ValidationError):
            validate_report(data)

        data = make_report()
        data["findings"][0]["line"] = "42"  # string, not int
        with self.assertRaises(ValidationError):
            validate_report(data)

        data = make_report()
        data["checks"][0]["required"] = "yes"  # string, not bool
        with self.assertRaises(ValidationError):
            validate_report(data)


class WriterTests(unittest.TestCase):
    def _workspace(self):
        root = tempfile.mkdtemp(prefix="herdr-render-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        task_dir = os.path.join(root, ".agent-pr", "tasks", "task-001")
        os.makedirs(task_dir)
        config = {"schema_version": 1, "roles": {"controller": {"cli": "claude", "model": "model-ctrl", "extra_args": []}, "developer": {"cli": "codex", "model": "model-dev", "extra_args": []}, "reviewers": [{"id": "rev1", "cli": "claude", "model": "model-rev", "extra_args": [], "focus": ["tests"]}]}, "workflow": {"max_fix_attempts_per_finding": 3, "max_task_minutes": 120, "wait_timeout_ms": 600000, "model_diversity": "warn"}, "verification": {"required_checks": [{"id": "c1", "argv": ["pytest", "-q"]}]}, "safety": {"allow_stage": False, "allow_commit": False, "allow_push": False, "allow_merge": False}}
        self._write_json(os.path.join(task_dir, "workflow.resolved.json"), config)
        return root, task_dir

    def _write_json(self, path, data):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def _symlink(self, target, link, directory=False):
        """Create a symlink or skip the test if the platform cannot.

        Some sandboxes accept ``os.symlink`` but silently create nothing, so we
        verify the link actually exists before trusting it.
        """
        try:
            os.symlink(target, link, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable on this platform: {exc}")
        if not os.path.islink(link) or not os.path.lexists(link):
            self.skipTest("platform did not materialise a real symlink")

    def test_missing_config_blocks_ready(self):
        _, task = self._workspace()
        os.unlink(os.path.join(task, "workflow.resolved.json"))
        self._write_json(os.path.join(task, "report.json"), make_report())
        with self.assertRaises(ValidationError):
            write_report(task)

    def test_configured_second_reviewer_cannot_be_omitted(self):
        _, task = self._workspace()
        config_path = os.path.join(task, "workflow.resolved.json")
        with open(config_path) as handle:
            config = json.load(handle)
        second = dict(config["roles"]["reviewers"][0], id="rev2")
        config["roles"]["reviewers"].append(second)
        self._write_json(config_path, config)
        self._write_json(os.path.join(task, "report.json"), make_report())
        with self.assertRaises(ValidationError):
            write_report(task)

    def test_configured_check_cannot_be_omitted(self):
        _, task = self._workspace()
        config_path = os.path.join(task, "workflow.resolved.json")
        with open(config_path) as handle:
            config = json.load(handle)
        config["verification"]["required_checks"].append({"id": "lint", "argv": ["ruff", "check"]})
        self._write_json(config_path, config)
        self._write_json(os.path.join(task, "report.json"), make_report())
        with self.assertRaises(ValidationError):
            write_report(task)

    def test_write_report_happy_path(self):
        _, task_dir = self._workspace()
        self._write_json(os.path.join(task_dir, "report.json"), make_report())

        out = write_report(task_dir)

        self.assertEqual(out, os.path.join(task_dir, "report.html"))
        self.assertTrue(os.path.isfile(out))
        with open(out, "r", encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("Add the widget feature", content)
        self.assertNotIn("<script", content)

    def test_write_report_leaves_no_temp_files(self):
        _, task_dir = self._workspace()
        self._write_json(os.path.join(task_dir, "report.json"), make_report())
        write_report(task_dir)
        leftovers = [n for n in os.listdir(task_dir) if n.startswith(".report-")]
        self.assertEqual(leftovers, [])

    def test_write_report_invalid_report_raises_and_writes_nothing(self):
        _, task_dir = self._workspace()
        bad = make_report()
        bad["status"] = "READY_FOR_HUMAN"
        bad["participants"] = [p for p in bad["participants"] if p["role"] != "reviewer"]
        self._write_json(os.path.join(task_dir, "report.json"), bad)

        with self.assertRaises(ValidationError):
            write_report(task_dir)
        self.assertFalse(os.path.exists(os.path.join(task_dir, "report.html")))

    def test_write_report_rejects_unsafe_task_dir(self):
        stray = tempfile.mkdtemp(prefix="herdr-stray-")
        self.addCleanup(shutil.rmtree, stray, ignore_errors=True)
        self._write_json(os.path.join(stray, "report.json"), make_report())

        with self.assertRaises(PathSecurityError):
            write_report(stray)

    def test_write_report_rejects_tasks_root(self):
        root = tempfile.mkdtemp(prefix="herdr-render-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        tasks = os.path.join(root, ".agent-pr", "tasks")
        os.makedirs(tasks)
        self._write_json(os.path.join(tasks, "report.json"), make_report())

        # Missing the <task-id> leaf component.
        with self.assertRaises(PathSecurityError):
            write_report(tasks)

    def test_write_report_rejects_nonexistent_task_dir(self):
        root = tempfile.mkdtemp(prefix="herdr-render-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        missing = os.path.join(root, ".agent-pr", "tasks", "task-404")
        with self.assertRaises(PathSecurityError):
            write_report(missing)

    def test_write_report_rejects_symlink_task_dir(self):
        root = tempfile.mkdtemp(prefix="herdr-render-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        tasks = os.path.join(root, ".agent-pr", "tasks")
        os.makedirs(tasks)
        real = os.path.join(tasks, "real-001")
        os.makedirs(real)
        self._write_json(os.path.join(real, "report.json"), make_report())
        link = os.path.join(tasks, "link-001")
        self._symlink(real, link, directory=True)

        with self.assertRaises(PathSecurityError):
            write_report(link)

    def test_write_report_rejects_output_symlink(self):
        root, task_dir = self._workspace()
        self._write_json(os.path.join(task_dir, "report.json"), make_report())
        target = os.path.join(root, "elsewhere.html")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("keep me")
        self._symlink(target, os.path.join(task_dir, "report.html"))

        with self.assertRaises(PathSecurityError):
            write_report(task_dir)
        with open(target, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "keep me")  # untouched

    def test_write_report_rejects_report_json_symlink_outside(self):
        root, task_dir = self._workspace()
        outside = os.path.join(root, "outside.json")
        self._write_json(outside, make_report())
        self._symlink(outside, os.path.join(task_dir, "report.json"))

        with self.assertRaises(PathSecurityError):
            write_report(task_dir)

    def test_write_report_missing_report_json_raises(self):
        _, task_dir = self._workspace()
        with self.assertRaises(ReportError):
            write_report(task_dir)

    # --- symlink guards, exercised even where real symlinks are unavailable -- #
    def _islink_patch(self, flagged):
        """Patch os.path.islink so exactly ``flagged`` reports as a symlink."""
        original = os.path.islink
        flagged_n = os.path.normcase(os.path.abspath(flagged))

        def fake(path):
            try:
                if os.path.normcase(os.path.abspath(path)) == flagged_n:
                    return True
            except (TypeError, ValueError):
                pass
            return original(path)

        return mock.patch.object(render_report.os.path, "islink", side_effect=fake)

    def test_symlink_task_dir_guard(self):
        _, task_dir = self._workspace()
        self._write_json(os.path.join(task_dir, "report.json"), make_report())
        with self._islink_patch(task_dir):
            with self.assertRaises(PathSecurityError):
                write_report(task_dir)

    def test_symlink_report_json_guard(self):
        _, task_dir = self._workspace()
        self._write_json(os.path.join(task_dir, "report.json"), make_report())
        with self._islink_patch(os.path.join(task_dir, "report.json")):
            with self.assertRaises(PathSecurityError):
                write_report(task_dir)

    def test_symlink_output_guard(self):
        _, task_dir = self._workspace()
        self._write_json(os.path.join(task_dir, "report.json"), make_report())
        with self._islink_patch(os.path.join(task_dir, "report.html")):
            with self.assertRaises(PathSecurityError):
                write_report(task_dir)
        # The guard fired before writing, so no output was produced.
        self.assertFalse(os.path.exists(os.path.join(task_dir, "report.html")))


class DiffParsingTests(unittest.TestCase):
    """_parse_diff must use hunk state, not bare prefixes (review finding)."""

    def test_added_line_starting_with_plus_is_an_addition(self):
        # The added line's own text begins with "++", so the raw line is "+++counter;".
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,2 +1,3 @@\n"
            " a = 1\n"
            "+++counter;\n"
            " b = 2\n"
        )
        rows = list(render_report._parse_diff(diff))
        self.assertEqual(rows[0][0], "meta")  # --- a/x.py  (file header)
        self.assertEqual(rows[1][0], "meta")  # +++ b/x.py  (file header)
        self.assertEqual(rows[2][0], "hunk")
        added = [r for r in rows if r[0] == "add"]
        self.assertEqual(len(added), 1, "the '+++counter;' line must be an addition")
        self.assertEqual(added[0][1], "+++counter;")
        self.assertEqual(added[0][3], 2, "added line must carry its new-file number")
        self.assertEqual(render_report._diff_stat(diff), (1, 0))
        html = render_report._diff_block(diff)
        self.assertIn(
            '<span class="dl dl-add"><span class="ln"></span>'
            '<span class="ln">2</span><span class="code">+++counter;</span></span>',
            html,
        )

    def test_removed_line_starting_with_minus_is_a_removal(self):
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,3 +1,2 @@\n"
            " a = 1\n"
            "---counter;\n"
            " b = 2\n"
        )
        rows = list(render_report._parse_diff(diff))
        removed = [r for r in rows if r[0] == "del"]
        self.assertEqual(len(removed), 1, "the '---counter;' line must be a removal")
        self.assertEqual(removed[0][1], "---counter;")
        self.assertEqual(removed[0][2], 2, "removed line must carry its old-file number")
        self.assertEqual(render_report._diff_stat(diff), (0, 1))

    def test_headerless_fragment_still_classified_by_prefix(self):
        # Hand-written fragments (and the bundled example) have no @@ header;
        # prefix classification is the only signal available there.
        diff = "--- a/x.py\n+++ b/x.py\n- old\n+ new\n"
        kinds = [k for k, *_ in render_report._parse_diff(diff)]
        self.assertEqual(kinds, ["meta", "meta", "del", "add"])
        self.assertEqual(render_report._diff_stat(diff), (1, 1))

    def test_second_hunk_uses_its_own_line_numbers(self):
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-a\n"
            "+b\n"
            "@@ -10,1 +10,1 @@\n"
            "-c\n"
            "+d\n"
        )
        rows = list(render_report._parse_diff(diff))
        self.assertEqual([r[3] for r in rows if r[0] == "add"], [1, 10])
        self.assertEqual([r[2] for r in rows if r[0] == "del"], [1, 10])

    def test_nonewline_marker_is_not_counted(self):
        diff = (
            "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n"
            "-old\n+new\n\\ No newline at end of file\n"
        )
        self.assertEqual(render_report._diff_stat(diff), (1, 1))
        self.assertEqual(
            [r[0] for r in render_report._parse_diff(diff)],
            ["meta", "meta", "hunk", "del", "add", "meta"],
        )


if __name__ == "__main__":
    unittest.main()
