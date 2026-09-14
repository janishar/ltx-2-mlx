# ltx studio

A local web control surface for every `ltx-2-mlx` command — text, image, audio
and video to video, retake/extend, keyframes, IC-LoRA control, prompt tools and
training — from one browser tab, with nothing sent off your machine.

It is a Python **stdlib-only** server (`server.py`, no FastAPI/uvicorn, so it
runs in the repo's existing uv environment) plus a no-build-step vanilla JS
front end (`static/`). The layout and palette follow
[h3 studio](https://github.com/janishar/h3c-studio); the declarative task
catalog follows AuK Studio.

## Running

```bash
bash web/run.sh --model /path/to/model            # port 8720, 127.0.0.1
# or
LTX_MODEL=/path/to/model uv run python web/server.py --port 8720
```

Open http://127.0.0.1:8720. `--model` accepts an mlx-forge pack directory, a
directory of the official Lightricks LTX-2.5 files (converted and quantized on
load, see the main README), or a Hugging Face repo id. It can also be changed
from the **Model** button, which shows what the model can run (distilled / dev
transformer, LTX-2.5 vs 2.3). `--gemma` sets the Gemma 3 repo used by LTX-2.3
packs and prompt enhancement.

There is **no authentication** — keep it bound to `127.0.0.1`.

### Run from VS Code

`.vscode/launch.json` ships ready-made configurations for the Python Debugger
extension (`Cmd+Shift+D`, pick one, `F5`). They use the repo's `.venv`
interpreter, so no setup beyond `uv sync` is needed.

| Configuration | What it does |
| --- | --- |
| **ltx studio (dev)** | Runs `web/server.py` on `127.0.0.1:8720` — the everyday config. Static files are served uncached, so front-end edits only need a browser refresh; restart for server changes. |
| **ltx studio (custom paths)** | Same, but prompts for the model, Gemma repo and port instead of using the hardcoded model path. |
| **ltx studio (debug - step into ltx packages)** | `justMyCode: false`, for stepping into `ltx_core_mlx` / `ltx_pipelines_mlx` from the server. Render jobs run in child processes, so breakpoints inside a render need the **ltx-2-mlx: generate** config instead. |
| **ltx studio (LAN - 0.0.0.0, no auth)** | Binds to all interfaces — anyone who can reach the port can run jobs. |
| **ltx-run: modes** | Prints which tasks the chosen model supports. |
| **ltx-2-mlx: generate (prompt)** | Runs one distilled generation in-process under the debugger (prompts for model and prompt), writing `outputs/vscode-generate.mp4`. |
| **pytest: fast suite** | `pytest -m "not slow"` under the debugger. |

All except "custom paths" and the prompting configs have `--model` hardcoded to
a sample path — edit it in `.vscode/launch.json`, or use "custom paths".
`.vscode/tasks.json` adds **run: ltx studio**, **setup: uv sync**,
**test: fast suite** (default test task) and **lint: ruff** (default build task).

## Using it

1. Pick a **task**. Tasks are grouped: Generate (text, image, first+last frame,
   multi-image anchors, prompt beats), Audio, Edit Video (retake, extend,
   keyframe interpolation), Control (IC-LoRA control, HDR, lip dub), Tools
   (prompt enhance, model info) and Training (slice, preprocess, train). Tasks
   the current model can't run explain why and stay disabled.
2. Drop or browse **inputs** (images, videos, audio). They land in the
   session's `inputs/`; click one to fill the next empty slot of the task, or
   pick it from a slot's menu.
3. Fill in the prompt, canvas (presets up to 1080p), duration on the 8k+1 frame
   grid (or LTX-2.5 auto duration), seed, and task options. **Advanced** holds
   sampler knobs, LoRAs, quantize-on-load, low-RAM streaming, tiling and extra
   raw arguments. **Command** shows the exact `ltx-2-mlx` invocation.
4. **Render** (or ⌘/Ctrl+Enter) queues the job; **Queue 3 seeds** queues three
   random seeds. One job runs at a time; the stage shows the phase, denoising
   step progress, elapsed time and a **Stop** button, and the terminal streams
   the live log.
5. Every take appears on the right with its settings. From a take you can
   **Reuse settings**, **Chain →** (last frame becomes the start image of
   Image → Video), pull its first/last frame, the video itself (for retake,
   extend or control) or its audio (for audio → video) into inputs.

## Timeline

**Create Timeline** opens a full-screen editor for joining clips into one
video:

- **Browse** (left) — starts in the current session's `outputs/`; the
  breadcrumb walks up to `sessions` and into any session's `outputs/`,
  `timeline/` or `inputs/`. Click a clip to append it to the queue; a clip can
  be added more than once (the badge counts uses).
- **Queue** (middle) — numbered in play order with the running total length.
  Drag to reorder, ✕ to remove, **Clear** to start over.
- **Combined result** (right) — name the output and **Combine**. ffmpeg
  letterboxes every clip onto the largest width/height in the queue, resamples
  to 24 fps, adds silence for clips without audio, and writes
  `timeline/<name>.mp4` plus a `.json` sidecar listing the source clips. The
  result plays here and appears in the **Timeline** list under Takes.

Source clips are only read, never modified. A combined video can be pulled
back into inputs with **Use video** (e.g. to extend it).

## Sessions

```
web/sessions/<name>/
  setting.json   task, prompt and all form values — saved as you edit
  inputs/        uploads, extracted frames/audio, takes reused as inputs
  outputs/       rendered .mp4 takes, each with a .json sidecar (params, argv, probe)
  timeline/      combined videos, each with a .json sidecar (source clips, probe)
```

Switch, create, duplicate or delete sessions from the top bar; the last one is
restored on start. `web/sessions/` is git-ignored.

## Adding a task

Edit `static/tasks.js` only: declare the subcommand, which shared blocks it uses
(prompt, canvas, duration, seed, low-RAM, tiling, quantize), its fields, an
availability check, and a `build()` that returns the argument list. The server
appends `--model`, `--gemma`, `--quantize-on-load` and `--output` itself and
only runs whitelisted subcommands, without a shell.

## Requirements and limits

- `ffmpeg`/`ffprobe` on `PATH` for media probing, thumbnails, frame/audio
  extraction and combining.
- Jobs can be stopped, but a stopped job leaves no take.
- `info` on an official-weights directory lists no files: the CLI inspects the
  directory directly rather than the virtual pack.
- Uploads are stored as sent; format support is whatever ffmpeg and the
  pipelines accept.
