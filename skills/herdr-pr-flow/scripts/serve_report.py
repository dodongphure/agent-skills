#!/usr/bin/env python3
"""Loopback-only live feedback server for herdr-pr-flow.

Serves one task's ``report.html`` and accepts comment batches from the page at
the same origin, writing them to ``inbox/human/<batch-id>.json``. Standard
library only.

Security posture (deliberate):
  * binds 127.0.0.1 only -- never 0.0.0.0 -- so VS Code Remote / an SSH tunnel
    forwards it without exposing the port to a network;
  * rejects any non-loopback client;
  * requires a per-launch random token on the write endpoint;
  * accepts any *loopback* Host/Origin (a forwarded port may be remapped);
  * no permissive CORS, no commands, no source edits, no git;
  * writes only validated submissions inside the task directory;
  * never owns report.json / state.json / events.jsonl.

It queues a submission; it does not mean the controller has imported it.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import pathlib
import secrets
import signal
import socket
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import feedback as feedback_mod  # noqa: E402

DEFAULT_PORT = 8765
PIDFILE = "report.server.pid"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
MAX_BODY_BYTES = feedback_mod.MAX_ENVELOPE_BYTES


class ServerError(RuntimeError):
    pass


def _load_policy(task_dir: str):
    """Resolve the frozen report policy through the single version adapter.

    Returns the ``{mode, port}`` dict, or ``None`` when no frozen config exists.
    """
    config_path = os.path.join(task_dir, "workflow.resolved.json")
    if not (os.path.isfile(config_path) and not os.path.islink(config_path)):
        return None
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "herdr_config_gate", pathlib.Path(__file__).with_name("check_config.py")
    )
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    with open(config_path, "r", encoding="utf-8-sig") as handle:
        config = json.load(handle)
    gate.validate_config(config)
    return gate.report_policy(config)


def resolve_port(task_dir: str, override=None) -> int:
    """--port wins; else the frozen policy; else the default.

    The port is fixed on purpose: a busy port is a clear error rather than a
    silent re-pick, so a bookmarked URL keeps working.
    """
    if override is not None:
        return override
    try:
        policy = _load_policy(task_dir)
    except Exception as exc:  # ConfigError and friends become one clear message
        raise ServerError(f"failed to read workflow.resolved.json: {exc}") from exc
    if policy and isinstance(policy.get("port"), int):
        return policy["port"]
    return DEFAULT_PORT


def require_live_mode(task_dir: str) -> None:
    """Refuse to serve a report whose frozen policy is not ``live``.

    An absent config is allowed (the helper may be exercised without one), but a
    present config that selects ``static`` or ``offline-feedback`` is a hard error:
    the loopback helper exists only for the live path.
    """
    try:
        policy = _load_policy(task_dir)
    except Exception as exc:
        raise ServerError(f"failed to read workflow.resolved.json: {exc}") from exc
    if policy and policy.get("mode") != "live":
        raise ServerError(
            f"the frozen report policy selects {policy.get('mode')!r}; the loopback "
            "helper serves only 'live' mode"
        )


def _loopback_host(value: str) -> bool:
    if not value:
        return False
    host = value.rsplit("@", 1)[-1]
    if host.startswith("["):
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.split(":", 1)[0]
    return host in LOOPBACK_HOSTS


def _same_origin_ok(handler) -> bool:
    """Loopback-only Host and Origin; any port, because forwarding may remap it."""
    host = handler.headers.get("Host", "")
    if not _loopback_host(host):
        return False
    origin = handler.headers.get("Origin")
    if origin is None:
        return True
    parsed = urllib.parse.urlsplit(origin)
    return parsed.scheme in ("http", "https") and _loopback_host(parsed.netloc)


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "herdr-pr-flow-report"
    task_dir = ""
    token = ""

    # -- helpers ------------------------------------------------------------ #
    def _client_is_loopback(self) -> bool:
        address = self.client_address[0] if self.client_address else ""
        return address in ("127.0.0.1", "::1") or address.startswith("127.")

    def _drain(self, limit: int) -> None:
        """Consume up to ``limit`` unread request bytes before answering.

        Closing a socket that still holds unread bytes makes the OS send a TCP
        reset, so a client that announced an over-cap body would see
        ``ConnectionResetError`` instead of the 413 we just wrote. The drain is
        bounded: a hostile ``Content-Length`` is never read in full.
        """
        remaining = limit
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=True).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, fmt, *args):  # keep the console readable
        sys.stderr.write("herdr-report-server: " + (fmt % args) + "\n")

    # -- routes ------------------------------------------------------------- #
    def do_GET(self):  # noqa: N802
        if not self._client_is_loopback():
            self._json(403, {"status": "rejected", "message": "loopback clients only"})
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html", "/report.html"):
            report_path = os.path.join(self.task_dir, "report.html")
            if not os.path.isfile(report_path):
                self._json(404, {"status": "missing", "message": "no report.html yet"})
                return
            with open(report_path, "rb") as handle:
                body = handle.read()
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._json(200, {"status": "ok"})
            return
        self._json(404, {"status": "missing", "message": "unknown path"})

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_POST(self):  # noqa: N802
        if not self._client_is_loopback():
            self._json(403, {"status": "rejected", "message": "loopback clients only"})
            return
        if urllib.parse.urlsplit(self.path).path != "/api/feedback":
            self._json(404, {"status": "missing", "message": "unknown path"})
            return
        if not _same_origin_ok(self):
            self._json(403, {"status": "rejected", "message": "origin not allowed"})
            return
        if self.headers.get("X-Herdr-Token", "") != self.token:
            self._json(403, {"status": "rejected", "message": "bad or missing token"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._json(415, {"status": "rejected", "message": "application/json required"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._json(400, {"status": "rejected", "message": "bad content length"})
            return
        if length <= 0:
            self._json(413, {"status": "rejected", "message": "body size out of range"})
            return
        if length > MAX_BODY_BYTES:
            self._drain(min(length, MAX_BODY_BYTES + 1048576))
            self._json(413, {"status": "rejected", "message": "body size out of range"})
            return
        raw = self.rfile.read(length)
        try:
            text = raw.decode("utf-8")
            envelope = feedback_mod.load_json(text)
            if not isinstance(envelope, dict):
                raise feedback_mod.FeedbackError("envelope must be a JSON object")
            manifest = feedback_mod.load_manifest(self.task_dir, envelope.get("view_id"))
            existing = feedback_mod.existing_comment_index(self.task_dir)
            result = feedback_mod.validate_envelope(envelope, manifest, existing)
            staged = feedback_mod.stage_envelope(self.task_dir, result["batch_id"], text)
        except (feedback_mod.FeedbackError, UnicodeDecodeError, OSError) as exc:
            self._json(422, {"status": "rejected", "message": str(exc)})
            return
        # "Queued" is durable transport storage only -- the controller's receipt
        # is what says "imported".
        self._json(202, {
            "status": "queued",
            "batch_id": result["batch_id"],
            "comments": len(result["comments"]),
            "retried": len(result["retried_comment_ids"]),
            "staged": os.path.basename(staged),
            "note": "queued for the controller; not yet imported",
        })


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_server(task_dir: str, port: int, token: str) -> _Server:
    handler = type("BoundHandler", (_Handler,), {"task_dir": task_dir, "token": token})
    try:
        server = _Server(("127.0.0.1", port), handler)
    except OSError as exc:
        raise ServerError(
            f"cannot bind 127.0.0.1:{port} ({exc}); the port is fixed by policy, "
            "so free it or change report.port in workflow.json"
        ) from exc
    return server


def _write_pidfile(task_dir: str, pid: int, port: int) -> str:
    path = os.path.join(task_dir, PIDFILE)
    if os.path.islink(path):
        raise ServerError("refusing a symlinked pidfile")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"pid": pid, "port": port, "task_dir": task_dir}, handle)
    return path


def stop_server(task_dir: str):
    """Stop the recorded server. Returns ``(payload, exit_code)``; prints nothing."""
    path = os.path.join(task_dir, PIDFILE)
    if not os.path.isfile(path) or os.path.islink(path):
        return {"status": "NOT_RUNNING", "message": "no pidfile"}, 0
    with open(path, "r", encoding="utf-8") as handle:
        record = json.load(handle)
    recorded = record.get("task_dir")
    if not isinstance(recorded, str) or os.path.normcase(os.path.abspath(recorded)) != os.path.normcase(
        os.path.abspath(task_dir)
    ):
        # Never signal a pid that a different task recorded (or a reused pid).
        return {"status": "STALE_PIDFILE", "message": "pidfile belongs to another task"}, 2
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return {"status": "INVALID_PIDFILE"}, 2
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        try:
            os.unlink(path)
        except OSError:
            pass
        return {"status": "NOT_RUNNING", "message": str(exc)}, 0
    try:
        os.unlink(path)
    except OSError:
        pass
    return {"status": "STOPPED", "pid": pid, "port": record.get("port")}, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--port", type=int, default=None,
                        help="override report.port (0 picks a free port; tests only)")
    parser.add_argument("--stop", action="store_true", help="stop the recorded server")
    parser.add_argument("--token", default=None, help="fixed token (tests only)")
    args = parser.parse_args()

    try:
        task_dir = feedback_mod.resolve_task_dir(args.task_dir)
        if args.stop:
            payload, code = stop_server(task_dir)
            print(json.dumps(payload))
            return code
        require_live_mode(task_dir)
        port = resolve_port(task_dir, args.port)
        token = args.token or secrets.token_urlsafe(24)
        server = make_server(task_dir, port, token)
    except (ServerError, feedback_mod.FeedbackError, OSError) as exc:
        print(json.dumps({"status": "FAILED", "message": str(exc)}), file=sys.stderr)
        return 2

    bound_port = server.server_address[1]
    _write_pidfile(task_dir, os.getpid(), bound_port)
    url = f"http://127.0.0.1:{bound_port}/?t={token}"
    print(json.dumps({
        "status": "LISTENING",
        "host": "127.0.0.1",
        "port": bound_port,
        "url": url,
        "task_dir": task_dir,
        "note": "loopback only; stop with --stop or Ctrl-C",
    }))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        path = os.path.join(task_dir, PIDFILE)
        if os.path.isfile(path):
            try:
                os.unlink(path)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
