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

There is **no authentication** — keep it bound to `127.0.0.1` (see [Security](#security)).

| Flag | Default | Description |
| --- | --- | --- |
| `--model` | `$LTX_MODEL` | Model directory, official LTX-2.5 files or Hugging Face repo id. |
| `--gemma` | `$LTX_GEMMA` | Gemma 3 repo for LTX-2.3 packs and prompt enhancement. |
| `--host` | `127.0.0.1` | Bind address. A warning is printed for anything but loopback. |
| `--port` | `8720` | Bind port. |
| `--allow-host` | *(none)* | Extra `Host` names to accept, comma-separated. IP addresses and `localhost` are always accepted. |

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
3. Fill in the prompt, canvas (see [Canvas size](#canvas-size)), duration on
   the 8k+1 frame grid (or LTX-2.5 auto duration), seed, and task options. **Advanced** holds
   sampler knobs, LoRAs, quantize-on-load, low-RAM streaming, tiling and extra
   raw arguments. **Command** shows the exact `ltx-2-mlx` invocation.
   **History ▾** under the prompt brings back any prompt this session's takes used.
4. **Render** (or ⌘/Ctrl+Enter) queues the job; **Queue 3 seeds** (⇧⌘/Ctrl+Enter)
   queues three random seeds and opens them side by side when they finish (see
   [Comparing takes](#comparing-takes)). Both sit in the render bar pinned to the
   bottom of the left pane, next to an estimate taken from this session's finished
   takes (`≈` when earlier takes had the same settings, `~` when scaled from takes
   of the same task at another size or length).
   One job runs at a time. The progress card shows an Encode → Load → Denoise →
   Decode → Save stepper, denoising step progress, elapsed time, the time left in
   the current denoising stage (from the pipeline's own `[estimate]` lines) and a
   **Stop** button; the tab title shows overall progress, and 🔔 in the top bar
   turns on a browser notification when a render finishes while the tab is in the
   background. A failed render pops up its error and, for known problems (out of
   memory, the macOS GPU watchdog, a missing DurationHead or dev transformer,
   Hugging Face access, ffmpeg), a hint about what to try.
5. Optionally tick **Live preview** before rendering to watch the video take
   shape — see [Live preview](#live-preview).
6. Every take appears under **Takes** on the right with its settings. From a take
   you can **Chain →** (last frame becomes the start image of Image → Video),
   **Reuse** its settings, pull the video itself (**Use video**, for retake, extend
   or control) or its **Last frame** into inputs. The **⋮** menu adds the first
   frame, the audio track (for audio → video), **Previews (N)**, **Add to compare**,
   **Download** and **Delete…**. ☆ stars a take and **★ starred only** filters the
   list. The **Timeline** tab lists combined videos.

The terminal is saved per session in `terminal.log` and restored when the session
opens; drag its top edge to resize it. With more than eight inputs of mixed kinds,
chips above the library filter it by kind.

## Comparing takes

- **Seed grid** — after **Queue 3 seeds** finishes, the viewer shows the takes
  muted, looped and playing in sync, each with **☆ Keep** (stars the take) and
  **Open**. Any 2–4 takes picked with **Add to compare** open the same grid from
  **Grid** in the compare bar.
- **A/B wipe** — with exactly two takes picked, **A/B wipe** overlays them: drag
  across the video or use the slider to move the split.

Esc or **Close** returns to the selected take.

## Keyboard

| Key | Action |
| --- | --- |
| ⌘/Ctrl+Enter | Render |
| ⇧⌘/Ctrl+Enter | Queue 3 seeds |
| Space | Play / pause the viewer |
| ← / → | Step one frame back / forward |
| J / K | Next / previous take |
| Esc | Close dialogs, menus and the compare view |

Space, arrows and J/K are ignored while typing in a field.

## Canvas size

Pick an **aspect ratio** and drag **Megapixels** (0.1–2.1 MP); the studio
solves the closest legal width × height. LTX needs sizes on a pixel grid:
two-stage pipelines (distilled, two-stage, HQ, audio → video, keyframe,
IC-LoRA, lip dub) render stage 1 at half size, so their sizes step in
multiples of **64**; `generate` with the one-stage pipeline renders at full
size and steps in multiples of **32**. Switching pipeline re-fits the size to
the new grid.

- **Aspect ratio** — 16:9, 9:16, 1:1, 4:3, 3:4, 3:2, 2:3, 21:9; **Match
  input** uses the task's selected image or video; **Custom** keeps the current
  size and locks its ratio for the slider.
- **Readout** — resolved size, actual megapixels and ratio (the grid can shift
  the ratio a few percent), latent size (width/32 × height/32) and the grid.
- **Presets and Width/Height** — preset chips and typed sizes still work;
  typed values snap to the grid when you leave the field, and the aspect and
  megapixel controls follow.

Above 720p (0.9 MP) memory grows quickly with duration; beyond 1080p consider
tiling in **Advanced**.

## Live preview

Off by default. Tick **Live preview** (above the Render button) to have the
pipeline decode a short animated WebP of the current latent while it denoises;
each one appears in the viewer as soon as it is written, with a badge showing
its step and stage (e.g. `Preview step 3/8 · stage 1`). Available for
generate, audio → video, retake, extend, keyframe, IC-LoRA, HDR and lip dub;
the option hides for other tasks.

| Setting | CLI flag | Effect |
| --- | --- | --- |
| **Every N steps** | `--stepwise-interval` | Preview every N denoising steps (1–100). The final step of each stage is always previewed. |
| **Clip length** | `--stepwise-frames` | Latent frames to decode: **Still frame** (1), **Short** (3 → 17 frames), **Default** (8 → 57 frames), **Long** (16 → 121 frames). Longer clips show more motion but decode slower. |
| **Position** | `--stepwise-frame` | Which part of the video the clip is centred on: **Middle**, **Start**, **End**, or **Custom** latent frame index (negative counts from the end). |

The hint under the settings shows the resulting cost. Each preview runs a VAE
decode, and the decoder stays loaded for the whole render, so expect a slower
render and higher peak memory. With `--low-ram` most of the memory saving is
lost. Image quality is fixed by the pipeline (WebP quality 90) and is not
adjustable.

While a job runs, clicking another take stops the viewer following the render;
**Show live preview** in the progress panel switches back. When the take is
done the viewer switches to the finished video, and the take's ⋮ menu gains
**Previews (N)**: a slider through every preview in order, with
**Back to video** to return. Previews live in `previews/<job>/`. They are
deleted with their take, and also when the job fails, is stopped, or produced
none.

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
  result plays here and appears in the **Timeline** tab on the right.

Source clips are only read, never modified. A combined video can be pulled
back into inputs with **Use video** (e.g. to extend it).

## Sessions

```
web/sessions/<name>/
  setting.json   task, prompt and all form values — saved as you edit
  terminal.log   the session's terminal output (rotated at ~2 MB)
  inputs/        uploads, extracted frames/audio, takes reused as inputs
  outputs/       rendered .mp4 takes, each with a .json sidecar (params, argv, probe, previews, starred)
  previews/      live-preview WebPs, one folder per render
  timeline/      combined videos, each with a .json sidecar (source clips, probe)
```

Switch sessions from the top bar; new, duplicate and delete live in the **⋯**
menu next to it. The last session is restored on start. `web/sessions/` is
git-ignored.

## Model and setup

The **Model** button shows a status dot: green when everything checks out, amber
for a Hugging Face repo id (not inspected until it downloads) or a missing
optional file, red when no model is set, the directory is missing, or
`ffmpeg`/`ffprobe` aren't on `PATH`. The dialog lists each check (transformer
variants, VAE decoder, spatial upscaler, ffmpeg, ffprobe) and what the model can
run; **Recheck** runs the checks again after you fix something.

## Security

ltx studio has no authentication, so it defends against the one thing a local
tool must: other websites and other machines driving it.

- It binds to `127.0.0.1` by default and warns for any other address. Anyone
  who can reach the port can run jobs.
- Requests whose `Host` header isn't an IP address, `localhost`, the `--host`
  value or an `--allow-host` name are refused, which blocks DNS rebinding.
- State-changing requests must come from the studio's own origin and carry a
  JSON content type (uploads: an `X-Filename` header), so a page you visit can't
  forge them.
- Only whitelisted `ltx-2-mlx` subcommands run, without a shell, and task inputs
  must be files inside the session's `inputs/`.

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
