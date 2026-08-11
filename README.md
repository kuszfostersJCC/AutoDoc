# AutoDoc

A Windows tray application that turns a recorded screen-and-voice walkthrough
into a written SOP. You click through a process while narrating it; AutoDoc
captures a screenshot at every click, transcribes your narration locally with
Whisper, sends the transcript and screenshots to an on-premises vision model,
and writes the result as Word, HTML and Markdown — optionally publishing it
straight to BookStack.

Built for Jackson College IT to solve a specific problem: documentation that
nobody has time to write, for processes that touch FERPA-protected student
records and therefore cannot be sent to a cloud API.

**Status:** internal tool in active use by a handful of staff. Not packaged for
general consumption — see [Is this useful outside JCC?](#is-this-useful-outside-jcc)

---

## Where to start

This README is the **map and the setup guide**. It tells you what exists, how to
get it running, and which file answers which question. It deliberately contains
almost no *reasoning* — no "why Qt and not tkinter", no "why screenshots are
stored raw".

All of that lives in **[`HANDOFF.md`](HANDOFF.md)**, which is the real handoff
document and is written for whoever picks this up next (human or AI coding
session). It is long because the reasoning is the valuable part: most of it is
"here is the landmine, here is why it's shaped this way, here is what happens if
you 'fix' it."

| If you are… | Read, in this order |
|---|---|
| Setting it up to run | [Setup](#setup-from-a-clean-checkout) below, then stop. |
| About to change code | [Before you change anything](#before-you-change-anything) below → `HANDOFF.md` "Critical invariants" → the section of `HANDOFF.md` covering your area. |
| Doing UI work | `pyside6-ui-style-guide.md` **first**. It is a binding contract, not advice; its §11 checklist defines "done". |
| Building a release | The comment block at the top of `autodoc.spec`, then `HANDOFF.md` "Build process" and "Remote updates". |
| Trying to understand a decision | `HANDOFF.md`. If it isn't there, it wasn't a decision — it was an accident. |

---

## What it does

- **Records** mouse clicks with a screenshot at each one, plus continuous voice
  narration. Global hotkeys for pause/resume, stop, and a click-less manual
  screenshot.
- **Captures the click target's accessibility name** via UI Automation, so the
  model knows you clicked "Save" rather than just where on screen you clicked.
- **Transcribes locally** with OpenAI Whisper. Audio never leaves the machine
  under any configuration.
- **Generates** an SOP from the transcript + screenshots using a vision model on
  the district's AI server, and writes `.docx`, `.html` and `.md`.
- **Edits after the fact** — a session editor lets you fix the transcript, drop
  screenshots, draw redaction boxes, import external photos, switch the
  screenshot privacy mode, and re-run generation without re-recording.
- **Publishes** to BookStack as a draft page with inline screenshots, if enabled.
- **Updates itself** from GitHub Releases via Settings → Check for updates.

Three screenshot render modes — raw, privacy-blurred outside a circle at the
click, or a red ring at the click — are applied at *render* time, so they can be
changed per session after recording.

---

## Requirements

- **Windows.** Uses UI Automation via `comtypes` and expects a Windows tray. Not
  portable as written.
- **Python 3.10+** to run from source. The current venv is 3.14.6.
- **Network access to the district AI server** to generate documentation.
  Recording, transcription, and editing all work fully offline; only the
  "Generate Documentation" step needs the server.
- **~150 MB** for the Whisper `base` model, downloaded on first run and cached.

---

## Setup from a clean checkout

```bash
git clone https://github.com/kuszfostersJCC/AutoDoc.git
cd AutoDoc
python -m venv venv
venv\Scripts\activate

# Install torch FIRST, CPU-only. The default wheel pulls ~2 GB of CUDA
# libraries that this app never uses.
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

# Create your config from the documented example.
copy config.example.json config.json

python main.py
```

That's it — AutoDoc appears in the system tray. `config.example.json` is
annotated key by key; read it rather than guessing, particularly the AI-server
block.

Two things happen on first run: Whisper downloads `base.pt` into `models/`
(~150 MB, once), and if `config.json` is missing it is written from the defaults
in `config.py`. Those defaults are the real JCC deployment values, not generic
placeholders — so a fresh checkout inside the district is already pointed at a
working server.

### Optional: BookStack publishing

Off by default. Set `bookstack_enabled: true` in `config.json` and place a
`bookstack.token` file in the project root. The token is **not** a config key —
`HANDOFF.md` "BookStack publishing" explains where it has to live and why.

---

## What is not in this repo

The repo is public, so several things are excluded on purpose. If a path below
is missing from your checkout, that is correct.

| Path | What it is | How to get it |
|---|---|---|
| `config.json` | Live config, including the AI server's API key | `copy config.example.json config.json` |
| `bookstack.token` | BookStack bot credential | From the maintainer; only needed for publishing |
| `models/base.pt` | Whisper weights, ~150 MB | Downloaded automatically on first run |
| `sessions/`, `output/`, `exports/` | Recorded sessions and generated documents | Created at runtime. May contain student data — never commit. |
| `dist/`, `build/`, `releases/` | Build output and packaged zips | Produced by `pyinstaller autodoc.spec` and `scripts/make_release.py` |
| `venv/` | Virtual environment | Created during setup |

---

## Runtime shape

```
  tray icon (tray/tray_app.py)
        |
        |  start recording
        v
  recorder/          clicks -> screenshot + UI Automation click target
                     microphone -> audio buffer
        |
        v
  transcription/     Whisper, entirely local. Audio never leaves the machine.
        |
        v
  sessions/<id>/     session.json + screenshots/  <- the durable record
        |
        |  "Generate Documentation"
        v
  generation/run.py  the ONE generate-then-write pipeline
        |
        +--> generation/content.py     builds the prompt + image blocks
        +--> generation/backends/      -> AI server, http://172.20.28.203:8080
        +--> generation/renderers.py   normalizes, then writes md/html/docx
        +--> publish/                  optional BookStack draft page
        |
        v
  output/<session>/  the finished documents
```

Everything visual runs on the Qt main thread. Recording, transcription,
generation and the hotkey listener all run on worker threads and reach the UI
**only** through `ui_service.call_soon()`. This is invariant #1 and the most
expensive one to get wrong.

---

## Repo map

| Path | What lives there |
|---|---|
| `main.py` | Entry point: DPI awareness, QApplication, theme, config, hand off to the tray |
| `config.py` | `AutoDocConfig` — the single source of truth for every setting |
| `ui_service.py` | The worker-thread → main-thread seam. Read its docstring before using it. |
| `recorder/` | Click capture, screenshots, audio, hotkeys, UI Automation |
| `transcription/` | Whisper runner and model cache |
| `generation/` | Prompt building, the AI-server backend, the render/write pipeline |
| `publish/` | BookStack client, publisher, tag vocabulary, token loading |
| `sessions_window/` | Sessions list, session editor, Settings tab, redaction dialog |
| `tray/` | Tray icon and menu |
| `theme/` | Palettes, `app.qss`, icons. All styling goes through here. |
| `scripts/make_release.py` | Packages `dist/AutoDoc/` into a release zip with sensitive paths stripped |
| `autodoc.spec` | PyInstaller build spec. Its header comment is required reading before building. |
| `updater.py` | GitHub Releases check, download, and the file-swap dance |

Design documents: `AutoDoc-Design-PRD.md` (original scope),
`PRD-v1.1-features.md`, `PRD-v1.3-bookstack.md`.

---

## Before you change anything

Four things will cost you real time if you skip them.

1. **Read `HANDOFF.md`'s "Critical invariants" section.** Ten numbered rules,
   each one derived from something that actually broke. Several are the kind of
   thing that looks like a cleanup opportunity and is not.

2. **UI work is governed by `pyside6-ui-style-guide.md`.** No `setStyleSheet`,
   no hardcoded colors or fonts in widget files. Both light and dark themes need
   a visual pass — the static checks cannot see the defects that actually reach
   users.

3. **Adding a config key is a two-place edit:** the `AutoDocConfig` dataclass in
   `config.py`, and `config.example.json` (documentation only, read by no code).
   Everything else derives from the dataclass. Do not reintroduce a
   hand-maintained defaults dict — there used to be three, and they drifted.

4. **There is no automated test suite.** Verification so far has been throwaway
   scripts against fake servers plus manual runs. `generation/` and `publish/`
   are both testable and worth testing if you touch them; the Qt and PyInstaller
   layers are not testable that way. `python main.py` from source is the fastest
   real check that nothing is broken.

A note on the AI server specifically: **AutoDoc can be misconfigured in ways
that produce perfect documentation and wrong usage metrics.** There is no error
message for it. If you touch `generation/backends/`, read `HANDOFF.md` "The AI
server" first.

---

## Building and releasing

```bash
pyinstaller autodoc.spec        # -> dist/AutoDoc/  (onedir, not onefile)
python scripts/make_release.py  # -> releases/AutoDoc-v<version>.zip
```

The build prints what it is shipping and warns about the known foot-guns —
**read that output before handing out a build.** Bump `version.py` and tag the
matching commit together; `updater.py` compares the two and will silently
disagree with reality if they drift. Full process, including the file-lock
problem that makes self-updating non-trivial, is in `HANDOFF.md`.

`version.py` is the only place the version lives. Neither this file nor
`HANDOFF.md` restates it, on purpose — a hardcoded version in HANDOFF went two
releases stale before anyone noticed.

The exe is unsigned, so Windows SmartScreen flags it on first launch per
machine. Be present for "More info → Run anyway" the first time.

---

## Privacy

The constraint this tool is built around: **student records must not leave the
institution's network.**

- Audio is transcribed locally and never transmitted.
- Transcripts and screenshots go only to the on-premises AI server, which is
  built so that no component can persist a request or response body.
- Screenshots are stored raw on disk and can contain whatever was on screen.
  That is a deliberate, accepted trade — it is what makes privacy modes
  retroactively editable. Recorded sessions live in `sessions/` and are
  gitignored; treat that folder as sensitive.

`HANDOFF.md` and the AI server's own README carry the details.

---

## Is this useful outside JCC?

Partly. The recording, transcription, editing and rendering layers are
general-purpose. But the configuration defaults, the AI server address, the
BookStack instance and the FERPA framing are all specific to one institution,
and there is no installer or onboarding for anyone else. Treat it as a working
reference implementation rather than a product.

Public visibility was a deliberate choice so that release zips could be
distributed over plain HTTPS without GitHub accounts. Nothing FERPA-sensitive
lives in the repo.
