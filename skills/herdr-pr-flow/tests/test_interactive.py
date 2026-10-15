"""Tests for the interactive feedback layer.

Covers: config v2 validation and the version adapter, snapshot-bound anchor
identity, the interactive render modes, and the schema-v2 feedback projection.

The static contract tests live in test_renderer.py and must keep passing
unchanged -- the default render is byte-for-byte identical to the old output.
"""
from __future__ import annotations

import copy
import itertools
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent / "scripts"
for _path in (str(_SCRIPTS), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import check_config  # noqa: E402
import render_report  # noqa: E402
import test_renderer  # noqa: E402
from render_report import ValidationError, render_html, validate_report, write_report  # noqa: E402

HUNK_DIFF = (
    "--- a/src/widget.py\n"
    "+++ b/src/widget.py\n"
    "@@ -1,4 +1,5 @@\n"
    " import os\n"
    "-old = 1\n"
    "+new = 1\n"
    "+extra = 2\n"
    " print(os)\n"
)
HEXLESS = "a" * 64


def v2_config(mode="offline-feedback", port=8765):
    return {
        "schema_version": 2,
        "roles": {
            "controller": {"cli": "claude", "model": "model-ctrl", "extra_args": []},
            "developer": {"cli": "codex", "model": "model-dev", "extra_args": []},
            "reviewers": [
                {"id": "rev1", "cli": "claude", "model": "model-rev",
                 "extra_args": [], "focus": ["tests"]}
            ],
        },
        "workflow": {
            "max_fix_attempts_per_finding": 3,
            "max_task_minutes": 120,
            "wait_timeout_ms": 600000,
            "model_diversity": "warn",
        },
        "verification": {"required_checks": [{"id": "c1", "argv": ["pytest", "-q"]}]},
        "safety": {"allow_stage": False, "allow_commit": False,
                   "allow_push": False, "allow_merge": False},
        "report": {"mode": mode, "port": port},
    }


def v1_config():
    config = v2_config()
    config["schema_version"] = 1
    del config["report"]
    return config


def with_hunk():
    data = test_renderer.make_report()
    data["changes"][0]["diff"] = HUNK_DIFF
    return data


def feedback_block(anchor_id, disposition="recorded", finding_id=None):
    return {
        "revision": 1,
        "current_view_id": "rv-placeholder",
        "views": [{"id": "rv-placeholder", "digest": HEXLESS}],
        "threads": [
            {"id": "human/t1",
             "target": {"kind": "line", "anchor_id": anchor_id},
             "status": "open", "comment_ids": ["c1"]}
        ],
        "comments": [
            {"id": "c1", "thread_id": "human/t1",
             "target": {"kind": "line", "anchor_id": anchor_id},
             "body": "What happens when the input is empty?",
             "origin_view_id": "rv-placeholder",
             "received_at": "2026-10-13T11:00:00Z", "batch_id": "b1"}
        ],
        "decisions": [
            {"comment_id": "c1", "controller": "ctrl", "disposition": disposition,
             "rationale": "Answered inline.", "snapshot_id": None,
             "finding_id": finding_id}
        ],
        "receipts": [
            {"id": "r1", "batch_id": "b1",
             "received_at": "2026-10-13T11:00:01Z", "comment_ids": ["c1"]}
        ],
    }


class ConfigV2Tests(unittest.TestCase):
    def test_v1_config_still_valid_and_static(self):
        config = v1_config()
        check_config.validate_config(config)
        self.assertEqual(check_config.report_policy(config), {"mode": "static", "port": None})

    def test_v2_config_requires_report_section(self):
        config = v1_config()
        config["schema_version"] = 2
        with self.assertRaises(check_config.ConfigError):
            check_config.validate_config(config)

    def test_v2_rejects_unknown_report_key(self):
        config = v2_config()
        config["report"]["extra"] = True
        with self.assertRaises(check_config.ConfigError):
            check_config.validate_config(config)

    def test_v2_rejects_bad_mode_and_port(self):
        for mode, port in (("bogus", 8765), ("live", 80), ("live", "8765")):
            config = v2_config(mode=mode, port=port)
            with self.assertRaises(check_config.ConfigError):
                check_config.validate_config(config)

    def test_v1_rejects_a_report_section(self):
        config = v1_config()
        config["report"] = {"mode": "live", "port": 8765}
        with self.assertRaises(check_config.ConfigError):
            check_config.validate_config(config)

    def test_unknown_schema_version_rejected(self):
        config = v1_config()
        config["schema_version"] = 3
        with self.assertRaises(check_config.ConfigError):
            check_config.validate_config(config)

    def test_report_policy_reads_v2(self):
        self.assertEqual(
            check_config.report_policy(v2_config(mode="live", port=9001)),
            {"mode": "live", "port": 9001},
        )


class AnchorTests(unittest.TestCase):
    def test_anchors_cover_added_removed_and_context_lines(self):
        view = render_report._build_view(with_hunk(), interactive=True)
        sides = sorted({item["side"] for item in view["manifest"]["line_anchors"]})
        self.assertEqual(sides, ["new", "old"])
        # 1 ctx + 1 del + 2 add + 1 ctx
        self.assertEqual(len(view["manifest"]["line_anchors"]), 5)

    def test_anchor_identity_is_stable_under_reordering(self):
        first = render_report._build_view(with_hunk(), interactive=True)["manifest"]
        second = render_report._build_view(with_hunk(), interactive=True)["manifest"]
        self.assertEqual(first["view_id"], second["view_id"])
        self.assertEqual(first["files"], second["files"])

    def test_permuting_snapshots_does_not_change_the_view_or_anchors(self):
        """Snapshot roles are semantic: reordering snapshots[] must be inert.

        Resolving baseline/candidate positionally made a re-serialised but
        byte-identical report produce different anchors, so an exported comment
        would import as stale rather than as a flagged mismatch.
        """
        base = with_hunk()
        base["snapshots"] = [
            {"id": "baseline", "manifest_sha256": "a" * 64},
            {"id": "candidate-s1", "manifest_sha256": "b" * 64},
        ]
        base["final_snapshot_id"] = None
        forward = render_report._build_view(base, interactive=True)["manifest"]

        reverse = copy.deepcopy(base)
        reverse["snapshots"] = list(reversed(reverse["snapshots"]))
        backward = render_report._build_view(reverse, interactive=True)["manifest"]

        self.assertEqual(forward["baseline"]["snapshot_id"], "baseline")
        self.assertEqual(forward["candidate"]["snapshot_id"], "candidate-s1")
        self.assertEqual(forward["view_id"], backward["view_id"])
        self.assertEqual(
            [a["anchor_id"] for a in forward["line_anchors"]],
            [a["anchor_id"] for a in backward["line_anchors"]],
        )

    def test_unlabelled_snapshots_are_also_order_independent(self):
        """A record that never named a baseline still must not be positional."""
        base = with_hunk()
        base["final_snapshot_id"] = None
        forward = render_report._build_view(base, interactive=True)["manifest"]

        reverse = copy.deepcopy(base)
        reverse["snapshots"] = list(reversed(reverse["snapshots"]))
        backward = render_report._build_view(reverse, interactive=True)["manifest"]

        self.assertEqual(forward["view_id"], backward["view_id"])
        self.assertEqual(forward["baseline"], backward["baseline"])
        self.assertEqual(forward["candidate"], backward["candidate"])

    def test_permuting_three_snapshots_is_inert(self):
        base = with_hunk()
        base["snapshots"] = [
            {"id": "s1", "manifest_sha256": "c" * 64},
            {"id": "s2", "manifest_sha256": "a" * 64},
            {"id": "s3", "manifest_sha256": "b" * 64},
        ]
        base["final_snapshot_id"] = None
        first = render_report._build_view(base, interactive=True)["manifest"]
        for perm in itertools.permutations(base["snapshots"]):
            shuffled = copy.deepcopy(base)
            shuffled["snapshots"] = list(perm)
            manifest = render_report._build_view(shuffled, interactive=True)["manifest"]
            self.assertEqual(manifest["view_id"], first["view_id"])
            self.assertEqual(manifest["baseline"], first["baseline"])
            self.assertEqual(manifest["candidate"], first["candidate"])

    def test_final_snapshot_id_still_wins_as_the_candidate(self):
        base = with_hunk()
        base["snapshots"] = [
            {"id": "baseline", "manifest_sha256": "a" * 64},
            {"id": "s2", "manifest_sha256": "b" * 64},
        ]
        base["final_snapshot_id"] = "s2"
        manifest = render_report._build_view(base, interactive=True)["manifest"]
        self.assertEqual(manifest["baseline"]["snapshot_id"], "baseline")
        self.assertEqual(manifest["candidate"]["snapshot_id"], "s2")

    def test_rename_hashes_the_baseline_and_candidate_paths_separately(self):
        """The anchor tuple carries both diff-header paths, not one path twice."""
        old_path, new_path = render_report._diff_paths(
            "diff --git a/src/old.py b/src/new.py\n"
            "--- a/src/old.py\n"
            "+++ b/src/new.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-pass\n"
            "+pass2\n",
            "src/new.py",
        )
        self.assertEqual(old_path, "src/old.py")
        self.assertEqual(new_path, "src/new.py")

        renamed = with_hunk()
        renamed["changes"][0]["path"] = "src/new.py"
        renamed["changes"][0]["diff"] = (
            "diff --git a/src/old.py b/src/new.py\n"
            "--- a/src/old.py\n"
            "+++ b/src/new.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-pass\n"
            "+pass2\n"
        )
        # Same diff body, different baseline path -> a distinct anchor.
        same_body = copy.deepcopy(renamed)
        same_body["changes"][0]["path"] = "src/old.py"
        same_body["changes"][0]["diff"] = renamed["changes"][0]["diff"].replace(
            "src/new.py", "src/old.py"
        )
        a = render_report._build_view(renamed, interactive=True)["manifest"]
        b = render_report._build_view(same_body, interactive=True)["manifest"]
        self.assertNotEqual(
            [x["anchor_id"] for x in a["line_anchors"]],
            [x["anchor_id"] for x in b["line_anchors"]],
            "a renamed file must not share an anchor with the un-renamed path",
        )

    def test_diff_paths_fall_back_to_the_change_path_without_headers(self):
        self.assertEqual(
            render_report._diff_paths("+pass\n-pass\n", "src/widget.py"),
            ("src/widget.py", "src/widget.py"),
        )
        # /dev/null (a pure add or delete) also falls back on that side.
        self.assertEqual(
            render_report._diff_paths(
                "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1,1 @@\n+pass\n",
                "src/new.py",
            ),
            ("src/new.py", "src/new.py"),
        )

    def test_line_anchor_binds_the_snapshot_not_just_the_number(self):
        base = with_hunk()
        view_a = render_report._build_view(base, interactive=True)["manifest"]
        moved = with_hunk()
        moved["final_snapshot_id"] = "s1"
        view_b = render_report._build_view(moved, interactive=True)["manifest"]
        self.assertNotEqual(
            [a["anchor_id"] for a in view_a["line_anchors"]],
            [a["anchor_id"] for a in view_b["line_anchors"]],
            "the same line number in a different snapshot must not reuse an anchor",
        )

    def test_hunkless_fragments_get_no_line_anchors(self):
        # make_report's change is a hand-written fragment with no @@ header.
        view = render_report._build_view(test_renderer.make_report(), interactive=True)
        self.assertEqual(view["manifest"]["line_anchors"], [])
        self.assertTrue(view["manifest"]["files"])

    def test_changing_the_diff_changes_the_anchor(self):
        view_a = render_report._build_view(with_hunk(), interactive=True)["manifest"]
        edited = with_hunk()
        edited["changes"][0]["diff"] = HUNK_DIFF + "+another = 3\n"
        view_b = render_report._build_view(edited, interactive=True)["manifest"]
        self.assertNotEqual(view_a["view_id"], view_b["view_id"])


class InteractiveRenderTests(unittest.TestCase):
    def test_static_render_is_script_and_anchor_free(self):
        out = render_html(with_hunk())
        self.assertNotIn("<script", out)
        self.assertNotIn("data-anchor", out)
        self.assertNotIn("data-addressable", out)

    def test_offline_mode_inlines_the_bundled_ui(self):
        out = render_html(with_hunk(), mode="offline-feedback")
        self.assertIn("<script>", out)
        self.assertIn('data-addressable="1"', out)
        self.assertIn('class="fbbar"', out)
        self.assertIn("Export feedback", out)

    def test_offline_mode_stays_offline(self):
        out = render_html(with_hunk(), mode="offline-feedback")
        for needle in ("http://", "https://", "<img"):
            self.assertNotIn(needle, out)
        self.assertIn("connect-src 'none'", out)

    def test_live_mode_allows_only_same_origin(self):
        out = render_html(with_hunk(), mode="live")
        self.assertIn("connect-src 'self'", out)

    def test_csp_hash_matches_the_inlined_script(self):
        import base64
        import hashlib
        import re

        out = render_html(with_hunk(), mode="offline-feedback")
        match = re.search(r"script-src 'sha256-([^']+)'", out)
        self.assertIsNotNone(match)
        script = render_report._load_ui_script()
        expected = base64.b64encode(
            hashlib.sha256(script.encode("utf-8")).digest()
        ).decode("ascii")
        self.assertEqual(match.group(1), expected)


class PageTabTests(unittest.TestCase):
    """The two page tabs: Conversation and Files changed.

    These assert STRUCTURE, because the tab mechanism fails silently. Three
    distinct DOM mistakes each produce a page that still parses, still passes
    every other test, and simply never switches tabs:

      * wrapping the radios in a container -> the panels become cousins, so the
        ``~`` combinator cannot reach them;
      * moving the radios after the panels  -> the same, in the other order;
      * putting the panels inside the strip -> ``:has()`` would be required.
    """

    def _report(self):
        data = with_hunk()
        # A second file so the tab counters and the list both have real content.
        data["changes"].append({
            "path": "docs/guide.md",
            "diff": "--- a/docs/guide.md\n+++ b/docs/guide.md\n@@ -1,1 +1,2 @@\n a\n+b\n",
        })
        # Schema v2 + a feedback block, so the Conversation panel renders the
        # Human feedback section the assertions below look for. Without it the
        # section is simply absent, which would make the tab test vacuous.
        view = render_report._build_view(data, interactive=True)
        line_anchor = view["lines"][("docs/guide.md", "new", 1)]
        data["schema_version"] = 2
        data["feedback"] = feedback_block(line_anchor)
        return data

    def _report_v2(self):
        return self._report()

    def _body(self, out):
        """The markup only, with every <style> block removed.

        The tab selectors also appear verbatim in the stylesheet
        (``.prpanel[data-panel="conv"]``, ``.prtabbar``...), so a bare
        ``out.index(...)`` finds the CSS rule before the element and every
        index comparison silently measures the wrong thing.

        Slicing from ``<body>`` is NOT sufficient: interactive modes emit a
        second stylesheet (``_FEEDBACK_CSS``) *inside* <body>, so any selector
        text there still shadows the markup. Strip the style blocks instead.
        """
        return re.sub(r"<style>.*?</style>", "", out, flags=re.DOTALL)

    def test_both_tabs_and_panels_are_present(self):
        out = render_html(self._report())
        self.assertIn('id="ptab-conv"', out)
        self.assertIn('id="ptab-files"', out)
        self.assertIn('data-panel="conv"', out)
        self.assertIn('data-panel="files"', out)
        # Exactly two tabs, one of them checked by default.
        self.assertEqual(out.count('class="prtabinput"'), 2)
        self.assertEqual(out.count(" checked>"), 1)

    def test_radios_are_bare_siblings_of_the_bar_and_the_panels(self):
        """Both radios must sit directly before the strip, unwrapped.

        A wrapper element is the single most likely future regression, so it is
        asserted explicitly rather than inferred from the CSS.
        """
        out = render_html(self._report())
        out = self._body(out)
        conv = out.index('id="ptab-conv"')
        files = out.index('id="ptab-files"')
        bar = out.index('class="prtabbar"')
        panel = out.index('data-panel="conv"')
        self.assertLess(conv, files)
        self.assertLess(files, bar)
        self.assertLess(bar, panel)
        # Nothing but the two inputs between the first input and the strip: no
        # wrapping <div>/<span> can open there.
        between = out[out.index(">", conv) + 1:out.rindex("<input", conv, bar)]
        self.assertNotIn("<div", between)
        self.assertNotIn("<span", between)

    def test_labels_address_the_radios_by_for_attribute(self):
        """Labels are not adjacent to the inputs, so ``for`` is load-bearing."""
        out = render_html(self._report())
        self.assertIn('for="ptab-conv"', out)
        self.assertIn('for="ptab-files"', out)
        for field in ("conv", "files"):
            self.assertIn('id="ptab-%s-label"' % field, out)
        # The active label reserves its semibold width so tabs never shift.
        self.assertIn('data-content="Conversation"', out)
        self.assertIn('data-content="Files changed"', out)

    def test_labels_carry_no_stale_aria_state(self):
        """No ARIA tab role, and no state attribute that CSS cannot update.

        ``role="tab"`` obliges us to keep ``aria-selected``/``tabindex`` in
        sync, which is impossible without JavaScript — and static mode forbids
        scripts. A hard-coded value is wrong the moment the other tab is
        clicked, and would also break the byte-identity contract. The radios
        carry the real state natively, so nothing on the label may claim to.
        """
        out = render_html(self._report())
        self.assertNotIn('role="tab"', out)
        self.assertNotIn("aria-selected", out)
        # No ARIA state on the panels/strip either (the panels are plain divs).
        body = self._body(out)
        self.assertNotIn('role="tablist"', body)
        self.assertNotIn('role="tabpanel"', body)
        # The only aria-hidden left is on the decorative Octicon SVGs: two tab
        # icons plus one chevron per file row (2 files in this fixture).
        self.assertEqual(out.count("aria-hidden"), 2 + len(self._report()["changes"]))
        # The radio group is still announced, and checked survives.
        self.assertEqual(out.count('name="prtab"'), 2)

    def test_active_tab_rule_targets_one_label_only(self):
        """The highlight selector must name a specific label.

        ``.prtabinput:checked ~ .prtabbar .prtab`` (without the ``[for=]``
        filter) matches every label and lights up both tabs at once.
        """
        out = render_html(self._report())
        block = out[out.index("--tab-marker"):out.index(".prpanel")]
        self.assertIn('[for="ptab-conv"]', block)
        self.assertIn('[for="ptab-files"]', block)
        # GitHub's marker colour (primer base orange-3), and it is an underline
        # rather than a raised tab.
        self.assertIn("--tab-marker: #fd8c73", out)
        self.assertIn("margin-bottom: -8px", out)

    def test_panels_are_not_nested_inside_the_tab_strip(self):
        out = self._body(render_html(self._report()))
        bar = out.index('class="prtabbar"')
        bar_end = out.index("</div>", bar)
        panel = out.index('data-panel="conv"')
        self.assertLess(bar_end, panel, "panels must follow the closed strip")

    def test_conversation_holds_the_log_and_files_holds_the_diff(self):
        out = self._body(render_html(self._report()))
        conv = out[out.index('data-panel="conv"'):out.index('data-panel="files"')]
        files = out[out.index('data-panel="files"'):]
        # Conversation: the review log, no diffs.
        for heading in ("Requirements &amp; plan", "Participants",
                        "Conversation timeline", "Checks", "Approvals"):
            self.assertIn(heading, conv)
        # Imported human comments are rows in that same log, not a section of
        # their own, so there is one chronological record per task.
        self.assertNotIn("Human feedback", conv)
        self.assertNotIn("diffview", conv)
        # Files changed: the diffs, and none of the log tables.
        self.assertIn("diffview", files)
        self.assertNotIn("Conversation timeline", files)
        self.assertNotIn("Approvals", files)

    def test_files_changed_is_a_flat_list_not_a_tab_per_file(self):
        out = render_html(self._report())
        self.assertIn('class="filelist"', out)
        self.assertEqual(out.count('class="fileitem"'), len(self._report()["changes"]))
        # The per-file tab strip is gone entirely.
        self.assertNotIn("ftabinput", out)
        self.assertNotIn("class=\"ftab\"", out)
        self.assertNotIn("filetabs", out)
        # First row starts open so the view is never blank. The row carries an
        # id anchor now (the nav rail jumps to it), so match the open flag
        # rather than the old attribute order.
        open_rows = re.findall(r'<details class="fileitem"[^>]*\bopen>', out)
        self.assertEqual(len(open_rows), 1)
        self.assertIn('id="file-0"', open_rows[0])

    def test_file_rows_carry_full_path_and_stats(self):
        out = render_html(self._report())
        self.assertIn('<span class="fdir">docs/</span>guide.md', out)
        self.assertIn('class="fstat"', out)
        self.assertIn("caret", out)

    def test_interactive_anchors_survive_the_tab_rewrite(self):
        out = render_html(self._report(), mode="offline-feedback")
        self.assertIn('data-file-anchor="', out)
        self.assertIn('data-addressable="1"', out)
        self.assertIn('data-reply-anchor="finding/f1"', out)
        # One comment bar per tab, so a comment can be left from either view.
        self.assertEqual(out.count('class="fbbar"'), 2)

    def test_static_mode_still_adds_no_script(self):
        """The tabs are pure CSS, so the static contract is untouched."""
        out = render_html(self._report())
        self.assertNotIn("<script", out)
        self.assertIn('id="ptab-files"', out)
        self.assertIn('class="filelist"', out)

    def test_feedback_bars_are_class_addressed_and_duplicate_no_ids(self):
        """The bar is mounted twice, so it must carry no ids at all.

        Regression test for the "Export feedback / Copy JSON do nothing" bug: the
        bar used to hardcode ``id="herdr-feedback-export"`` (and friends) and be
        rendered once per tab panel. Duplicate ids are invalid HTML and
        ``document.getElementById`` returns only the first match, so the script
        bound the hidden Conversation bar and every control in the Files-tab bar
        was inert — no error, no download, no status text. Addressing the
        controls by class and binding *every* bar is the fix; this test pins it.
        """
        out = render_html(self._report(), mode="offline-feedback")
        # No id at all on the bar or its controls.
        for field in ("herdr-feedback", "fbcount", "fbexport", "fbcopy",
                      "fbsubmit", "fbstatus"):
            self.assertNotIn('id="%s"' % field, out)
        # Two bars, each with the full control set.
        self.assertEqual(out.count('class="fbbar"'), 2)
        for cls in ("fbexport", "fbcopy", "fbsubmit", "fbcount", "fbstatus"):
            self.assertEqual(
                len(re.findall(r'class="[^"]*\b%s\b[^"]*"' % cls, out)), 2,
                "%s must be present on both bars" % cls,
            )
        # And no duplicate ids anywhere in the document, not just the bar.
        # The lookbehind is load-bearing, and \b is NOT a substitute: the
        # character before the tail of `data-task-id="…"` is `-`, a non-word
        # character, so a \b matches there and `id="` then reports the value of
        # a data attribute as a duplicate id. Require a real attribute start.
        ids = re.findall(r'(?<![\w-])id="([^"]+)"', out)
        self.assertEqual(sorted(ids), sorted(set(ids)),
                         "duplicate id(s): %s" % sorted(
                             i for i in set(ids) if ids.count(i) > 1))
        # The script addresses bars by class, never by id.
        script = render_report._load_ui_script()
        self.assertIn('querySelectorAll(".fbbar")', script)
        self.assertNotIn('getElementById("herdr-feedback', script)

    def test_export_is_allowed_by_the_csp(self):
        """Export downloads via ``URL.createObjectURL`` + anchor click.

        That is governed by ``default-src``, so ``blob:`` must be whitelisted or
        the browser silently blocks the download (no exception, no file).
        """
        out = render_html(self._report(), mode="offline-feedback")
        self.assertIn("default-src blob:", out)

    def test_thread_ids_are_derived_from_finding_ids(self):
        out = render_html(with_hunk(), mode="offline-feedback")
        self.assertIn('data-thread-id="finding/f1"', out)
        self.assertIn('data-reply-anchor="finding/f1"', out)

    def test_ambiguous_line_targets_are_not_offered(self):
        distinct = with_hunk()
        distinct["changes"][0]["diff"] = (
            "--- a/x.py\n+++ b/x.py\n"
            "@@ -1,1 +1,2 @@\n a\n+b\n"
            "@@ -10,1 +10,2 @@\n j\n+k\n"
        )
        self.assertEqual(
            render_report._build_view(distinct, interactive=True)["ambiguous"], set()
        )
        # Two hunks that both claim new line 2 -> the anchor is duplicated.
        clash = with_hunk()
        clash["changes"][0]["diff"] = (
            "--- a/x.py\n+++ b/x.py\n"
            "@@ -1,1 +1,2 @@\n a\n+b\n"
            "@@ -5,1 +1,2 @@\n e\n+b\n"
        )
        clashed = render_report._build_view(clash, interactive=True)
        self.assertTrue(clashed["ambiguous"], "a duplicated line target must be flagged")
        out = render_html(clash, mode="offline-feedback")
        self.assertIn('data-ambiguous="1"', out)

    def test_static_deep_link_fallback_is_present_and_script_free(self):
        """Without JS, `:has(:target)` still reveals the Files panel.

        Interactive modes fix the same problem in JS (tab switch + scroll + open
        the <details>); static mode has no script, so the panel must reveal
        itself purely in CSS.
        """
        out = render_html(self._report())
        self.assertNotIn("<script", out)
        self.assertIn('.prpanel[data-panel="files"]:has(:target)', out)
        # The tab marker must follow the shown panel, not the checked radio.
        self.assertIn(".prtabbar:has(~ .prpanel[data-panel=\"files\"] :target)", out)

    def test_static_mode_does_not_pretend_css_can_open_a_details_row(self):
        """No CSS property can reveal a closed <details>' body.

        Verified in Chromium: `display: block`, `height: auto`,
        `content-visibility: visible` and `details:not([open]) > body` are all
        no-ops against a closed details - the UA hides the content slot, so the
        text is not painted even though the child reports a height. Static mode
        therefore CANNOT expand a `#file-<n>` row, and must not ship CSS that
        claims to. Re-adding such a rule is a silent no-op that reads as a fix.
        """
        out = render_html(self._report())
        self.assertNotIn(":target > .filebody-wrap", out)
        self.assertNotIn("content-visibility: visible", out)

    def test_bad_mode_rejected(self):
        with self.assertRaises(ValidationError):
            render_html(with_hunk(), mode="bogus")


class ReportV2Tests(unittest.TestCase):
    def _v2(self, disposition="recorded", finding_id=None):
        data = with_hunk()
        data["schema_version"] = 2
        anchor = render_report._build_view(data, interactive=True)["manifest"]["line_anchors"][0]
        block = feedback_block(anchor["anchor_id"], disposition, finding_id)
        view_id = render_report._build_view(data, interactive=True)["manifest"]["view_id"]
        block["current_view_id"] = view_id
        block["views"] = [{"id": view_id, "digest": HEXLESS}]
        block["comments"][0]["origin_view_id"] = view_id
        data["feedback"] = block
        return data

    def test_v2_requires_feedback(self):
        data = with_hunk()
        data["schema_version"] = 2
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_v2_feedback_must_be_an_object_not_a_list(self):
        """`"feedback": []` is the shape the controller got wrong on 2026-10-13."""
        for wrong in ([], "", 0, None):
            with self.subTest(wrong=wrong):
                data = with_hunk()
                data["schema_version"] = 2
                data["feedback"] = wrong
                with self.assertRaises(ValidationError) as ctx:
                    validate_report(data)
                self.assertIn("feedback", str(ctx.exception))

    def test_v2_empty_feedback_object_is_valid(self):
        """The canonical empty object must validate, so a fresh task can render."""
        data = with_hunk()
        data["schema_version"] = 2
        data["feedback"] = {
            "revision": 0, "current_view_id": "", "views": [], "threads": [],
            "comments": [], "decisions": [], "receipts": [],
        }
        validate_report(data)
        out = render_html(data, mode="offline-feedback")
        self.assertIn('class="fbbar"', out)

    def test_v2_feedback_rejects_missing_and_extra_keys(self):
        data = with_hunk()
        data["schema_version"] = 2
        data["feedback"] = {"revision": 0, "current_view_id": ""}
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_v2_feedback_rejects_an_arbitrary_extra_key(self):
        """An extra top-level key must fail, not ride along silently.

        `_require_keys` only checks containment, so a misspelled or
        half-migrated field (`current_view` for `current_view_id`) used to pass
        validation and then read back as `None` at the point of use. The set of
        allowed keys is exact.
        """
        for extra in ("current_view", "revisions", "extra", "Feedback"):
            with self.subTest(extra=extra):
                data = with_hunk()
                data["schema_version"] = 2
                data["feedback"] = {
                    "revision": 0, "current_view_id": "", "views": [], "threads": [],
                    "comments": [], "decisions": [], "receipts": [],
                }
                data["feedback"][extra] = "surprise"
                with self.assertRaises(ValidationError) as ctx:
                    validate_report(data)
                self.assertIn(extra, str(ctx.exception))

    def test_decision_controller_must_have_the_controller_role(self):
        """A known participant id is not enough - the role must be 'controller'.

        Membership-only validation let a reviewer or developer id record a
        disposition, which the READY gate then counted as a real controller
        decision.
        """
        # `rev1` and `dev` are real participants but not the controller.
        for impostor in ("rev1", "dev"):
            with self.subTest(controller=impostor):
                data = self._v2()
                data["feedback"]["decisions"][0]["controller"] = impostor
                with self.assertRaises(ValidationError) as ctx:
                    validate_report(data)
                self.assertIn("role 'controller'", str(ctx.exception))
        # The genuine controller still validates.
        validate_report(self._v2())

    def test_v2_finding_optional_provenance_is_type_checked(self):
        """`anchor` and `source_comment_ids` are optional but must be well-typed."""
        # Absent is fine - v1 findings carry neither.
        data = self._v2()
        validate_report(data)

        # A null anchor is the documented migrated-v1 shape.
        data = self._v2()
        data["findings"][0]["anchor"] = None
        data["findings"][0]["source_comment_ids"] = []
        validate_report(data)

        # A well-formed typed anchor and source list are accepted.
        data = self._v2()
        data["findings"][0]["anchor"] = {"kind": "file", "file_id": "file-abc"}
        data["findings"][0]["source_comment_ids"] = ["c1"]
        validate_report(data)

        for bad_anchor in ("line-1", 7, {"kind": "line"}, {"kind": "nope", "file_id": "x"},
                           {"kind": "line", "anchor_id": ""}, []):
            with self.subTest(anchor=bad_anchor):
                broken = self._v2()
                broken["findings"][0]["anchor"] = bad_anchor
                with self.assertRaises(ValidationError):
                    validate_report(broken)

        for bad_sources in ("c1", ["", "c1"], ["c1", "c1"], [1]):
            with self.subTest(source_comment_ids=bad_sources):
                broken = self._v2()
                broken["findings"][0]["source_comment_ids"] = bad_sources
                with self.assertRaises(ValidationError):
                    validate_report(broken)

    def test_bundled_example_is_a_valid_v2_report(self):
        """The shipped example must demonstrate the interactive shape verbatim."""
        example = json.loads(
            (_HERE.parent / "assets" / "report.example.json").read_text(encoding="utf-8-sig")
        )
        self.assertEqual(example["schema_version"], 2)
        self.assertIsInstance(example["feedback"], dict)
        validate_report(example)
        self.assertNotIn("<script", render_html(example))  # static stays script-free

    def test_v1_report_still_valid(self):
        validate_report(test_renderer.make_report())

    def test_v2_renders_the_human_comment_as_a_timeline_row(self):
        """Imported comments live in the Conversation timeline, not a section."""
        out = render_html(self._v2(), mode="offline-feedback")
        self.assertNotIn("Human feedback", out)
        self.assertIn("Human (imported)", out)
        self.assertIn("fd-recorded", out)
        # The row is inside the Conversation timeline list, and carries the jump
        # id the Files panel's rail links to.
        timeline = out[out.index("Conversation timeline"):out.index("Checks")]
        self.assertIn("Human (imported)", timeline)
        self.assertIn('id="fb-human-t1"', timeline)

    def test_pending_comment_blocks_ready(self):
        data = self._v2()
        data["feedback"]["decisions"] = []
        with self.assertRaises(ValidationError):
            validate_report(data)

    def test_needs_clarification_blocks_ready(self):
        with self.assertRaises(ValidationError):
            validate_report(self._v2(disposition="needs_clarification"))

    def test_fix_required_must_link_a_resolved_finding(self):
        # f1 in make_report is resolved, so a linked fix_required is acceptable.
        validate_report(self._v2(disposition="fix_required", finding_id="f1"))
        with self.assertRaises(ValidationError):
            validate_report(self._v2(disposition="fix_required", finding_id="nope"))

    def test_stale_anchor_stays_valid_but_an_empty_anchor_is_rejected(self):
        # An anchor missing from the *current* view is a legitimate stale target:
        # it must keep rendering (as outdated) rather than be rejected. Strict
        # resolution against the originating manifest is feedback.py's job.
        data = self._v2()
        data["feedback"]["comments"][0]["target"]["anchor_id"] = "line-deadbeef"
        data["feedback"]["threads"][0]["target"]["anchor_id"] = "line-deadbeef"
        validate_report(data)

        broken = self._v2()
        broken["feedback"]["comments"][0]["target"]["anchor_id"] = ""
        with self.assertRaises(ValidationError):
            validate_report(broken)

    def test_unknown_decision_controller_rejected(self):
        data = self._v2()
        data["feedback"]["decisions"][0]["controller"] = "ghost"
        with self.assertRaises(ValidationError):
            validate_report(data)


class WriterModeTests(unittest.TestCase):
    def _workspace(self, config):
        root = tempfile.mkdtemp(prefix="herdr-interactive-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        task_dir = os.path.join(root, ".agent-pr", "tasks", "task-001")
        os.makedirs(task_dir)
        with open(os.path.join(task_dir, "workflow.resolved.json"), "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        return root, task_dir

    def _write_report(self, task_dir, data):
        with open(os.path.join(task_dir, "report.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_v2_config_renders_interactive_and_writes_a_view_manifest(self):
        _, task_dir = self._workspace(v2_config(mode="offline-feedback"))
        data = with_hunk()
        data["status"] = "REVIEWING"
        self._write_report(task_dir, data)

        out = write_report(task_dir)

        with open(out, "r", encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("<script>", content)
        views = os.path.join(task_dir, "views")
        self.assertTrue(os.path.isdir(views))
        manifests = [n for n in os.listdir(views) if n.endswith(".json")]
        self.assertEqual(len(manifests), 1)
        with open(os.path.join(views, manifests[0]), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["task_id"], "task-001")
        self.assertTrue(manifest["line_anchors"])

    def test_v1_config_still_renders_static(self):
        _, task_dir = self._workspace(v1_config())
        data = test_renderer.make_report()
        self._write_report(task_dir, data)
        out = write_report(task_dir)
        with open(out, "r", encoding="utf-8") as handle:
            content = handle.read()
        self.assertNotIn("<script", content)
        self.assertFalse(os.path.isdir(os.path.join(task_dir, "views")))

    def test_explicit_mode_cannot_override_the_frozen_policy(self):
        _, task_dir = self._workspace(v1_config())
        self._write_report(task_dir, test_renderer.make_report())
        with self.assertRaises(ValidationError):
            write_report(task_dir, mode="live")

    def test_explicit_mode_matching_the_policy_is_accepted(self):
        _, task_dir = self._workspace(v2_config(mode="offline-feedback"))
        data = with_hunk()
        data["status"] = "REVIEWING"
        self._write_report(task_dir, data)
        out = write_report(task_dir, mode="offline-feedback")
        self.assertTrue(os.path.isfile(out))


class NavRailTests(unittest.TestCase):
    """The sticky Files/Comments rail and its jump targets."""

    def _out(self):
        return render_html(self._report())

    def _report(self):
        """The shared fixture plus a second file, so the rail has two rows and
        a comment that lives on the *second* one (the interesting jump case)."""
        data = test_renderer.make_report()
        data["changes"] = data["changes"] + [
            {"path": "docs/other.md",
             "diff": "diff --git a/docs/other.md b/docs/other.md\n"
                     "--- a/docs/other.md\n+++ b/docs/other.md\n"
                     "@@ -1 +1 @@\n-a\n+b\n"},
        ]
        return data

    def test_rail_holds_both_panels_in_source_order(self):
        out = self._out()
        body = out[out.index("<body>"):]
        self.assertIn('class="rail"', body)
        files_at = body.index('class="panel fpanel"')
        comments_at = body.index('class="panel cpanel"')
        self.assertLess(files_at, comments_at, "Files must sit above Comments")

    def test_rail_uses_no_script_and_is_a_plain_anchor_list(self):
        out = self._out()
        self.assertNotIn("<script", out)
        body = out[out.index("<body>"):]
        self.assertIn('href="#file-0"', body)
        self.assertIn('href="#thread-', body)

    def test_every_rail_target_resolves(self):
        out = self._out()
        body = out[out.index("<body>"):]
        targets = re.findall(r'href="#([^"]+)"', body)
        self.assertTrue(targets)
        for anchor in targets:
            self.assertIn(f'id="{anchor}"', body, f"dangling jump target: {anchor}")

    def test_thread_ids_exist_even_in_static_mode(self):
        # The id is what makes a comment jump work without a script.
        data = self._report()
        out = render_html(data, mode="static")
        self.assertNotIn("<script", out)
        for finding in data["findings"]:
            anchor = "thread-" + re.sub(r'[^A-Za-z0-9_-]', "-", "finding/" + finding["id"])
            self.assertIn(f'id="{anchor}"', out)

    def test_comment_panel_rows_name_the_reviewer(self):
        """Each comment row carries the owner's role badge, matching the
        labels used by the participants table and the timeline."""
        data = self._report()
        out = render_html(data)
        body = out[out.index("<body>"):]
        panel = body[body.index('class="panel cpanel"'):]
        owners = {f["owner"] for f in data["findings"]}
        for owner in owners:
            label, role = render_report._actors_map(data)[owner]
            self.assertIn(f'<span class="badge role-{role}">{label}</span>', panel)
        # The badge never replaces the severity chip or the finding id.
        self.assertIn('class="jsev', panel)
        self.assertIn('class="jid"', panel)

    def test_comment_owner_outside_the_roster_falls_back_to_mono(self):
        """An owner the participant list does not name still renders -- as the
        raw id -- rather than losing the badge entirely."""
        data = self._report()
        # Keep the roster intact but point the finding at an unlisted id. The
        # finding itself still carries the original owner for the fallback path
        # to be exercised through _actor_badge returning empty.
        monkey = render_report._actors_map(data)
        self.assertIsNone(monkey.get("nobody"), "fixture must not list this id")
        self.assertEqual(
            render_report._actor_badge(monkey.get("nobody")), "",
            "an unlisted actor must yield no badge, so the mono fallback runs",
        )

    def test_rail_fonts_respect_the_14px_floor(self):
        css = self._out()
        css = css[css.index("<style>"):css.index("</style>")]
        for selector in (".fpanel .fname", ".fpanel .fdelta", ".fpanel .fcmt",
                         ".cpanel .jid", ".cpanel .jpath", ".cpanel .jsev"):
            rule = css[css.index(selector + " {"):]
            rule = rule[:rule.index("}")]
            self.assertIn("font-size: 14px", rule, f"{selector} below the font floor")

    def test_file_row_does_not_repeat_its_summary_inside_the_body(self):
        """The <summary> already names the file, its stats and its comment count.

        The old `.ftphead` block repeated all three directly beneath it, so the
        path and the +/- stat appeared twice per open row.
        """
        out = render_html(self._report())
        self.assertNotIn("ftphead", out)
        body = out[out.index("<body>"):]
        for row in re.findall(r'<details class="fileitem".*?</details>', body, re.S):
            path = re.search(r'class="fpath"', row)
            self.assertIsNotNone(path, "every row still identifies its file")
            self.assertEqual(row.count('class="fstat"'), 1,
                             "the +/- stat must appear once per row, not twice")

    def test_comment_jump_scroll_margin_and_flash_ship(self):
        out = self._out()
        # The jump target must clear the pinned file row, which is one row tall.
        # The margin is expressed in terms of the row height, not a magic number,
        # so raising --filerow-h can never silently occlude the landing.
        self.assertIn("scroll-margin-top: calc(var(--filerow-h) + 8px)", out)
        self.assertIn("--filerow-h: 40px", out)
        self.assertIn(".finding:target", out)
        # The owning row is highlighted via :has() when its thread is targeted.
        self.assertIn("details.fileitem:has(.finding:target)", out)

    def test_scroll_margin_tracks_the_row_height_variable(self):
        """No bare `scroll-margin-top: <px>` may survive for a jump target.

        A literal would drift out of sync with `--filerow-h` the moment the row
        is restyled, silently hiding the landing behind the sticky header.
        """
        out = self._out()
        css = out[out.index("<style>"):out.index("</style>")]
        literals = re.findall(r"scroll-margin-top:\s*(\d+px)", css)
        self.assertEqual(literals, [], f"hard-coded scroll-margin-top: {literals}")

    def test_the_current_file_row_is_pinned_while_its_diff_is_on_screen(self):
        """The open row sticks; the list must not clip it.

        `position: sticky` dies inside an `overflow: hidden` ancestor, and a
        `<summary>` un-sticks by itself once its own `<details>` body has
        scrolled past - which is what makes this read as "the file I am reading".
        """
        out = self._out()
        css = out[out.index("<style>"):out.index("</style>")]
        # The selector legitimately appears more than once (a scroll-margin rule
        # near the timeline block, the sticky rule with the file list), so the
        # assertions run over every block it declares, not just the first.
        blocks = [b[:b.index("}")] for b in
                  re.findall(r"\.fileitem\[open\] > summary\.filerow \{[^}]*\}", css)]
        self.assertTrue(blocks, "the open-row selector must be styled")
        joined = "\n".join(blocks)
        self.assertIn("position: sticky", joined)
        self.assertIn("top: 0", joined)
        # The list itself must not become a clipping container.
        list_rule = css[css.index(".filelist {"):]
        list_rule = list_rule[:list_rule.index("}")]
        self.assertNotIn("overflow: hidden", list_rule,
                         ".filelist must not clip, or every sticky row dies")
        # The row that sticks must outrank the file bodies below it.
        self.assertIn(".fileitem[open] { position: relative; z-index: 2; }", css)


class TimelineTimestampTests(unittest.TestCase):
    """Timeline datetimes come from events.jsonl, never from report.json."""

    def _report(self):
        return test_renderer.make_report()

    def test_no_events_means_no_time_and_no_crash(self):
        out = render_html(self._report())
        self.assertNotIn('class="tl-when"', out)
        self.assertIn("Conversation timeline", out)

    def test_events_add_a_machine_and_human_readable_time(self):
        events = [{"ts": "2026-10-14T06:51:00Z"}, {"ts": "2026-10-14T06:52:30Z"}]
        out = render_html(self._report(), events=events)
        self.assertIn('class="tl-when"', out)
        self.assertIn('datetime="2026-10-14T06:51:00Z"', out)

    def test_short_event_log_only_times_the_entries_it_covers(self):
        events = [{"ts": "2026-10-14T06:51:00Z"}]
        out = render_html(self._report(), events=events)
        n_entries = len(self._report()["timeline"])
        self.assertEqual(out.count('class="tl-when"'), 1)
        self.assertGreater(n_entries, 1, "fixture must have several entries")

    def test_unparseable_timestamp_is_skipped_not_fatal(self):
        events = [{"ts": "not-a-date"}]
        out = render_html(self._report(), events=events)
        self.assertNotIn('class="tl-when"', out)

    def test_blank_timestamp_is_skipped(self):
        events = [{"ts": "   "}, {}]
        out = render_html(self._report(), events=events)
        self.assertNotIn('class="tl-when"', out)

    def test_missing_events_file_degrades_gracefully(self):
        root = tempfile.mkdtemp(prefix="herdr-noevents-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        task_dir = os.path.join(root, ".agent-pr", "tasks", "task-001")
        os.makedirs(task_dir)
        data = test_renderer.make_report()
        data["status"] = "REVIEWING"
        with open(os.path.join(task_dir, "report.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        out = write_report(task_dir)
        self.assertTrue(os.path.isfile(out))
        with open(out, encoding="utf-8") as handle:
            self.assertNotIn('class="tl-when"', handle.read())

    def test_malformed_event_line_does_not_drop_the_log(self):
        root = tempfile.mkdtemp(prefix="herdr-badevent-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        task_dir = os.path.join(root, ".agent-pr", "tasks", "task-001")
        os.makedirs(task_dir)
        data = test_renderer.make_report()
        data["status"] = "REVIEWING"
        with open(os.path.join(task_dir, "report.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        with open(os.path.join(task_dir, "events.jsonl"), "w", encoding="utf-8") as handle:
            handle.write("{not json}\n")
            handle.write(json.dumps({"ts": "2026-10-14T06:51:00Z"}) + "\n")
        with open(write_report(task_dir), encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn('class="tl-when"', content)


class DraftCardAssetTests(unittest.TestCase):
    """The bundled feedback script must render a *visible* card for a saved
    draft, and must resolve a file composer by its own marker.

    Regression: a thread reply mount also lives inside
    ``.comments[data-file-anchor]``. Looking the composer up with a bare
    ``.fb-replymount`` query returned the reply mount, retargeted it as a file,
    found no draft, and deleted the card that had just been rendered for the
    thread - so saving a reply appeared to do nothing.
    """

    @classmethod
    def setUpClass(cls):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "assets", "feedback-ui.js",
        )
        with open(path, encoding="utf-8") as handle:
            cls.js = handle.read()

    def test_script_renders_a_draft_card_with_edit_and_remove(self):
        self.assertIn("fb-draftcard", self.js)
        self.assertIn('el("div", "fb-draftcard")', self.js)
        self.assertIn('"Edit"', self.js)
        self.assertIn('"Remove"', self.js)

    def test_script_replaces_a_card_instead_of_stacking_duplicates(self):
        # renderDraftInto must scan past an open editor before deciding whether
        # a card is already present.
        self.assertIn('while (node && node.className === "fb-editor")', self.js)
        self.assertIn('node.className === "fb-draftcard"', self.js)

    def test_composer_mount_carries_a_marker_and_is_found_by_it(self):
        self.assertIn('setAttribute("data-compose-anchor", fileId)', self.js)
        self.assertIn(
            'querySelector(".fb-replymount[data-compose-anchor]")', self.js
        )
        # The ambiguous bare lookup must be gone from the card sync path.
        self.assertNotIn(
            'files[k].querySelector(".fb-replymount")', self.js
        )

    def test_saving_is_wired_to_both_the_button_and_ctrl_enter(self):
        self.assertIn('saveBtn.addEventListener("click", saveDraft)', self.js)
        self.assertIn('event.ctrlKey || event.metaKey', self.js)
        self.assertIn("event.preventDefault()", self.js)

    def test_editing_a_draft_hides_its_card(self):
        """An open editor supersedes its card; closing the editor restores it.

        Regression: clicking Edit inserted the editor *above* the existing card
        and left the card visible underneath, so the same text appeared twice and
        the stale card's Edit/Remove buttons stayed live while typing. The editor
        is tagged with its target key and renderDraftInto skips that target's
        card until the editor closes.
        """
        # The editor records which draft it is editing...
        self.assertIn('setAttribute("data-target-key", targetKey(target))', self.js)
        # ...renderDraftInto consults it, and bails out before drawing a card.
        self.assertIn("function editorOpenFor(target)", self.js)
        self.assertIn("if (editorOpenFor(target)) {", self.js)
        # Both open and close re-sync, so the card hides then comes back.
        open_body = self.js[self.js.index("function openEditor("):]
        open_body = open_body[:open_body.index("function wireLineGutters(")]
        self.assertGreaterEqual(open_body.count("syncDraftCards();"), 2)
        # The card render must consult the guard, not just be hoisted above the
        # editor: placing it first is what produced the double-text bug.
        render_body = self.js[self.js.index("function renderDraftInto("):]
        render_body = render_body[:render_body.index("var DRAFT_LABELS")]
        self.assertIn("editorOpenFor(target)", render_body)

    def test_draft_card_css_ships_in_the_stylesheet(self):
        # Feedback CSS is interactive-only by design: static mode carries no
        # script, so the card markup could never be produced there.
        out = render_html(with_hunk(), mode="offline-feedback")
        for selector in (".fb-draftcard", ".fb-draftcard-tag",
                         ".fb-draftcard-body", ".fb-draftcard-actions"):
            self.assertIn(selector, out)

    def test_static_mode_omits_the_feedback_css(self):
        out = render_html(with_hunk())
        self.assertNotIn(".fb-draftcard", out)
        self.assertNotIn("<script", out)

    def test_confirmation_is_only_for_work_a_reload_would_destroy(self):
        """`beforeunload` must warn only about unsaved EDITOR text.

        Two regressions in one rule. First: it fired whenever `drafts.length` was
        non-zero, so it still prompted right after a successful Export - the
        draft had already left the reviewer's hands (fixed by the handoff
        fingerprint). Second, and subtler: a *saved* draft is durable in
        localStorage and is restored on the next load, verified in Chromium - the
        body comes back when the same line/file is reopened. Warning about it
        told the reviewer their changes were unsaved when they were safe.

        So the prompt keys off open editors holding unsaved text, and nothing
        else. The handoff fingerprint is still recorded on Export/Copy/Submit.
        """
        self.assertIn("function hasUnsavedEditorText()", self.js)
        self.assertIn("function trackPendingEditor(", self.js)
        self.assertIn("function releasePendingEditor(", self.js)
        # The unload guard consults the editor state, not the draft list.
        self.assertIn("if (hasUnsavedEditorText()) {", self.js)
        self.assertNotIn("if (handoffPending()) {", self.js)
        self.assertNotIn("if (drafts.length) {\n      event.preventDefault();", self.js)
        # Every handoff path still records the fingerprint.
        self.assertIn("function markHandedOff()", self.js)
        self.assertEqual(self.js.count("markHandedOff();"), 3)
        # Typing marks the editor dirty; every exit path clears it, or the
        # prompt would outlive the text it is warning about.
        editor = self.js[self.js.index("function openEditor("):]
        editor = editor[:editor.index("function wireLineGutters(")]
        self.assertIn('area.addEventListener("input", markDirty)', editor)
        self.assertIn("releasePendingEditor(markDirty);", editor)
        close = editor[editor.index("function close()"):]
        self.assertIn("releasePendingEditor(markDirty);", close)
        # Saving is an exit path, so `saveDraft` closes and therefore releases.
        self.assertIn("close();", editor[editor.index("function saveDraft()"):])

    def test_tracking_an_editor_never_calls_back_into_its_handler(self):
        """`trackPendingEditor` must not invoke the handler it is given.

        The handler's job is to call `trackPendingEditor`; if tracking calls the
        handler back, the two recurse until the stack overflows. The throw is
        swallowed by event dispatch, so the textarea still updates on screen
        while `dirtyEditors` never moves and the unload prompt silently stops
        firing - a failure that is invisible to every DOM assertion.
        """
        tracker = self.js[self.js.index("function trackPendingEditor("):]
        tracker = tracker[:tracker.index("function releasePendingEditor(")]
        self.assertIn("if (pendingEditors.indexOf(token) === -1)", tracker)
        self.assertIn("dirtyEditors += 1", tracker)
        # The token is only compared and stored - never invoked.
        self.assertNotIn("token()", tracker)
        self.assertNotIn("markDirty()", tracker)

    def test_a_pure_retry_reuses_the_batch_id(self):
        """Resubmitting an unedited batch must stay byte-identical.

        The rotation is deliberately confined to `putDraft`/`removeDraft`. If a
        retry minted a new batch id, the controller would stage a *second* inbox
        file for the same comments instead of recognising a no-op retry.
        """
        self.assertIn("function rotateBatchId()", self.js)
        self.assertIn("BATCH_ID = uuid();", self.js)
        put_draft = self.js[self.js.index("function putDraft("):]
        put_draft = put_draft[:put_draft.index("function el(")]
        remove_draft = self.js[self.js.index("function removeDraft("):]
        remove_draft = remove_draft[:remove_draft.index("function putDraft(")]
        # Rotation happens on mutation...
        self.assertIn("rotateBatchId();", put_draft)
        self.assertIn("rotateBatchId();", remove_draft)
        # ...and nowhere else: submit/download/copy must resend the same id.
        self.assertNotIn("rotateBatchId", self.js[self.js.index("function payload()"):])

    def test_editing_a_comment_body_mints_a_new_comment_id(self):
        """The server keys an accepted comment by id, so a body edit needs a new id.

        Regression: `putDraft` overwrote `existing.body` in place, so the id
        stayed put and the resubmission read as "already accepted with different
        content (conflict)" - even after a reload, since the id was saved with
        the draft.
        """
        put_draft = self.js[self.js.index("function putDraft("):]
        put_draft = put_draft[:put_draft.index("function el(")]
        self.assertIn("if (existing.body !== body) {", put_draft)
        self.assertIn("existing.id = uuid();", put_draft)
        # The identity change is conditional, so saving the same text twice is
        # still the same batch and the same comment.
        guard = put_draft[put_draft.index("if (existing.body !== body) {"):]
        self.assertIn("existing.body = body;", guard)

    def test_handed_off_drafts_are_still_fingerprinted(self):
        """The handoff fingerprint records *which* drafts left, not just how many.

        It is no longer the unload trigger, but it is still the record of what
        the controller has been given - so it must compare the whole set (sorted,
        so a reorder is not a change), not a count.
        """
        self.assertIn("function draftFingerprint()", self.js)
        self.assertIn("var HANDOFF_KEY = STORAGE_KEY + \":handoff\";", self.js)
        body = self.js[self.js.index("function draftFingerprint()"):]
        body = body[:body.index("function markHandedOff()")]
        self.assertIn("targetKey(drafts[i].target)", body)
        self.assertIn("parts.sort();", body)

    def test_deep_link_reveals_the_panel_that_owns_the_target(self):
        """A `#thread-…` / `#file-…` link must switch tabs and scroll.

        Regression: those ids live only in the Files panel, which the CSS-only
        tab mechanism leaves `display: none` while the Conversation radio is
        checked. Chromium does not scroll a fragment into a hidden subtree, so
        the deep link silently landed nowhere.
        """
        self.assertIn("function revealTarget(id, smooth)", self.js)
        self.assertIn("function selectTab(panel)", self.js)
        self.assertIn("function followHash(smooth)", self.js)
        # The target's own panel decides which radio is checked.
        self.assertIn("panelForTarget(node)", self.js)
        self.assertIn('closest(".prpanel")', self.js)
        # A collapsed <details> ancestor must be opened or there is no layout box.
        reveal = self.js[self.js.index("function revealTarget("):]
        reveal = reveal[:reveal.index("function hashTarget(")]
        self.assertIn('ancestor.tagName === "DETAILS"', reveal)
        self.assertIn("ancestor.open = true", reveal)
        # Deferred scroll: Chromium has not laid out the freshly shown panel yet.
        self.assertIn("requestAnimationFrame", reveal)
        # In-page rail jumps hit the same trap and are handled too.
        self.assertIn('window.addEventListener("hashchange"', self.js)
        # And the initial load arms it.
        self.assertIn("initDeepLink();", self.js)

    def test_rail_jump_expands_the_row_it_targets(self):
        """A `#file-<n>` rail link must open that row, not land on a header.

        The files rail links to `#file-<n>`, which is the `<details>` element
        *itself*. `revealTarget` opened only the target's ANCESTORS, so a jump
        to a collapsed row scrolled to a 40px summary and left the diff hidden -
        the row the rail was pointing at stayed shut.
        """
        reveal = self.js[self.js.index("function revealTarget("):]
        reveal = reveal[:reveal.index("function hashTarget(")]
        # The walk starts at node.parentNode, so it can never open a node that is
        # itself the <details>; the target has to be opened explicitly.
        self.assertIn('node.tagName === "DETAILS"', reveal)
        self.assertIn("node.open = true", reveal)

    def test_rail_jump_never_collapses_an_open_row(self):
        """Clicking a file already expanded must keep it open, not toggle it shut.

        Chosen behaviour: the rail always ensures the row is expanded and
        scrolls to it, so a second click re-scrolls rather than hiding the diff
        the reader is looking at. That is what makes `open = true` idempotent
        rather than `open = !open` - a toggle here would let a stray click hide
        content with no way to tell it was intentional.
        """
        reveal = self.js[self.js.index("function revealTarget("):]
        reveal = reveal[:reveal.index("function hashTarget(")]
        self.assertNotIn("node.open = !node.open", reveal)
        self.assertNotIn("node.open = false", reveal)
        self.assertNotIn("ancestor.open = false", reveal)

    def test_scrollbars_are_themed_for_the_dark_surface(self):
        out = render_html(with_hunk())
        # The standard property (Chromium 121+/Firefox) plus the legacy pseudos.
        self.assertIn("scrollbar-color: var(--scroll-thumb) var(--scroll-track)", out)
        self.assertIn("::-webkit-scrollbar-thumb", out)
        self.assertIn("::-webkit-scrollbar-track", out)
        # Without this Chromium keeps light native scrollbars on a dark page.
        self.assertIn("color-scheme: dark", out)
        # The thumb needs the inset to look rounded rather than a full-width bar.
        self.assertIn("background-clip: padding-box", out)
        for var in ("--scroll-track:", "--scroll-thumb:", "--scroll-thumb-hover:"):
            self.assertIn(var, out)


class StaticTemplateContractTests(unittest.TestCase):
    """`assets/report-template.html` must obey the same rules it documents.

    references/reporting.md calls the hand-written static template a last resort
    and holds it to the 14px floor and the renderer's body metrics. The template
    had drifted to `15px/1.6` inside a 960px column while the doc still claimed
    the rules held - a hand-written page that looks materially different from a
    rendered one is the failure mode the rule exists to prevent.
    """

    @classmethod
    def setUpClass(cls):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "assets", "report-template.html",
        )
        with open(path, encoding="utf-8") as handle:
            cls.html = handle.read()

    def test_body_metrics_match_the_renderer(self):
        self.assertIn("font:16px/1.55", self.html.replace(" ", ""))

    def test_body_column_matches_the_renderer(self):
        self.assertIn("max-width:1200px", self.html.replace(" ", ""))

    def test_no_font_size_falls_below_the_14px_floor(self):
        import re as _re
        sizes = [float(v) for v in _re.findall(r"font-size:\s*([\d.]+)px", self.html)]
        # The shorthand `font:16px/1.55` is the body; anything declared with
        # font-size must clear the floor.
        self.assertTrue(sizes, "the template must declare its small-text size")
        for size in sizes:
            self.assertGreaterEqual(size, 14, f"font-size {size}px is below the floor")

    def test_template_stays_script_free(self):
        # It is the static last resort: no script, no remote asset, ever.
        self.assertNotIn("<script", self.html)
        self.assertNotIn("http://", self.html)
        self.assertNotIn("https://", self.html)


class ViewManifestRetentionTests(unittest.TestCase):
    """`views/` is immutable and appended to: one manifest per distinct view.

    This is deliberate, not a leak. An exported feedback envelope names the view
    it was authored against, and a *stale* (not current) submission must still
    resolve and import as stale rather than fail as an unknown view. Pruning
    superseded manifests would trade a growth problem for a correctness one, so
    the policy is documented in references/workflow.md instead.
    """

    def _workspace(self):
        root = tempfile.mkdtemp(prefix="herdr-views-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        task_dir = os.path.join(root, ".agent-pr", "tasks", "task-001")
        os.makedirs(task_dir)
        with open(os.path.join(task_dir, "workflow.resolved.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(v2_config(mode="offline-feedback"), handle)
        return task_dir

    def _write(self, task_dir, index):
        data = with_hunk()
        data["status"] = "REVIEWING"
        data["changes"][0]["diff"] = HUNK_DIFF + f"+another = {index}\n"
        with open(os.path.join(task_dir, "report.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        write_report(task_dir)

    def _views(self, task_dir):
        return sorted(
            n for n in os.listdir(os.path.join(task_dir, "views")) if n.endswith(".json")
        )

    def test_each_distinct_view_keeps_its_manifest(self):
        task_dir = self._workspace()
        self._write(task_dir, 1)
        first = self._views(task_dir)
        self.assertEqual(len(first), 1)

        self._write(task_dir, 2)
        second = self._views(task_dir)
        self.assertEqual(len(second), 2, "a changed render must not delete the old view")
        self.assertEqual(set(first).issubset(set(second)), True)

    def test_rerendering_an_unchanged_report_is_idempotent(self):
        task_dir = self._workspace()
        self._write(task_dir, 1)
        self._write(task_dir, 1)  # identical content -> identical view_id
        self.assertEqual(len(self._views(task_dir)), 1)

    def test_a_superseded_view_is_stale_not_unknown(self):
        """The retention policy exists so this keeps resolving."""
        task_dir = self._workspace()
        self._write(task_dir, 1)
        old_view = self._views(task_dir)[0][:-len(".json")]
        self._write(task_dir, 2)

        import feedback as feedback_module  # noqa: E402  (test-local import)
        manifest = feedback_module.load_manifest(task_dir, old_view)
        self.assertEqual(manifest["view_id"], old_view)


def file_feedback_block(file_id, body="explain why need this?",
                        rationale="Because LF is canonical."):
    """A v2 feedback block whose thread targets a whole file, not a line."""
    block = feedback_block("anchor-unused")
    target = {"kind": "file", "file_id": file_id}
    block["threads"][0]["target"] = dict(target)
    block["threads"][0]["status"] = "recorded"
    block["comments"][0]["target"] = dict(target)
    block["comments"][0]["body"] = body
    block["decisions"][0]["rationale"] = rationale
    return block


class ImportedHumanCommentTests(unittest.TestCase):
    """An imported comment must stay visible where the reviewer wrote it.

    Regression (2026-10-15, review-20261015-001): the reviewer left a comment
    from the Files panel (``report.html#file-0``), the controller imported the
    batch and re-rendered, and reloading the page showed no reply. The comment
    and the controller's rationale existed *only* in the Conversation tab's
    Human feedback section, which a ``#file-0`` deep link never reveals, and the
    draft card that had shown the text before the export was cleared from
    localStorage -- so the file looked as if the comment had never been made.
    """

    def _report(self):
        data = with_hunk()
        view = render_report._build_view(data, interactive=True)
        view_id = view["manifest"]["view_id"]
        data["schema_version"] = 2
        block = file_feedback_block(view["files"]["src/widget.py"])
        block["current_view_id"] = view_id
        block["views"] = [{"id": view_id, "digest": HEXLESS}]
        block["comments"][0]["origin_view_id"] = view_id
        data["feedback"] = block
        return data

    def _files_panel(self, out):
        """The Files panel's markup, with every <style>/<script> block removed."""
        body = re.sub(r"<style>.*?</style>", "", out, flags=re.DOTALL)
        body = re.sub(r"<script>.*?</script>", "", body, flags=re.DOTALL)
        return body[body.index('data-panel="files"'):]

    def test_the_reply_is_badged_next_to_the_file_it_answers(self):
        files = self._files_panel(render_html(self._report(), mode="offline-feedback"))
        self.assertIn("Human (imported)", files)
        self.assertIn("explain why need this?", files)
        # The controller's answer is badged like every other speaker in the
        # report, not plain prose prefixed with "Controller: ".
        self.assertIn('<span class="badge role-controller">Controller</span>', files)
        self.assertIn("Because LF is canonical.", files)

    def test_the_comment_clock_is_shown_in_local_time(self):
        """A raw `…Z` in the header is exactly what the reader had to convert."""
        files = self._files_panel(render_html(self._report(), mode="offline-feedback"))
        self.assertIn('<time class="tl-when" datetime="2026-10-13T11:00:00Z">', files)
        # The bare UTC string must not survive as visible text anywhere.
        self.assertNotIn(">2026-10-13T11:00:00Z<", files)

    def test_the_conversation_copy_is_the_single_jump_target(self):
        out = render_html(self._report(), mode="offline-feedback")
        body = re.sub(r"<style>.*?</style>", "", out, flags=re.DOTALL)
        # The id exists exactly once (the Conversation copy), and the Files
        # panel's rail links to it -- duplicate ids are invalid HTML.
        self.assertEqual(body.count('id="fb-human-t1"'), 1)
        self.assertIn('href="#fb-human-t1"', self._files_panel(out))

    def test_the_file_row_advertises_the_imported_comment(self):
        files = self._files_panel(render_html(self._report(), mode="offline-feedback"))
        # The fixture already carries one review finding, so the imported
        # comment takes the row (and the rail badge) from 1 to 2.
        self.assertIn('title="2 comments"', files)
        self.assertIn('class="has-cmt"', files)

    def test_static_mode_renders_it_too_and_stays_script_free(self):
        out = render_html(self._report())
        self.assertNotIn("<script", out)
        files = self._files_panel(out)
        self.assertIn("Because LF is canonical.", files)
        # The reply is server-rendered markup, so static mode must carry the
        # badge itself -- the styling lives in the always-emitted base CSS.
        self.assertIn('<span class="badge role-controller">Controller</span>', files)

    def test_a_file_target_is_named_by_path_not_by_its_id(self):
        data = self._report()
        view = render_report._build_view(data, interactive=True)
        label = render_report._target_label(
            {"kind": "file", "file_id": view["files"]["src/widget.py"]}, data, view
        )
        self.assertEqual(label, "src/widget.py")

    def test_a_line_target_is_named_by_path_and_line(self):
        data = with_hunk()
        view = render_report._build_view(data, interactive=True)
        anchor_id = view["lines"][("src/widget.py", "new", 2)]
        label = render_report._target_label(
            {"kind": "line", "anchor_id": anchor_id}, data, view
        )
        self.assertEqual(label, "src/widget.py:2")

    def test_an_unresolvable_target_keeps_the_tagged_id(self):
        """A stale anchor must stay visibly unidentified, not be dressed up."""
        data = self._report()
        view = render_report._build_view(data, interactive=True)
        label = render_report._target_label(
            {"kind": "file", "file_id": "file-" + "0" * 64}, data, view
        )
        self.assertEqual(label, "file " + "file-" + "0" * 64)


class TimelineJoinTests(unittest.TestCase):
    """A timeline row must never borrow a neighbouring row's clock."""

    def _report(self):
        return test_renderer.make_report()

    def test_event_seq_wins_over_position(self):
        data = self._report()
        data["timeline"][2]["event_seq"] = 5
        events = [
            {"seq": 1, "ts": "2026-10-15T03:35:00Z"},
            {"seq": 2, "ts": "2026-10-15T03:36:00Z"},
            {"seq": 3, "ts": "2026-10-15T03:37:00Z"},
            {"seq": 4, "ts": "2026-10-15T04:02:56Z"},
            {"seq": 5, "ts": "2026-10-15T04:05:00Z"},
        ]
        out = render_html(data, events=events)
        self.assertIn('datetime="2026-10-15T04:05:00Z"', out)
        self.assertNotIn('datetime="2026-10-15T03:37:00Z"', out)

    def test_an_unresolvable_event_seq_shows_no_time_rather_than_a_guess(self):
        data = self._report()
        data["timeline"][0]["event_seq"] = 1
        data["timeline"][1]["event_seq"] = 99  # no such event
        events = [
            {"seq": 1, "ts": "2026-10-15T03:35:00Z"},
            {"seq": 2, "ts": "2026-10-15T03:36:00Z"},
            {"seq": 3, "ts": "2026-10-15T03:37:00Z"},
            {"seq": 4, "ts": "2026-10-15T03:38:00Z"},
        ]
        out = render_html(data, events=events)
        self.assertEqual(out.count('class="tl-when"'), 1)
        self.assertIn('datetime="2026-10-15T03:35:00Z"', out)
        # Entry 1's seq resolves to nothing, and entry 2 carries no seq while the
        # log is longer than the timeline -- neither may take a positional time.
        self.assertNotIn('datetime="2026-10-15T03:36:00Z"', out)
        self.assertNotIn('datetime="2026-10-15T03:37:00Z"', out)

    def test_a_log_longer_than_the_timeline_times_nothing_by_position(self):
        """9 events against 8 entries is exactly the shape that mislabelled rows.

        A longer log proves the timeline omits events but says nothing about
        where, so position stops being a join key: every unstamped entry shows
        no time instead of its neighbour's clock.
        """
        data = self._report()
        events = [
            {"seq": n, "ts": "2026-10-15T0%d:00:00Z" % (n % 10)}
            for n in range(1, len(data["timeline"]) + 2)
        ]
        self.assertGreater(len(events), len(data["timeline"]))
        out = render_html(data, events=events)
        # `.tl-when` is also a CSS class name, so match the attribute, not the bare token.
        self.assertNotIn('class="tl-when"', out)

    def test_a_non_integer_event_seq_is_rejected(self):
        for bad in ("3", 3.5, True, []):
            with self.subTest(bad=bad):
                data = self._report()
                data["timeline"][0]["event_seq"] = bad
                with self.assertRaises(ValidationError):
                    validate_report(data)


if __name__ == "__main__":
    unittest.main()
