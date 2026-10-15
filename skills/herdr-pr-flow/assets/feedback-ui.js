/* herdr-pr-flow interactive feedback UI.
 *
 * Bundled with the skill and inlined by scripts/render_report.py into the
 * report only in the opt-in offline-feedback / live modes. Dependency-free and
 * offline: it never loads a remote asset. In live mode it posts to the
 * task-local loopback helper started by scripts/serve_report.py; otherwise it
 * exports a JSON blob the human hands back to the controller.
 *
 * All user text is inserted with textContent (never innerHTML), so a hostile
 * comment body cannot become markup. This file must not contain the literal
 * script end tag.
 */
(function () {
  "use strict";

  // The bar is rendered once per tab panel (Conversation *and* Files changed),
  // so these class names - not ids - are how the script reaches its controls.
  // Only one bar is ever visible, but the hidden one must stay in sync and stay
  // bound: binding by id hit only the first bar, leaving every control in the
  // other panel completely inert.
  var bars = document.querySelectorAll(".fbbar");
  if (!bars.length) {
    return;
  }

  var bar = bars[0];
  var TASK_ID = bar.getAttribute("data-task-id") || "";
  var VIEW_ID = bar.getAttribute("data-view-id") || "";
  var MODE = bar.getAttribute("data-mode") || "offline-feedback";
  var ENDPOINT = bar.getAttribute("data-endpoint") || "";
  var STORAGE_KEY = "herdr-feedback:" + TASK_ID + ":" + VIEW_ID;
  var BATCH_ID = uuid();
  var LIVE = MODE === "live";

  // `batch_id` names one immutable submission, not one page load. The controller
  // stages each batch at `inbox/human/<batch-id>.json` and refuses to overwrite
  // it with different bytes, so a batch id that survives an edit makes the
  // *second* submission of a session collide with the first:
  // "a different submission already uses this batch_id" (HTTP 422). The same
  // trap sits under the comment id - the server keys accepted comments by id and
  // rejects a re-used id whose content changed. Both are therefore rotated the
  // moment a draft changes, and deliberately NOT on a pure retry: resending an
  // unchanged batch must stay byte-identical so `stage_envelope` treats it as a
  // no-op instead of a conflict.
  function rotateBatchId() {
    BATCH_ID = uuid();
  }

  var drafts = [];

  function all(selector) {
    var found = [];
    for (var i = 0; i < bars.length; i += 1) {
      var nodes = bars[i].querySelectorAll(selector);
      for (var j = 0; j < nodes.length; j += 1) {
        found.push(nodes[j]);
      }
    }
    return found;
  }

  function forEachBar(selector, handler) {
    var nodes = all(selector);
    for (var i = 0; i < nodes.length; i += 1) {
      handler(nodes[i]);
    }
  }

  function setStatus(message, kind) {
    forEachBar(".fbstatus", function (node) {
      node.textContent = message || "";
      node.className = "fbstatus" + (kind ? " fbstatus-" + kind : "");
    });
  }

  function uuid() {
    if (window.crypto && window.crypto.randomUUID) {
      return window.crypto.randomUUID();
    }
    return "c-" + Date.now().toString(16) + "-" + Math.random().toString(16).slice(2, 10);
  }

  function token() {
    var match = /[?&]t=([^&]+)/.exec(window.location.search);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function targetKey(target) {
    if (target.kind === "line") {
      return "line:" + target.anchor_id;
    }
    if (target.kind === "thread") {
      return "thread:" + target.thread_id;
    }
    return "file:" + target.file_id;
  }

  function load() {
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      if (raw) {
        var parsed = JSON.parse(raw);
        if (Object.prototype.toString.call(parsed) === "[object Array]") {
          drafts = parsed;
        }
      }
    } catch (err) {
      /* browser storage is best-effort only, never the durable copy */
    }
  }

  function save() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(drafts));
    } catch (err) {
      /* session-only; warn on reload instead */
    }
  }

  // A draft that has been Exported, Copied or Submitted has left the reviewer's
  // hands, so leaving the page no longer risks losing it. `HANDOFF_KEY` records
  // the exact draft set that was handed over: any later edit adds or changes a
  // draft, the fingerprint stops matching, and the prompt comes back. Without
  // this the unload prompt fired forever after a single comment - including
  // immediately after a successful Export, which reads as a bug.
  var HANDOFF_KEY = STORAGE_KEY + ":handoff";

  function draftFingerprint() {
    var parts = [];
    for (var i = 0; i < drafts.length; i += 1) {
      // Sorted by key so a reorder is not mistaken for a change.
      parts.push(targetKey(drafts[i].target) + "\u0000" + drafts[i].body);
    }
    parts.sort();
    return parts.length + "\u0001" + parts.join("\u0002");
  }

  function markHandedOff() {
    try {
      window.localStorage.setItem(HANDOFF_KEY, draftFingerprint());
    } catch (err) {
      /* best-effort only */
    }
  }

  // True when there is a draft the reviewer has not yet handed over.
  function handoffPending() {
    if (!drafts.length) {
      return false;
    }
    var marked = null;
    try {
      marked = window.localStorage.getItem(HANDOFF_KEY);
    } catch (err) {
      marked = null;
    }
    return marked !== draftFingerprint();
  }

  // Saved drafts live in localStorage and are restored on the next load, so
  // warning about them is a false alarm - it tells the reviewer their work is
  // about to be lost when it is not. Only text still sitting in an OPEN editor
  // that has never been saved is genuinely at risk. `dirtyEditors` counts those
  // editors; it is what `beforeunload` consults.
  //
  // `markDirty` is passed in only as an identity token: it is the key under
  // which an editor is registered, so releasing the same editor twice cannot
  // double-count. It must NOT be called here - it calls straight back into
  // `markDirty`, which calls back into this function, and the mutual recursion
  // overflows the stack inside the `input` handler. The throw is swallowed by
  // event dispatch, so the textarea still updates while `dirtyEditors` never
  // moves and the unload prompt silently never fires.
  var dirtyEditors = 0;
  var pendingEditors = [];

  function trackPendingEditor(token) {
    if (pendingEditors.indexOf(token) === -1) {
      pendingEditors.push(token);
      dirtyEditors += 1;
    }
  }

  function releasePendingEditor(token) {
    var at = pendingEditors.indexOf(token);
    if (at !== -1) {
      pendingEditors.splice(at, 1);
      dirtyEditors -= 1;
    }
  }

  function hasUnsavedEditorText() {
    return dirtyEditors > 0;
  }

  function draftFor(target) {
    var key = targetKey(target);
    for (var i = 0; i < drafts.length; i += 1) {
      if (targetKey(drafts[i].target) === key) {
        return drafts[i];
      }
    }
    return null;
  }

  function refresh() {
    var label = drafts.length + (drafts.length === 1 ? " comment" : " comments");
    forEachBar(".fbcount", function (node) {
      node.textContent = label;
    });
    forEachBar(".fbsubmit", function (node) {
      node.disabled = drafts.length === 0;
    });
    syncDraftMarkers();
    syncDraftCards();
  }

  function removeDraft(target) {
    var key = targetKey(target);
    drafts = drafts.filter(function (item) {
      return targetKey(item.target) !== key;
    });
    // A removal is a content change: the batch this draft belonged to is now
    // different, so it needs a fresh id before it can be submitted again.
    rotateBatchId();
    save();
    refresh();
  }

  function putDraft(target, body) {
    var existing = draftFor(target);
    if (existing) {
      if (existing.body !== body) {
        // The server fingerprints a comment by id, so editing the text must mint
        // a new id. Re-using it made an edited comment read as "already accepted
        // with different content (conflict)" - even after a reload, because the
        // id was persisted with the draft.
        existing.id = uuid();
        existing.body = body;
        rotateBatchId();
      }
    } else {
      drafts.push({ id: uuid(), target: target, body: body });
      rotateBatchId();
    }
    save();
    refresh();
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  }

  // A saved draft must be VISIBLE, not just stored: without this the editor
  // closes and the reviewer sees no trace of their own text anywhere, which
  // reads as "it did not save". Each draft renders as a read-only card next to
  // the thing it targets, with Edit/Remove so it can be revised.
  function draftCard(draft, label) {
    var card = el("div", "fb-draftcard");
    var head = el("div", "fb-draftcard-head");
    head.appendChild(el("span", "fb-draftcard-tag", label));
    var body = el("div", "fb-draftcard-body");
    body.textContent = draft.body;
    var actions = el("div", "fb-draftcard-actions");
    card.appendChild(head);
    card.appendChild(body);
    card.appendChild(actions);
    return card;
  }

  // True while an editor for this exact target is open. The card must be hidden
  // then: the editor is pre-filled from the same draft, so showing both puts the
  // same text on screen twice and leaves the card's Edit/Remove buttons live
  // underneath the editor the reviewer is typing in.
  function editorOpenFor(target) {
    var editors = document.querySelectorAll(".fb-editor");
    for (var i = 0; i < editors.length; i += 1) {
      if (editors[i].getAttribute("data-target-key") === targetKey(target)) {
        return true;
      }
    }
    return false;
  }

  function renderDraftInto(anchorNode, target, label) {
    // Replace any previously rendered card for this target so edits do not
    // stack. The editor may sit between the anchor and the card (openEditor
    // inserts at the same position), so skip over it rather than assuming the
    // card is the immediate next sibling.
    var node = anchorNode.nextSibling;
    while (node && node.className === "fb-editor") {
      node = node.nextSibling;
    }
    if (node && node.className === "fb-draftcard") {
      node.parentNode.removeChild(node);
    }
    // Editing this target supersedes its card until the editor closes.
    if (editorOpenFor(target)) {
      return;
    }
    var draft = draftFor(target);
    if (!draft) {
      return;
    }
    // Insert after the editor when one is open, so the editor stays adjacent to
    // the control that opened it.
    var after = anchorNode.nextSibling;
    while (after && after.className === "fb-editor") {
      after = after.nextSibling;
    }
    if (after && after.className === "fb-draftcard") {
      after = after.nextSibling;
    }
    var card = draftCard(draft, label);
    var actions = card.querySelector(".fb-draftcard-actions");
    var editBtn = el("button", "fb-btn fb-btn-ghost", "Edit");
    editBtn.type = "button";
    var removeBtn = el("button", "fb-btn fb-btn-danger", "Remove");
    removeBtn.type = "button";
    editBtn.addEventListener("click", function () {
      openEditor(anchorNode, target, label);
    });
    removeBtn.addEventListener("click", function () {
      removeDraft(target);
      if (card.parentNode) {
        card.parentNode.removeChild(card);
      }
      setStatus("Draft removed.");
    });
    actions.appendChild(editBtn);
    actions.appendChild(removeBtn);
    anchorNode.parentNode.insertBefore(card, after);
  }

  var DRAFT_LABELS = {
    thread: "Your reply",
    file: "Your comment",
    line: "Your comment"
  };

  function syncDraftCards() {
    var mounts = document.querySelectorAll(".fb-replymount[data-reply-anchor]");
    for (var j = 0; j < mounts.length; j += 1) {
      renderDraftInto(
        mounts[j],
        { kind: "thread", thread_id: mounts[j].getAttribute("data-reply-anchor") },
        DRAFT_LABELS.thread
      );
    }
    var files = document.querySelectorAll(".comments[data-file-anchor]");
    for (var k = 0; k < files.length; k += 1) {
      var holder = files[k].querySelector(".fb-replymount[data-compose-anchor]");
      if (holder) {
        renderDraftInto(
          holder,
          { kind: "file", file_id: files[k].getAttribute("data-file-anchor") },
          DRAFT_LABELS.file
        );
      }
    }
  }

  function syncDraftMarkers() {
    var rows = document.querySelectorAll(".dl[data-addressable='1']");
    for (var i = 0; i < rows.length; i += 1) {
      rows[i].classList.toggle(
        "fb-draft",
        !!draftFor({
          kind: "line",
          anchor_id: rows[i].getAttribute("data-anchor")
        })
      );
    }
    var mounts = document.querySelectorAll(".fb-replymount[data-reply-anchor]");
    for (var j = 0; j < mounts.length; j += 1) {
      var card = mounts[j].closest(".finding");
      if (card) {
        card.classList.toggle(
          "fb-draft",
          !!draftFor({
            kind: "thread",
            thread_id: mounts[j].getAttribute("data-reply-anchor")
          })
        );
      }
    }
    var files = document.querySelectorAll(".comments[data-file-anchor]");
    for (var k = 0; k < files.length; k += 1) {
      files[k].classList.toggle(
        "fb-draft",
        !!draftFor({
          kind: "file",
          file_id: files[k].getAttribute("data-file-anchor")
        })
      );
    }
  }

  function openEditor(anchorNode, target, label) {
    if (anchorNode.nextSibling && anchorNode.nextSibling.className === "fb-editor") {
      anchorNode.nextSibling.querySelector("textarea").focus();
      return;
    }
    var wrap = el("div", "fb-editor");
    // Identify which draft this editor supersedes, so renderDraftInto can hide
    // that target's card while the editor is open.
    wrap.setAttribute("data-target-key", targetKey(target));
    var area = el("textarea");
    area.setAttribute("aria-label", label);
    area.value = draftFor(target) ? draftFor(target).body : "";
    var row = el("div", "fb-row");
    var saveBtn = el("button", "fb-btn fb-btn-primary", "Save draft");
    saveBtn.type = "button";
    var cancelBtn = el("button", "fb-btn fb-btn-ghost", "Cancel");
    cancelBtn.type = "button";
    var removeBtn = el("button", "fb-btn fb-btn-danger", "Remove");
    removeBtn.type = "button";
    var hint = el("span", "fb-hint", "Ctrl+Enter to save. Draft only - use Export or Submit.");
    row.appendChild(saveBtn);
    row.appendChild(cancelBtn);
    row.appendChild(removeBtn);
    row.appendChild(hint);
    wrap.appendChild(area);
    wrap.appendChild(row);
    // Keep the editor adjacent to the anchor: if a saved-draft card already
    // sits there, insert ahead of it. The card is then hidden by refresh() so
    // the editor stands alone while editing.
    var before = anchorNode.nextSibling;
    if (before && before.className === "fb-draftcard") {
      anchorNode.parentNode.insertBefore(wrap, before);
    } else {
      anchorNode.parentNode.insertBefore(wrap, anchorNode.nextSibling);
    }
    // Hide this target's card for as long as the editor is open.
    syncDraftCards();

    // An open editor holding unsaved text is the ONLY thing a reload can
    // actually destroy. Typing marks it; saving, removing or cancelling clears
    // it. Without this the prompt fired after a *saved* draft, whose body is
    // already durable in localStorage and is restored on the next load.
    // The function is its own token: re-registering it is a no-op, and any exit
    // path releases exactly this editor.
    var markDirty = function () {
      if (area.value.trim().length > 0) {
        trackPendingEditor(markDirty);
      } else {
        releasePendingEditor(markDirty);
      }
    };
    if (area.value.trim().length > 0) {
      trackPendingEditor(markDirty);
    }
    area.addEventListener("input", markDirty);

    function close() {
      releasePendingEditor(markDirty);
      if (wrap.parentNode) {
        wrap.parentNode.removeChild(wrap);
      }
      // Restore the card (it may also be gone because the draft was removed).
      syncDraftCards();
    }

    function saveDraft() {
      var text = area.value.trim();
      if (text) {
        putDraft(target, text);
        setStatus("Draft saved.", "ok");
      } else {
        removeDraft(target);
        setStatus("Empty draft removed.", "warn");
      }
      close();
    }

    saveBtn.addEventListener("click", saveDraft);
    cancelBtn.addEventListener("click", close);
    removeBtn.addEventListener("click", function () {
      removeDraft(target);
      setStatus("Draft removed.");
      close();
    });
    area.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        close();
        return;
      }
      // Ctrl+Enter (and Cmd+Enter on macOS) saves, matching the primary button.
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        saveDraft();
      }
    });
    area.focus();
  }

  function wireLineGutters() {
    var rows = document.querySelectorAll(".dl[data-addressable='1']");
    for (var i = 0; i < rows.length; i += 1) {
      (function (row) {
        var target = {
          kind: "line",
          anchor_id: row.getAttribute("data-anchor")
        };
        var path = row.getAttribute("data-path") || "";
        var side = row.getAttribute("data-side") || "";
        var line = row.getAttribute("data-line") || "";
        var gutter = el("span", "dl-gutter");
        var button = el("button", "fb-btn fb-btn-gutter", "+");
        button.type = "button";
        button.title = "Comment on " + path + " (" + side + " " + line + ")";
        button.setAttribute(
          "aria-label",
          "Add a comment on " + path + " " + side + " line " + line
        );
        button.addEventListener("click", function () {
          openEditor(row, target, "Comment on " + path + " line " + line);
        });
        gutter.appendChild(button);
        row.insertBefore(gutter, row.firstChild);
      })(rows[i]);
    }
  }

  function wireThreadReplies() {
    var mounts = document.querySelectorAll(".fb-replymount[data-reply-anchor]");
    for (var i = 0; i < mounts.length; i += 1) {
      (function (mount) {
        var threadId = mount.getAttribute("data-reply-anchor");
        var target = { kind: "thread", thread_id: threadId };
        var button = el("button", "fb-btn", "Reply");
        button.type = "button";
        button.addEventListener("click", function () {
          openEditor(mount, target, "Reply to " + threadId);
        });
        mount.appendChild(button);
      })(mounts[i]);
    }
  }

  function wireFileComposers() {
    var mounts = document.querySelectorAll(".comments[data-file-anchor]");
    for (var i = 0; i < mounts.length; i += 1) {
      (function (mount) {
        var fileId = mount.getAttribute("data-file-anchor");
        var target = { kind: "file", file_id: fileId };
        var button = el("button", "fb-btn", "Comment on this file");
        button.type = "button";
        var holder = el("div", "fb-replymount");
        // Mark the holder as the file composer. A thread reply mount also lives
        // inside .comments[data-file-anchor], so a bare .fb-replymount lookup
        // would find the WRONG mount, target it as a file, and delete the card
        // it had just rendered for the thread.
        holder.setAttribute("data-compose-anchor", fileId);
        holder.appendChild(button);
        button.addEventListener("click", function () {
          openEditor(holder, target, "Comment on file " + fileId);
        });
        mount.appendChild(holder);
      })(mounts[i]);
    }
  }

  function payload() {
    return {
      schema_version: 1,
      task_id: TASK_ID,
      view_id: VIEW_ID,
      batch_id: BATCH_ID,
      comments: drafts.map(function (item) {
        return { id: item.id, target: item.target, body: item.body };
      })
    };
  }

  function asJson() {
    return JSON.stringify(payload(), null, 2);
  }

  function download() {
    if (!drafts.length) {
      setStatus("Nothing to export yet.", "warn");
      return;
    }
    var blob = new Blob([asJson()], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = TASK_ID + ".feedback.json";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    markHandedOff();
    setStatus("Exported - pass the file to the controller; exported is not received.", "ok");
  }

  function copy() {
    if (!drafts.length) {
      setStatus("Nothing to copy yet.", "warn");
      return;
    }
    var text = asJson();
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () {
          markHandedOff();
          setStatus("Copied JSON to the clipboard.", "ok");
        },
        function () {
          setStatus("Clipboard blocked; use Export instead.", "error");
        }
      );
    } else {
      setStatus("Clipboard unavailable; use Export instead.", "error");
    }
  }

  function submit() {
    if (!drafts.length) {
      setStatus("Nothing to submit yet.", "warn");
      return;
    }
    fetch(ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Herdr-Token": token()
      },
      body: asJson()
    })
      .then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, body: body };
        });
      })
      .then(function (result) {
        if (result.ok) {
          markHandedOff();
          setStatus("Queued for the controller. The controller must still import it.", "ok");
        } else {
          setStatus(
            "Rejected: " +
              ((result.body && result.body.message) || "see the server log"),
            "error"
          );
        }
      })
      .catch(function () {
        setStatus("No live server reachable; use Export instead.", "error");
      });
  }

  // -------------------------------------------------------------------------
  // Deep links and tab selection
  // -------------------------------------------------------------------------
  // `#file-…` / `#thread-…` ids only exist inside the Files-changed panel, which
  // the CSS-only tab mechanism leaves hidden (the Conversation radio is the one
  // hardcoded `checked`). A fragment navigation into a `display: none` subtree
  // does not scroll, so the browser lands on the top of a *hidden* panel and the
  // deep link silently does nothing. Nothing here is CSS-solvable: `:target`
  // cannot reach a sibling panel through the radio, so the fix belongs in JS.
  function selectTab(panel) {
    var input = document.getElementById("ptab-" + panel);
    if (input) {
      input.checked = true;
    }
  }

  function panelForTarget(node) {
    var panel = node.closest ? node.closest(".prpanel") : null;
    return panel ? panel.getAttribute("data-panel") : null;
  }

  // Reveal the panel that owns `id`, expand any collapsed <details> ancestor,
  // then scroll the node into view. Safe to call repeatedly.
  function revealTarget(id, smooth) {
    if (!id) {
      return false;
    }
    var node = document.getElementById(id);
    if (!node) {
      return false;
    }
    var panel = panelForTarget(node);
    if (panel) {
      selectTab(panel);
    }
    // A finding inside a closed file row has no layout box, so its ancestor
    // <details> must be opened before any scroll can land.
    var ancestor = node.parentNode;
    while (ancestor && ancestor !== document.body) {
      if (ancestor.tagName === "DETAILS") {
        ancestor.open = true;
      }
      ancestor = ancestor.parentNode;
    }
    // The files-rail links to `#file-<n>`, which IS the <details> itself - so
    // the walk above (which starts at node.parentNode) never opens it and the
    // jump lands on a collapsed 40px header. Open the target directly as well.
    if (node.tagName === "DETAILS") {
      node.open = true;
    }
    if (node.scrollIntoView) {
      // Chromium reads layout asynchronously after a display flip, so defer to
      // the next frame instead of scrolling against a zero-height element.
      window.requestAnimationFrame(function () {
        node.scrollIntoView({
          block: "start",
          behavior: smooth ? "smooth" : "auto"
        });
      });
    }
    return true;
  }

  function hashTarget() {
    var raw = window.location.hash || "";
    return raw.length > 1 ? decodeURIComponent(raw.slice(1)) : "";
  }

  function followHash(smooth) {
    return revealTarget(hashTarget(), smooth);
  }

  function initDeepLink() {
    followHash(false);
    // A second pass: the first can run before the browser has applied its own
    // fragment scroll, and it self-corrects if fonts/images shift the layout.
    window.setTimeout(function () {
      followHash(false);
    }, 0);
    // In-page jumps (the nav rail) switch the hash without a reload, and would
    // otherwise hit the same hidden-panel problem on the very first click.
    window.addEventListener("hashchange", function () {
      followHash(true);
    });
  }

  load();
  wireLineGutters();
  wireThreadReplies();
  wireFileComposers();
  refresh();
  initDeepLink();
  // Bind EVERY bar: both panels carry a full copy of the controls, and only the
  // visible one can be clicked, so a per-bar binding is the only correct wiring.
  forEachBar(".fbsubmit", function (node) {
    node.hidden = !LIVE;
    node.addEventListener("click", submit);
  });
  forEachBar(".fbexport", function (node) {
    node.addEventListener("click", download);
  });
  forEachBar(".fbcopy", function (node) {
    node.addEventListener("click", copy);
  });
  window.addEventListener("beforeunload", function (event) {
    // Warn only about work a reload would actually DESTROY: text sitting in an
    // open editor that was never saved. Saved drafts persist in localStorage and
    // are restored on the next load, so warning about them told the reviewer
    // their changes were unsaved when they were safe - the opposite of helpful.
    if (hasUnsavedEditorText()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
})();
