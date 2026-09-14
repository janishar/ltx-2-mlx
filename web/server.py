#!/usr/bin/env python3
"""ltx studio — a local web control surface for every ``ltx-2-mlx`` command.

Stdlib only (no FastAPI/uvicorn), so it runs in the repo's existing uv
environment. Each render spawns ``python -m ltx_pipelines_mlx <subcommand>``
with an argv built from the browser form; one job runs at a time (one GPU) and
the rest wait in a queue. Logs and progress stream to the browser over SSE.

Sessions are plain directories:

    web/sessions/<name>/
        setting.json   UI state, saved while you edit
        inputs/        uploads and media pulled back from takes
        outputs/       rendered .mp4 takes, each with a .json sidecar
        previews/      optional live-preview clips per render (animated WebP per step)
        timeline/      combined videos from the timeline editor, each with a .json sidecar

Usage:
    uv run python web/server.py --model /path/to/model [--port 8720] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import mimetypes
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent
STATIC_DIR = WEB_DIR / "static"
SESSIONS_DIR = WEB_DIR / "sessions"

#: Subcommands the UI may run, and which of them take --model / --gemma / --quantize-on-load.
ALLOWED_COMMANDS = {
    "generate", "a2v", "retake", "extend", "keyframe", "ic-lora", "hdr-ic-lora", "lipdub",
    "enhance", "info", "preprocess", "slice", "train",
}  # fmt: skip
MODEL_COMMANDS = ALLOWED_COMMANDS - {"enhance", "slice", "train"}
GEMMA_COMMANDS = MODEL_COMMANDS - {"info"} | {"enhance"}
QUANTIZE_COMMANDS = {"generate", "a2v", "retake", "extend", "keyframe", "ic-lora", "hdr-ic-lora", "lipdub"}
SERVER_OWNED_FLAGS = {"--output", "-o", "--model", "-m", "--gemma", "--quantize-on-load", "--stepwise-image-output-dir"}
#: Subcommands whose pipelines accept the --stepwise-* live preview flags.
STEPWISE_COMMANDS = {"generate", "a2v", "retake", "extend", "keyframe", "ic-lora", "hdr-ic-lora", "lipdub"}
PREVIEW_NAME = re.compile(r"^seed_-?\d+(?:_s(\d+))?_step(\d+)of(\d+)\.webp$")

MEDIA_KINDS = {"inputs", "outputs", "timeline"}
VIDEO_EXTS = {".mp4", ".mov"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _load_launcher():
    """Reuse scripts/ltx_run.py's model inspection (filesystem only)."""
    spec = importlib.util.spec_from_file_location("ltx_run", REPO_ROOT / "scripts" / "ltx_run.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["ltx_run"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


LTX_RUN = _load_launcher()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def safe_name(text: str, default: str = "untitled") -> str:
    cleaned = SAFE_NAME.sub("-", text.strip()).strip(".-")
    return cleaned[:80] or default


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.replace(path)


def which(tool: str) -> str | None:
    return shutil.which(tool)


def ffprobe(path: Path) -> dict[str, Any]:
    """Duration, dimensions, fps and audio presence of a media file (empty dict on failure)."""
    if not which("ffprobe"):
        return {}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height,r_frame_rate,nb_frames:format=duration", "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=20,
        ).stdout  # fmt: skip
    except (subprocess.SubprocessError, OSError):
        return {}
    data = json.loads(out or "{}")
    info: dict[str, Any] = {}
    with contextlib.suppress(KeyError, TypeError, ValueError):
        info["duration"] = round(float(data["format"]["duration"]), 3)
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and "width" not in info:
            info["width"], info["height"] = stream.get("width"), stream.get("height")
            with contextlib.suppress(KeyError, ValueError, ZeroDivisionError, AttributeError):
                num, den = stream["r_frame_rate"].split("/")
                info["fps"] = round(float(num) / float(den), 3)
            with contextlib.suppress(KeyError, TypeError, ValueError):  # images have no nb_frames
                info["frames"] = int(stream["nb_frames"])
        if stream.get("codec_type") == "audio":
            info["has_audio"] = True
    return info


def video_thumbnail(src: Path) -> Path | None:
    """A cached 320px JPEG poster for a video (``.thumbs/<name>.jpg`` next to it), made on demand."""
    if not src.is_file():
        return None
    thumb = src.parent / ".thumbs" / f"{src.name}.jpg"
    if thumb.exists() and thumb.stat().st_mtime >= src.stat().st_mtime:
        return thumb
    if not which("ffmpeg"):
        return None
    thumb.parent.mkdir(exist_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-ss", "0.1", "-i", str(src), "-frames:v", "1",
         "-vf", "scale=320:-2", str(thumb)],
        capture_output=True, timeout=30,
    )  # fmt: skip
    return thumb if result.returncode == 0 and thumb.exists() else None


def cached_probe(path: Path) -> dict[str, Any]:
    """Probe via an existing sidecar (take/timeline ``x.json`` or input ``x.mp4.json``) before calling ffprobe."""
    for sidecar in (path.with_suffix(".json"), path.with_suffix(path.suffix + ".json")):
        probe = read_json(sidecar, {}).get("probe") if sidecar.exists() else None
        if probe:
            return probe
    return ffprobe(path)


def media_kind(name: str) -> str:
    mime = mimetypes.guess_type(name)[0] or ""
    if name.lower().endswith((".safetensors", ".yaml", ".yml", ".txt", ".json")):
        return "file"
    return mime.split("/")[0] if mime.split("/")[0] in {"image", "video", "audio"} else "file"


# ---------------------------------------------------------------------------
# state: config + sessions
# ---------------------------------------------------------------------------


class State:
    def __init__(self, model: str, gemma: str | None) -> None:
        self.lock = threading.Lock()
        self.model = model
        self.gemma = gemma
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        last = read_json(SESSIONS_DIR / "last_session.json", {}).get("name")
        self.active = last if last and (SESSIONS_DIR / last).is_dir() else None
        if self.active is None:
            existing = self.sessions()
            self.active = existing[0] if existing else "session-1"
        self.ensure_session(self.active)

    # sessions -------------------------------------------------------------
    def session_dir(self, name: str) -> Path:
        clean = safe_name(name, "session-1")
        path = (SESSIONS_DIR / clean).resolve()
        if path.parent != SESSIONS_DIR.resolve():
            raise ValueError("invalid session name")
        return path

    def ensure_session(self, name: str) -> Path:
        path = self.session_dir(name)
        for kind in MEDIA_KINDS:
            (path / kind).mkdir(parents=True, exist_ok=True)
        return path

    def sessions(self) -> list[str]:
        return sorted(p.name for p in SESSIONS_DIR.iterdir() if p.is_dir() and not p.name.startswith("."))

    def activate(self, name: str) -> dict[str, Any]:
        path = self.ensure_session(name)
        self.active = path.name
        write_json(SESSIONS_DIR / "last_session.json", {"name": path.name})
        return {"name": path.name, "settings": read_json(path / "setting.json", {})}

    def model_info(self) -> dict[str, Any]:
        info = LTX_RUN.inspect_model(self.model) if self.model else None
        if info is None:
            return {"model": "", "configured": False}
        return {
            "model": self.model,
            "configured": True,
            "local": info.local,
            "exists": Path(self.model).expanduser().exists() or not info.local,
            "has_distilled": info.has_distilled,
            "has_dev": info.has_dev,
            "is_25": info.is_25,
            "gemma": self.gemma or "",
        }

    def resolve_media(self, session: str, kind: str, name: str) -> Path:
        if kind not in MEDIA_KINDS:
            raise ValueError("invalid media kind")
        base = self.session_dir(session) / kind
        path = (base / Path(unquote(name)).name).resolve()
        if path.parent != base.resolve():
            raise ValueError("invalid media path")
        return path

    def thumbnail(self, session: str, kind: str, name: str) -> Path | None:
        return video_thumbnail(self.resolve_media(session, kind, name))

    def resolve_session_path(self, rel: str) -> Path:
        """Resolve a path relative to the sessions root, refusing anything that escapes it."""
        root = SESSIONS_DIR.resolve()
        path = (root / unquote(rel).lstrip("/")).resolve()
        if path != root and root not in path.parents:
            raise ValueError("path escapes the sessions directory")
        return path

    def list_media(self, session: str, kind: str) -> list[dict[str, Any]]:
        base = self.ensure_session(session) / kind
        items = []
        for path in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.is_dir() or path.name.startswith(".") or path.suffix in {".json", ".tmp"}:
                continue
            meta_path = path.with_suffix(path.suffix + ".json") if kind == "inputs" else path.with_suffix(".json")
            meta = read_json(meta_path, {})
            if "probe" not in meta and media_kind(path.name) in {"video", "audio", "image"}:
                meta["probe"] = ffprobe(path)
                with contextlib.suppress(OSError):
                    write_json(meta_path, meta)
            item = {
                "name": path.name,
                "kind": media_kind(path.name),
                "size": path.stat().st_size,
                "mtime": path.stat().st_mtime,
                "url": f"/media/{session}/{kind}/{path.name}",
                **meta,
            }
            if item["kind"] == "video":
                item["thumb"] = f"/thumb/{session}/{kind}/{path.name}?v={int(item['mtime'])}"
            items.append(item)
        return items


# ---------------------------------------------------------------------------
# runner: queue + subprocess + progress parsing
# ---------------------------------------------------------------------------

PHASE_RE = re.compile(r"^\[([^\]]+)\] \.\.\.$")
ESTIMATE_RE = re.compile(r"^\[estimate\] denoising(?: \(([^)]+)\))?: (\d+) steps")
TQDM_RE = re.compile(r"(Denoising[^:]*):\s+(\d+)%\|.*?\|\s*(\d+)/(\d+)")
SAVED_RE = re.compile(r"Saved to: (.+)$")


class Runner:
    def __init__(self, state: State) -> None:
        self.state = state
        self.jobs: dict[str, dict[str, Any]] = {}
        self.pending: list[str] = []
        self.current: str | None = None
        self.proc: subprocess.Popen | None = None
        self.cond = threading.Condition()
        self.subscribers: list[queue.Queue] = []
        self.sub_lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    # events ---------------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self.sub_lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.sub_lock, contextlib.suppress(ValueError):
            self.subscribers.remove(q)

    def emit(self, kind: str, payload: Any) -> None:
        message = json.dumps({"type": kind, "data": payload})
        with self.sub_lock:
            for q in list(self.subscribers):
                with contextlib.suppress(queue.Full):
                    q.put_nowait(message)

    def summary(self, job: dict[str, Any]) -> dict[str, Any]:
        keys = ("id", "session", "task_id", "label", "status", "created", "started", "finished",
                "elapsed", "output", "returncode", "progress", "seed", "argv_display", "error",
                "preview_latest", "preview_count")  # fmt: skip
        return {k: job.get(k) for k in keys}

    def queue_state(self) -> list[dict[str, Any]]:
        with self.cond:
            ids = ([self.current] if self.current else []) + list(self.pending)
            return [self.summary(self.jobs[i]) for i in ids if i in self.jobs]

    # submission -----------------------------------------------------------
    def build_argv(self, req: dict[str, Any], job_id: str | None = None) -> tuple[list[str], Path | None, Path]:
        subcommand = req.get("subcommand")
        if subcommand not in ALLOWED_COMMANDS:
            raise ValueError(f"unsupported command: {subcommand!r}")
        session = self.state.ensure_session(req.get("session") or self.state.active)
        argv: list[str] = [subcommand]
        skip_next = False
        for token in req.get("args", []):
            if skip_next:
                skip_next = False
                continue
            if isinstance(token, dict) and "input" in token:
                path = self.state.resolve_media(session.name, "inputs", token["input"])
                if not path.exists():
                    raise ValueError(f"input not found in session: {token['input']}")
                argv.append(str(path))
            elif isinstance(token, dict) and "path" in token:
                argv.append(str(Path(str(token["path"])).expanduser()))
            elif isinstance(token, (str, int, float)):
                text = str(token)
                if text in SERVER_OWNED_FLAGS:
                    skip_next = True
                    continue
                argv.append(text)
            else:
                raise ValueError(f"bad argument token: {token!r}")

        if subcommand in MODEL_COMMANDS:
            if not self.state.model:
                raise ValueError("no model configured — set it from the Model button")
            argv += ["--model", self.state.model]
        if subcommand in GEMMA_COMMANDS and self.state.gemma:
            argv += ["--gemma", self.state.gemma]
        if subcommand in QUANTIZE_COMMANDS and req.get("quantize") in {"8", "4", "none"}:
            argv += ["--quantize-on-load", req["quantize"]]
        if subcommand in STEPWISE_COMMANDS and isinstance(req.get("preview"), dict):
            argv += preview_args(req["preview"], session / "previews" / (job_id or uuid.uuid4().hex[:10]))

        output: Path | None = None
        kind = req.get("output", "mp4")
        stamp = datetime.now().strftime("%m%d-%H%M%S")
        stem = safe_name(req.get("take_name") or req.get("task_id") or "take", "take")
        if kind == "mp4":
            output = session / "outputs" / f"{stem}-{stamp}.mp4"
            argv += ["--output", str(output)]
        elif kind == "dir":
            output = session / "outputs" / f"{stem}-{stamp}"
            argv += ["--output", str(output)]
        return argv, output, session

    def submit(self, req: dict[str, Any]) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:10]
        argv, output, session = self.build_argv(req, job_id)
        preview_dir = session / "previews" / job_id if "--stepwise-image-output-dir" in argv else None
        job = {
            "id": job_id,
            "preview_dir": str(preview_dir) if preview_dir else None,
            "preview_count": 0,
            "session": session.name,
            "task_id": req.get("task_id"),
            "label": req.get("label") or req.get("task_id"),
            "status": "queued",
            "created": now_iso(),
            "argv": argv,
            "argv_display": "ltx-2-mlx " + " ".join(_quote(a) for a in argv),
            "output": str(output) if output else None,
            "output_kind": req.get("output", "mp4"),
            "params": req.get("params", {}),
            "seed": req.get("seed"),
            "progress": {"phase": "queued", "step": 0, "total": 0, "stage": 0},
        }
        with self.cond:
            self.jobs[job["id"]] = job
            self.pending.append(job["id"])
            self.cond.notify()
        self.emit("queue", self.queue_state())
        return self.summary(job)

    def cancel(self, job_id: str) -> bool:
        with self.cond:
            if job_id in self.pending:
                self.pending.remove(job_id)
                self.jobs[job_id]["status"] = "cancelled"
                if self.jobs[job_id].get("preview_dir"):
                    shutil.rmtree(self.jobs[job_id]["preview_dir"], ignore_errors=True)
                self.emit(
                    "queue",
                    [self.summary(self.jobs[i]) for i in ([self.current] if self.current else []) + self.pending],
                )
                return True
            if job_id == self.current and self.proc and self.proc.poll() is None:
                self.jobs[job_id]["status"] = "cancelling"
                _stop(self.proc)
                return True
        return False

    # worker ---------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            with self.cond:
                while not self.pending:
                    self.cond.wait()
                job_id = self.pending.pop(0)
                self.current = job_id
            try:
                self._run(self.jobs[job_id])
            except Exception as exc:
                job = self.jobs[job_id]
                job["status"], job["error"] = "failed", str(exc)
                if job.get("preview_dir"):
                    shutil.rmtree(job["preview_dir"], ignore_errors=True)
                self.emit("log", {"line": f"[studio] job failed to start: {exc}", "replace": False})
            finally:
                with self.cond:
                    self.current = None
                    self.proc = None
                self.emit("job", self.summary(self.jobs[job_id]))
                self.emit("queue", self.queue_state())

    def _run(self, job: dict[str, Any]) -> None:
        job["status"], job["started"] = "running", now_iso()
        started = time.monotonic()
        self.emit("job", self.summary(job))
        self.emit("queue", self.queue_state())
        self.emit("log", {"line": f"$ {job['argv_display']}", "replace": False, "kind": "cmd"})
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "TQDM_MININTERVAL": "0.5"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "ltx_pipelines_mlx", *job["argv"]],
            cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )  # fmt: skip
        tail: list[str] = []
        watcher_stop = threading.Event()
        watcher = None
        if job.get("preview_dir"):
            watcher = threading.Thread(target=self._watch_previews, args=(job, watcher_stop), daemon=True)
            watcher.start()
        self._pump(job, self.proc.stdout, tail)  # type: ignore[arg-type]
        returncode = self.proc.wait()
        if watcher is not None:
            watcher_stop.set()
            watcher.join(timeout=5)
        job["elapsed"] = round(time.monotonic() - started, 1)
        job["finished"], job["returncode"] = now_iso(), returncode
        cancelled = job["status"] == "cancelling"
        output = Path(job["output"]) if job.get("output") else None
        produced = output is not None and output.exists()
        if cancelled:
            job["status"] = "cancelled"
        elif returncode == 0 and (produced or job["output_kind"] == "none"):
            job["status"] = "done"
        else:
            job["status"] = "failed"
            job["error"] = next((line for line in reversed(tail) if line.strip()), f"exit code {returncode}")
        preview_dir = Path(job["preview_dir"]) if job.get("preview_dir") else None
        previews = list_previews(preview_dir) if preview_dir else []
        if job["output_kind"] == "mp4" and produced and output is not None:
            sidecar = {
                "task_id": job["task_id"], "label": job["label"], "created": job["created"],
                "elapsed": job["elapsed"], "seed": job.get("seed"), "params": job["params"],
                "argv": job["argv"], "probe": ffprobe(output),
            }  # fmt: skip
            if previews and preview_dir is not None:
                sidecar["previews"] = [_rel(p) for p in previews]
                sidecar["preview_dir"] = _rel(preview_dir)
            write_json(output.with_suffix(".json"), sidecar)
            self.emit("takes", {"session": job["session"]})
        if preview_dir is not None and (job["status"] != "done" or not previews):
            # Nothing owns these previews (no take, or none were written) — don't leave orphans.
            shutil.rmtree(preview_dir, ignore_errors=True)
        elif job["output_kind"] == "dir" and produced:
            self.emit("takes", {"session": job["session"]})
        state = job["status"]
        self.emit(
            "log", {"line": f"[studio] {job['label']}: {state} in {job['elapsed']}s", "replace": False, "kind": state}
        )

    def _watch_previews(self, job: dict[str, Any], stop: threading.Event) -> None:
        """Push each new stepwise preview to the browser as the pipeline writes it."""
        seen: set[str] = set()
        directory = Path(job["preview_dir"])
        while True:
            finished = stop.wait(0.5)
            for path in list_previews(directory):
                if path.name in seen:
                    continue
                seen.add(path.name)
                info = preview_info(path)
                job["preview_latest"] = info
                job["preview_count"] = len(seen)
                self.emit("preview", {"id": job["id"], "session": job["session"], "count": len(seen), **info})
            if finished:
                return

    def _pump(self, job: dict[str, Any], stream, tail: list[str]) -> None:
        buf = b""
        last_progress = 0.0
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                idx = min((i for i in (buf.find(b"\n"), buf.find(b"\r")) if i != -1), default=-1)
                if idx == -1:
                    break
                sep = buf[idx : idx + 1]
                line = buf[:idx].decode("utf-8", "replace")
                buf = buf[idx + 1 :]
                if not line.strip():
                    continue
                replace = sep == b"\r"
                if self._parse(job, line) and time.monotonic() - last_progress > 0.25:
                    last_progress = time.monotonic()
                    self.emit("progress", {"id": job["id"], "progress": job["progress"]})
                if not replace:
                    tail.append(line)
                    del tail[:-40]
                self.emit("log", {"line": line, "replace": replace})
        if buf.strip():
            line = buf.decode("utf-8", "replace")
            tail.append(line)
            self.emit("log", {"line": line, "replace": False})

    @staticmethod
    def _parse(job: dict[str, Any], line: str) -> bool:
        progress = job["progress"]
        text = line.strip()
        if m := ESTIMATE_RE.search(text):
            progress["stage"] += 1
            progress.update(phase=f"Denoising · stage {progress['stage']}", step=0, total=int(m.group(2)))
            return True
        if m := TQDM_RE.search(text):
            progress.update(step=int(m.group(3)), total=int(m.group(4)))
            return True
        if m := PHASE_RE.match(text):
            progress.update(phase=m.group(1), step=0, total=0)
            return True
        if text.startswith("[auto-duration]") or text.startswith("[official-weights]"):
            progress["note"] = text
            return True
        return False


def preview_args(options: dict[str, Any], directory: Path) -> list[str]:
    """``--stepwise-*`` flags for a live preview, validated into the ranges the CLI accepts."""

    def as_int(key: str, default: int | None, low: int, high: int) -> int | None:
        value = options.get(key, default)
        if value is None or value == "":
            return default
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"preview {key} must be an integer") from exc
        if not low <= number <= high:
            raise ValueError(f"preview {key} must be between {low} and {high}")
        return number

    directory.mkdir(parents=True, exist_ok=True)
    args = [
        "--stepwise-image-output-dir", str(directory),
        "--stepwise-interval", str(as_int("interval", 1, 1, 100)),
        "--stepwise-frames", str(as_int("frames", 8, 1, 32)),
    ]  # fmt: skip
    frame = as_int("frame", None, -512, 512)
    if frame is not None:
        args += ["--stepwise-frame", str(frame)]
    return args


def list_previews(directory: Path) -> list[Path]:
    """Finished preview files in write order (stage, then step). Temp files are skipped."""
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        if m := PREVIEW_NAME.match(path.name):
            found.append((int(m.group(1) or 0), int(m.group(2)), path))
    return [path for _, _, path in sorted(found)]


def preview_info(path: Path) -> dict[str, Any]:
    m = PREVIEW_NAME.match(path.name)
    stage, step, total = (int(m.group(1) or 0), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)
    rel = _rel(path)
    return {"url": f"/sfile/{rel}", "path": rel, "name": path.name, "stage": stage, "step": step, "total": total}


def _quote(arg: str) -> str:
    return arg if re.fullmatch(r"[A-Za-z0-9_./:=@+-]+", arg) else "'" + arg.replace("'", "'\\''") + "'"


def _stop(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)


# ---------------------------------------------------------------------------
# media operations (ffmpeg)
# ---------------------------------------------------------------------------


def _ffmpeg(*argv: str, timeout: float = 120) -> None:
    if not which("ffmpeg"):
        raise ValueError("ffmpeg is not on PATH")
    try:
        result = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", *argv], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"ffmpeg timed out after {timeout:.0f}s") from exc
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "ffmpeg failed")


def extract_frame(state: State, session: str, take: str, position: str) -> dict[str, Any]:
    src = state.resolve_media(session, "outputs", take)
    dst = state.session_dir(session) / "inputs" / f"{Path(take).stem}-{position}.png"
    if position == "first":
        _ffmpeg("-i", str(src), "-frames:v", "1", str(dst))
    else:
        _ffmpeg("-sseof", "-0.1", "-i", str(src), "-frames:v", "1", "-update", "1", str(dst))
    return {"name": dst.name}


def extract_audio(state: State, session: str, take: str) -> dict[str, Any]:
    src = state.resolve_media(session, "outputs", take)
    dst = state.session_dir(session) / "inputs" / f"{Path(take).stem}-audio.wav"
    _ffmpeg("-i", str(src), "-vn", "-ac", "2", str(dst))
    return {"name": dst.name}


def media_to_input(state: State, session: str, kind: str, name: str) -> dict[str, Any]:
    src = state.resolve_media(session, kind, name)
    dst = state.session_dir(session) / "inputs" / src.name
    if dst.exists():
        dst = dst.with_name(f"{dst.stem}-{uuid.uuid4().hex[:4]}{dst.suffix}")
    shutil.copy2(src, dst)
    return {"name": dst.name}


# ---------------------------------------------------------------------------
# timeline: browse clips across sessions, combine them into one video
# ---------------------------------------------------------------------------


def _rel(path: Path) -> str:
    return path.resolve().relative_to(SESSIONS_DIR.resolve()).as_posix()


def browse_timeline(state: State, session: str, rel: str) -> dict[str, Any]:
    """List folders and video clips under the sessions root.

    ``rel == ""`` opens the current session's outputs, ``"."`` the sessions
    root, anything else is a path relative to the sessions root.
    """
    if rel == "":
        target = state.ensure_session(session) / "outputs"
    elif rel == ".":
        target = SESSIONS_DIR.resolve()
    else:
        target = state.resolve_session_path(rel)
    if not target.is_dir():
        raise ValueError("directory not found")
    root = SESSIONS_DIR.resolve()
    dirs, files = [], []
    for entry in sorted(target.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or (entry.is_dir() and entry.name == "previews"):
            continue
        if entry.is_dir():
            dirs.append({"name": entry.name, "path": _rel(entry)})
        elif entry.suffix.lower() in VIDEO_EXTS:
            rel_path = _rel(entry)
            probe = cached_probe(entry)
            files.append({
                "name": entry.name, "path": rel_path, "size": entry.stat().st_size, "mtime": entry.stat().st_mtime,
                "duration": probe.get("duration"), "width": probe.get("width"), "height": probe.get("height"),
                "url": f"/sfile/{rel_path}", "thumb": f"/sthumb/{rel_path}?v={int(entry.stat().st_mtime)}",
            })  # fmt: skip
    resolved = target.resolve()
    return {
        "path": "." if resolved == root else _rel(resolved),
        "parent": None if resolved == root else ("." if resolved.parent == root else _rel(resolved.parent)),
        "dirs": dirs,
        "files": files,
    }


def combine_timeline(state: State, session: str, clips: list[str], name: str) -> dict[str, Any]:
    """Concatenate clips (paths relative to the sessions root) into ``<session>/timeline/<name>.mp4``.

    Clips are letterboxed onto the largest canvas, resampled to 24 fps, and clips
    without audio get matching silence so the audio track stays continuous. Sources
    are only read, never modified.
    """
    if not clips:
        raise ValueError("pick at least one clip")
    if not which("ffmpeg"):
        raise ValueError("ffmpeg is not on PATH")
    sources = []
    for rel in clips:
        path = state.resolve_session_path(rel)
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
            raise ValueError(f"not a video clip: {rel}")
        sources.append((path, ffprobe(path)))
    width = max((p.get("width") or 0) for _, p in sources) or 704
    height = max((p.get("height") or 0) for _, p in sources) or 448
    width, height = width + width % 2, height + height % 2

    out_dir = state.ensure_session(session) / "timeline"
    stem = safe_name(name, f"timeline-{datetime.now():%m%d-%H%M%S}")
    out = out_dir / f"{stem}.mp4"
    counter = 1
    while out.exists():
        out = out_dir / f"{stem}-{counter}.mp4"
        counter += 1

    args: list[str] = []
    for path, _ in sources:
        args += ["-i", str(path)]
    silent: dict[int, int] = {}
    for i, (_, probe) in enumerate(sources):
        if not probe.get("has_audio"):
            silent[i] = len(sources) + len(silent)
            args += ["-f", "lavfi", "-t", f"{probe.get('duration') or 1:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    filters, refs = [], ""
    for i in range(len(sources)):
        filters.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24[v{i}]"
        )
        filters.append(f"[{silent.get(i, i)}:a]aformat=sample_rates=48000:channel_layouts=stereo[a{i}]")
        refs += f"[v{i}][a{i}]"
    filters.append(f"{refs}concat=n={len(sources)}:v=1:a=1[vout][aout]")
    _ffmpeg(*args, "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", str(out), timeout=600)  # fmt: skip
    write_json(out.with_suffix(".json"), {"clips": clips, "created": now_iso(), "probe": ffprobe(out)})
    return {"name": out.name}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "ltx-studio"
    state: State
    runner: Runner

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet access log
        return

    # plumbing -------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _error(self, message: str, code: int = 400) -> None:
        self._json({"error": message}, code)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _session(self, params: dict[str, Any]) -> str:
        return safe_name(str(params.get("session") or self.state.active), "session-1")

    # routes ---------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/") :])
            if path.startswith("/media/"):
                _, _, session, kind, name = path.split("/", 4)
                return self._file(self.state.resolve_media(session, kind, name))
            if path.startswith("/thumb/"):
                _, _, session, kind, name = path.split("/", 4)
                thumb = self.state.thumbnail(session, kind, name)
                return self._file(thumb) if thumb else self._error("no thumbnail", 404)
            if path.startswith("/sfile/"):
                return self._file(self.state.resolve_session_path(path[len("/sfile/") :]))
            if path.startswith("/sthumb/"):
                thumb = video_thumbnail(self.state.resolve_session_path(path[len("/sthumb/") :]))
                return self._file(thumb) if thumb else self._error("no thumbnail", 404)
            if path == "/api/timeline":
                return self._json(self.state.list_media(self._session(query), "timeline"))
            if path == "/api/timeline/browse":
                return self._json(browse_timeline(self.state, self._session(query), query.get("path", "")))
            if path == "/api/events":
                return self._events()
            if path == "/api/config":
                return self._json({
                    "model": self.state.model_info(), "sessions": self.state.sessions(),
                    "active": self.state.active, "ffmpeg": bool(which("ffmpeg")), "repo": str(REPO_ROOT),
                })  # fmt: skip
            if path == "/api/sessions":
                return self._json({"sessions": self.state.sessions(), "active": self.state.active})
            if path == "/api/inputs":
                return self._json(self.state.list_media(self._session(query), "inputs"))
            if path == "/api/takes":
                items = [t for t in self.state.list_media(self._session(query), "outputs") if t["kind"] == "video"]
                return self._json(items)
            if path == "/api/queue":
                return self._json(self.runner.queue_state())
            return self._error("not found", 404)
        except ValueError as exc:
            return self._error(str(exc))
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as exc:  # never drop the connection without a response
            return self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        try:
            if path == "/api/upload":
                return self._upload(query)
            body = self._body()
            session = self._session(body)
            if path == "/api/render":
                jobs = [self.runner.submit(req) for req in body.get("jobs", [body])]
                return self._json({"jobs": jobs})
            if path == "/api/cancel":
                return self._json({"ok": self.runner.cancel(str(body.get("id")))})
            if path == "/api/model":
                model = str(body.get("model", "")).strip()
                if model and not Path(model).expanduser().exists() and "/" not in model:
                    return self._error("model path does not exist")
                with self.state.lock:
                    self.state.model = model
                    self.state.gemma = str(body.get("gemma", "")).strip() or None
                return self._json(self.state.model_info())
            if path == "/api/session/activate":
                return self._json(self.state.activate(session))
            if path == "/api/session/save":
                write_json(self.state.ensure_session(session) / "setting.json", body.get("settings", {}))
                return self._json({"ok": True})
            if path == "/api/session/duplicate":
                dst = self.state.session_dir(str(body.get("new_name", "")))
                if dst.exists():
                    return self._error("a session with that name already exists")
                shutil.copytree(self.state.ensure_session(session), dst)
                return self._json(self.state.activate(dst.name))
            if path == "/api/session/delete":
                target = self.state.session_dir(session)
                if target.exists():
                    shutil.rmtree(target)
                remaining = self.state.sessions()
                return self._json(self.state.activate(remaining[0] if remaining else "session-1"))
            if path == "/api/inputs/delete":
                target = self.state.resolve_media(session, "inputs", str(body.get("name")))
                target.unlink(missing_ok=True)
                target.with_suffix(target.suffix + ".json").unlink(missing_ok=True)
                return self._json({"ok": True})
            if path == "/api/takes/delete":
                target = self.state.resolve_media(session, "outputs", str(body.get("name")))
                preview_dir = read_json(target.with_suffix(".json"), {}).get("preview_dir")
                if preview_dir:
                    previews = self.state.resolve_session_path(preview_dir)
                    if previews.parent == self.state.session_dir(session) / "previews" and previews.is_dir():
                        shutil.rmtree(previews)
                target.unlink(missing_ok=True)
                target.with_suffix(".json").unlink(missing_ok=True)
                return self._json({"ok": True})
            if path == "/api/frame":
                return self._json(extract_frame(self.state, session, str(body["take"]), body.get("position", "last")))
            if path == "/api/audio":
                return self._json(extract_audio(self.state, session, str(body["take"])))
            if path == "/api/use-video":
                kind = body.get("kind", "outputs")
                return self._json(media_to_input(self.state, session, kind, str(body.get("name") or body["take"])))
            if path == "/api/timeline/render":
                result = combine_timeline(self.state, session, list(body.get("clips", [])), str(body.get("name", "")))
                self.runner.emit("timeline", {"session": session, "name": result["name"]})
                return self._json(result)
            if path == "/api/timeline/delete":
                target = self.state.resolve_media(session, "timeline", str(body.get("name")))
                target.unlink(missing_ok=True)
                target.with_suffix(".json").unlink(missing_ok=True)
                return self._json({"ok": True})
            return self._error("not found", 404)
        except (ValueError, KeyError) as exc:
            return self._error(str(exc))
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as exc:  # never drop the connection without a response
            return self._error(f"{type(exc).__name__}: {exc}", 500)

    # handlers -------------------------------------------------------------
    def _static(self, rel: str) -> None:
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return self._error("not found", 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        return self._send(200, target.read_bytes(), ctype)

    def _file(self, target: Path) -> None:
        if not target.is_file():
            return self._error("not found", 404)
        size = target.stat().st_size
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        code = 200
        if rng := re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "")):
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            elif rng.group(2):
                start = max(0, size - int(rng.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return None
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return None
        with open(target, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1 << 16, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        return None

    def _upload(self, query: dict[str, str]) -> None:
        session = self._session(query)
        original = Path(unquote(query.get("name", "upload"))).name
        stem, suffix = os.path.splitext(original)
        dst = self.state.ensure_session(session) / "inputs" / f"{safe_name(stem, 'upload')}{suffix.lower()}"
        if dst.exists():
            dst = dst.with_name(f"{dst.stem}-{uuid.uuid4().hex[:4]}{dst.suffix}")
        remaining = int(self.headers.get("Content-Length") or 0)
        with open(dst, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        meta = {"original_name": original, "probe": ffprobe(dst) if media_kind(dst.name) != "file" else {}}
        write_json(dst.with_suffix(dst.suffix + ".json"), meta)
        return self._json({"name": dst.name, "kind": media_kind(dst.name), **meta})

    def _events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.runner.subscribe()
        try:
            self.wfile.write(f"data: {json.dumps({'type': 'queue', 'data': self.runner.queue_state()})}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    message = q.get(timeout=15)
                    self.wfile.write(f"data: {message}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.runner.unsubscribe(q)


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Browsers abort media range requests all the time; that is not an error.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def main() -> None:
    parser = argparse.ArgumentParser(description="ltx studio web UI")
    parser.add_argument("--model", default=os.environ.get("LTX_MODEL", ""), help="model dir or HF repo (env LTX_MODEL)")
    parser.add_argument("--gemma", default=os.environ.get("LTX_GEMMA"), help="Gemma 3 repo for LTX-2.3 packs / enhance")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8720)
    args = parser.parse_args()

    state = State(args.model, args.gemma)
    Handler.state = state
    Handler.runner = Runner(state)
    httpd = StudioServer((args.host, args.port), Handler)
    print(f"ltx studio on http://{args.host}:{args.port}  (model: {args.model or 'not set'})", flush=True)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: no authentication — anyone who can reach this port can run jobs", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        httpd.serve_forever()


if __name__ == "__main__":
    main()
