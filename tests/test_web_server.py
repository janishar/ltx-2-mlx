"""Unit tests for web/server.py (ltx studio). Stdlib only: no MLX, no HTTP server, no weights."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ltx_studio_server", REPO_ROOT / "web" / "server.py")
assert _spec is not None and _spec.loader is not None
server = importlib.util.module_from_spec(_spec)
sys.modules["ltx_studio_server"] = server
_spec.loader.exec_module(server)


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "sessions")
    return server.State("/models/ltx-2.5", None)


@pytest.fixture
def runner(state):
    r = server.Runner.__new__(server.Runner)  # no worker thread
    r.state = state
    r.jobs, r.pending, r.current = {}, [], None
    r.cond, r.job_lock, r.sub_lock, r.subscribers = (
        server.threading.Condition(),
        server.threading.Lock(),
        server.threading.Lock(),
        [],
    )
    return r


def test_safe_name():
    assert server.safe_name("my shot/../x") == "my-shot-..-x"
    assert server.safe_name("  ") == "untitled"


def test_session_paths_stay_inside_sessions(state):
    assert state.session_dir("../../etc").parent == server.SESSIONS_DIR.resolve()
    with pytest.raises(ValueError):
        state.resolve_media("session-1", "secrets", "x.png")
    resolved = state.resolve_media("session-1", "inputs", "../../outputs/evil.png")
    assert resolved.parent == state.session_dir("session-1") / "inputs"


def test_build_argv_generate(state, runner):
    session = state.ensure_session("session-1")
    (session / "inputs" / "face.png").write_bytes(b"")
    argv, output, _ = runner.build_argv({
        "session": "session-1", "subcommand": "generate", "quantize": "8", "take_name": "shot 1",
        "args": ["--prompt", "hi", "--distilled", "--image", {"input": "face.png"}, "0", "1.0",
                 "--output", "/tmp/elsewhere.mp4", "--model", "other/model"],
    })  # fmt: skip
    assert argv[0] == "generate"
    assert str(session / "inputs" / "face.png") in argv
    assert "/tmp/elsewhere.mp4" not in argv and "other/model" not in argv
    assert argv[argv.index("--model") + 1] == "/models/ltx-2.5"
    assert argv[argv.index("--quantize-on-load") + 1] == "8"
    assert output is not None and output.parent == session / "outputs" and output.name.startswith("shot-1-")
    assert argv[argv.index("--output") + 1] == str(output)


def test_build_argv_rejects_bad_requests(state, runner):
    with pytest.raises(ValueError, match="unsupported command"):
        runner.build_argv({"subcommand": "rm", "args": []})
    with pytest.raises(ValueError, match="input not found"):
        runner.build_argv({"subcommand": "generate", "args": [{"input": "missing.png"}]})
    with pytest.raises(ValueError, match="bad argument"):
        runner.build_argv({"subcommand": "generate", "args": [["nested"]]})


def test_build_argv_tools_have_no_model_or_output(state, runner):
    argv, output, _ = runner.build_argv({"subcommand": "enhance", "output": "none", "args": ["--prompt", "x"]})
    assert "--model" not in argv and "--output" not in argv and output is None
    argv, output, _ = runner.build_argv({"subcommand": "slice", "output": "dir", "args": [{"path": "~/clips"}]})
    assert "--model" not in argv and output is not None and str(Path("~/clips").expanduser()) in argv


def test_progress_parsing():
    job = {"progress": {"phase": "", "step": 0, "total": 0, "stage": 0}}
    assert server.Runner._parse(job, "[Loading transformer (transformer.safetensors)] ...")
    assert job["progress"]["phase"].startswith("Loading transformer")
    assert server.Runner._parse(job, "[estimate] denoising (ancestral): 8 steps x 1 passes over 539 video tokens")
    assert job["progress"]["stage"] == 1 and job["progress"]["total"] == 8
    assert server.Runner._parse(job, "Denoising (ancestral):  50%|█████     | 4/8 [00:04<00:04,  1.0s/it]")
    assert (job["progress"]["step"], job["progress"]["total"]) == (4, 8)
    assert not server.Runner._parse(job, "some unrelated line")


def test_ffprobe_missing_file_is_empty(tmp_path: Path):
    assert server.ffprobe(tmp_path / "nope.mp4") == {}


def test_session_root_paths_cannot_escape(state):
    root = server.SESSIONS_DIR.resolve()
    assert state.resolve_session_path("session-1/outputs") == root / "session-1" / "outputs"
    assert state.resolve_session_path("") == root
    for bad in ("../secrets", "session-1/../../etc/passwd", "%2e%2e/etc"):
        with pytest.raises(ValueError):
            state.resolve_session_path(bad)


def test_browse_timeline(state):
    outputs = state.ensure_session("session-1") / "outputs"
    (outputs / "take.mp4").write_bytes(b"")
    (outputs / "notes.txt").write_text("not a clip")
    (outputs / ".thumbs").mkdir()
    (outputs / "preprocessed").mkdir()

    default = server.browse_timeline(state, "session-1", "")
    assert default["path"] == "session-1/outputs" and default["parent"] == "session-1"
    assert [f["name"] for f in default["files"]] == ["take.mp4"]
    assert default["files"][0]["url"] == "/sfile/session-1/outputs/take.mp4"
    assert [d["name"] for d in default["dirs"]] == ["preprocessed"]

    root = server.browse_timeline(state, "session-1", ".")
    assert root["path"] == "." and root["parent"] is None
    assert [d["name"] for d in root["dirs"]] == ["session-1"]

    session = server.browse_timeline(state, "session-1", "session-1")
    assert session["parent"] == "."
    with pytest.raises(ValueError):
        server.browse_timeline(state, "session-1", "../..")


def test_combine_timeline_validates_clips(state):
    with pytest.raises(ValueError, match="at least one"):
        server.combine_timeline(state, "session-1", [], "x")
    state.ensure_session("session-1")
    with pytest.raises(ValueError):
        server.combine_timeline(state, "session-1", ["session-1/outputs/missing.mp4"], "x")
    with pytest.raises(ValueError):
        server.combine_timeline(state, "session-1", ["../../etc/passwd"], "x")


def test_preview_args_and_validation(state, runner, tmp_path: Path):
    argv, _, session = runner.build_argv(
        {
            "subcommand": "generate",
            "args": ["--stepwise-image-output-dir", "/tmp/elsewhere"],
            "preview": {"interval": 2, "frames": 3, "frame": -1},
        },
        job_id="job123",
    )
    directory = session / "previews" / "job123"
    assert argv[argv.index("--stepwise-image-output-dir") + 1] == str(directory) and directory.is_dir()
    assert "/tmp/elsewhere" not in argv
    assert argv[argv.index("--stepwise-interval") + 1] == "2"
    assert argv[argv.index("--stepwise-frames") + 1] == "3"
    assert argv[argv.index("--stepwise-frame") + 1] == "-1"

    argv, _, _ = runner.build_argv({"subcommand": "generate", "args": [], "preview": {"frame": None}})
    assert "--stepwise-frame" not in argv and argv[argv.index("--stepwise-frames") + 1] == "8"
    argv, _, _ = runner.build_argv({"subcommand": "enhance", "output": "none", "args": [], "preview": {}})
    assert "--stepwise-image-output-dir" not in argv

    for bad in ({"interval": 0}, {"frames": 99}, {"frames": "many"}):
        with pytest.raises(ValueError):
            server.preview_args(bad, tmp_path / "p")


def test_list_previews_orders_by_stage_then_step(state):
    directory = state.ensure_session("session-1") / "previews" / "job"
    directory.mkdir(parents=True)
    for name in ("seed_1_s2_step001of003.webp", "seed_1_s1_step010of008.webp", "seed_1_s1_step002of008.webp",
                 "seed_1_s1_step003of008.webp.tmp", "notes.txt"):  # fmt: skip
        (directory / name).write_bytes(b"")
    names = [p.name for p in server.list_previews(directory)]
    assert names == ["seed_1_s1_step002of008.webp", "seed_1_s1_step010of008.webp", "seed_1_s2_step001of003.webp"]
    info = server.preview_info(directory / "seed_1_s2_step001of003.webp")
    assert (info["stage"], info["step"], info["total"]) == (2, 1, 3)
    assert info["url"] == "/sfile/session-1/previews/job/seed_1_s2_step001of003.webp"
    single = server.preview_info(directory / "seed_-5_step004of008.webp")
    assert (single["stage"], single["step"], single["total"]) == (0, 4, 8)


def test_request_guard_blocks_rebinding_and_cross_site():
    allowed = {"127.0.0.1", "studio.lan"}
    guard = server.request_guard
    json_post = {"Host": "127.0.0.1:8720", "Origin": "http://127.0.0.1:8720", "Content-Type": "application/json"}
    assert guard("POST", "/api/render", json_post, allowed) is None
    assert guard("GET", "/api/config", {"Host": "localhost:8720"}, allowed) is None
    assert guard("GET", "/api/config", {"Host": "[::1]:8720"}, allowed) is None
    assert guard("GET", "/api/config", {"Host": "studio.lan:8720"}, allowed) is None
    assert guard("GET", "/sfile/x", {"Host": "evil.example:8720"}, allowed)[0] == 403
    assert guard("GET", "/", {}, allowed)[0] == 403
    assert guard("POST", "/api/render", {**json_post, "Origin": "http://evil.example"}, allowed)[0] == 403
    assert guard("POST", "/api/render", {**json_post, "Origin": "null"}, allowed)[0] == 403
    no_origin = {"Host": "127.0.0.1:8720", "Content-Type": "application/json"}
    assert guard("POST", "/api/render", no_origin, allowed) is None
    assert guard("POST", "/api/render", {**no_origin, "Sec-Fetch-Site": "cross-site"}, allowed)[0] == 403
    # A no-cors form/fetch post can only send "simple" content types.
    assert guard("POST", "/api/session/delete", {**json_post, "Content-Type": "text/plain"}, allowed)[0] == 415
    assert guard("POST", "/api/session/delete", {"Host": "127.0.0.1:8720"}, allowed)[0] == 415
    assert guard("POST", "/api/upload?session=s", {"Host": "127.0.0.1:8720"}, allowed)[0] == 400
    assert guard("POST", "/api/upload?session=s", {"Host": "127.0.0.1:8720", "X-Filename": "a.png"}, allowed) is None


def test_progress_eta_and_stages():
    job = {"progress": {"phase": "", "step": 0, "total": 0, "stage": 0, "stage_key": None}}
    parse = server.Runner._parse
    assert parse(job, "[Loading text encoder (Gemma)] ...") and job["progress"]["stage_key"] == "encode"
    assert (
        parse(job, "[Loading transformer (transformer-dev.safetensors)] ...") and job["progress"]["stage_key"] == "load"
    )
    assert parse(job, "[estimate] denoising: 30 steps x 2 passes over 1650 video + 200 audio tokens = 60 forwards")
    assert job["progress"]["stage_key"] == "denoise"
    assert parse(job, "[estimate] denoising: ~1 min 30 s remaining (1.5 s/forward) (refined)")
    assert job["progress"]["eta_s"] == 90 and job["progress"]["eta_at"] > 0
    # Stage 2 reloads the transformer: the stepper never moves backwards, and the old ETA is dropped.
    assert parse(job, "[Loading transformer (transformer-distilled.safetensors)] ...")
    assert job["progress"]["stage_key"] == "denoise"
    assert parse(job, "[estimate] denoising: 3 steps x 1 passes over 6600 video + 200 audio tokens = 3 forwards")
    assert job["progress"]["stage"] == 2 and "eta_s" not in job["progress"]
    assert parse(job, "[Loading decoders (VAE + audio + vocoder)] ...") and job["progress"]["stage_key"] == "decode"
    assert parse(job, "[Decoding video + audio + muxing] ...") and job["progress"]["stage_key"] == "decode"
    assert parse(job, "Saved to: /tmp/x.mp4") and job["progress"]["stage_key"] == "save"


def test_parse_duration():
    assert server.parse_duration("5 s") == 5
    assert server.parse_duration("1 min 30 s") == 90
    assert server.parse_duration("1 h 30 min") == 5400
    assert server.parse_duration("soon") is None


def test_failure_hints():
    watchdog = ["Loading...", "libc++abi: [METAL] Command buffer execution failed: Impacting Interactivity (0000000e)"]
    assert "watchdog" in server.hint_for(watchdog)
    oom = [
        "[METAL] Command buffer execution failed: Insufficient Memory (00000008:kIOGPUCommandBufferCallbackErrorOutOfMemory)"
    ]
    assert "memory" in server.hint_for(oom)
    assert "DurationHead" in server.hint_for(["ValueError: ... Pass num_frames explicitly."])
    assert "dev transformer" in server.hint_for(["FileNotFoundError: --dev-transformer 'x' not found in model dir: /m"])
    # Normal loading lines that merely mention the dev transformer don't trigger that hint.
    assert server.hint_for(["[Loading transformer (transformer-dev.safetensors)] ...", "KeyError: 'x'"]) == ""
    tail = ["[Loading transformer] ...", "Traceback (most recent call last):", "ValueError: bad size", "  at end"]
    assert server.last_error_line(tail, 1) == "ValueError: bad size"
    assert server.last_error_line([], 3) == "exit code 3"


def _take(outputs: Path, name: str, elapsed: float, width: int, frames: int, task: str = "t2v") -> None:
    (outputs / f"{name}.mp4").write_bytes(b"")
    params = {"taskId": task, "common": {"width": width, "height": 448, "frames": frames, "quantize": "8"},
              "values": {task: {"pipeline": "distilled", "prompt_unrelated": "x"}}}  # fmt: skip
    server.write_json(outputs / f"{name}.json", {"elapsed": elapsed, "params": params})


def test_estimate_from_session_takes(state):
    outputs = state.ensure_session("session-1") / "outputs"
    params = {"taskId": "t2v", "common": {"width": 704, "height": 448, "frames": 49, "quantize": "8"},
              "values": {"t2v": {"pipeline": "distilled"}}}  # fmt: skip
    assert state.estimate("session-1", params) == {}
    _take(outputs, "a", 100, 704, 97)
    scaled = state.estimate("session-1", params)
    assert scaled == {"seconds": 51, "samples": 1, "exact": False}
    _take(outputs, "b", 40, 704, 49)
    _take(outputs, "c", 60, 704, 49)
    _take(outputs, "d", 999, 704, 49, task="i2v")
    assert state.estimate("session-1", params) == {"seconds": 50, "samples": 2, "exact": True}
    assert state.estimate("session-1", {"common": {}}) == {}


def test_star_round_trip_and_terminal_log(state):
    outputs = state.ensure_session("session-1") / "outputs"
    _take(outputs, "take", 10, 704, 49)
    assert state.set_star("session-1", "take.mp4", True)["starred"]
    item = next(t for t in state.list_media("session-1", "outputs") if t["name"] == "take.mp4")
    assert item["starred"] is True and item["elapsed"] == 10
    with pytest.raises(ValueError):
        state.set_star("session-1", "missing.mp4", True)

    for i in range(3):
        state.append_terminal("session-1", f"line {i}")
    assert state.terminal_tail("session-1", 2) == ["line 1", "line 2"]
    assert state.terminal_tail("nope") == []


def test_setup_checks(state, tmp_path: Path):
    pack = tmp_path / "pack"
    pack.mkdir()
    for name in ("transformer-distilled.safetensors", "vae_decoder.safetensors"):
        (pack / name).write_bytes(b"")
    checks = {c["label"]: c for c in server.setup_checks(server.LTX_RUN.inspect_model(str(pack)))}
    assert checks["transformer"]["level"] == "ok" and checks["VAE decoder"]["level"] == "ok"
    assert checks["upscaler"]["level"] == "warn"
    assert server.setup_checks(server.LTX_RUN.inspect_model("/does/not/exist"))[0]["level"] == "bad"
    assert server.setup_checks(server.LTX_RUN.inspect_model("Lightricks/LTX-2.5"))[0]["level"] == "info"
    assert server.setup_checks(None)[0]["level"] == "bad"
    assert server.checks_status([{"level": "ok"}, {"level": "info"}]) == "warn"


def test_jobs_submitted_together_get_distinct_outputs(state, runner):
    """Queue 3 seeds submits in the same second; each take needs its own file."""
    req = {"session": "session-1", "subcommand": "generate", "task_id": "t2v", "args": ["--prompt", "x"]}
    outputs = [runner.submit({**req, "seed": seed})["output"] for seed in (1, 2, 3)]
    assert len(set(outputs)) == 3
    assert all(Path(o).parent == state.session_dir("session-1") / "outputs" for o in outputs)
    job = runner.jobs[runner.pending[0]]
    assert runner.summary(job)["progress"]["stage_key"] is None
