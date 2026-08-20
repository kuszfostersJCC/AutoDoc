# AutoDoc

A Windows tray app that turns a recorded screen-and-voice walkthrough into a
written SOP. You click through a process while narrating it; AutoDoc screenshots
every click, transcribes the narration locally with Whisper, sends transcript and
screenshots to an on-premises vision model, and writes Word, HTML and Markdown —
optionally publishing to BookStack.

Built for Jackson College IT: the processes worth documenting touch
FERPA-protected records, so nothing can go to a cloud API. Internal tool, in use
by a handful of staff, not packaged for anyone else.

This file is the map and the setup guide. `HANDOFF.md` is invariants and
gotchas — read it before changing code. `pyside6-ui-style-guide.md` is a binding
contract for UI work; its §11 checklist defines "done".

## What it does

- Screenshots every mouse click, with the click target's accessibility name from
  UI Automation, so the model knows you clicked "Save" and not just a position.
- Records continuous narration; hotkeys for pause/resume, stop, and a click-less
  manual screenshot.
- Transcribes locally with Whisper. Audio never leaves the machine.
- Generates an SOP from transcript + screenshots and writes `.docx`, `.html`,
  `.md`.
- Session editor: fix the transcript, delete or crop screenshots, draw redaction
  boxes, note a screenshot for the model, import external photos, switch privacy
  mode, re-generate — all without re-recording.
- Publishes to BookStack as a draft page, if enabled.
- Self-updates from GitHub Releases via Settings → Check for updates.

Screenshot modes (raw / blurred outside the click / red ring at the click) apply
at render time, so they can be changed per session after recording.

## Requirements

- Windows. UI Automation via `comtypes`, and a Windows tray. Not portable.
- Python 3.10+ from source (current venv is 3.14.6).
- Network access to the district AI server for the generate step only; record,
  transcribe and edit work offline.
- ~150 MB for the Whisper `base` model, downloaded on first run.

## Setup from a clean checkout

```bash
git clone https://github.com/kuszfostersJCC/AutoDoc.git
cd AutoDoc
python -m venv venv
venv\Scripts\activate

# torch FIRST, CPU-only: the default wheel pulls ~2 GB of unused CUDA libraries.
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

copy config.example.json config.json
python main.py
```

AutoDoc appears in the tray. On first run Whisper downloads `base.pt` into
`models/`, and a missing `config.json` is written from `config.py`'s defaults —
which are the real JCC deployment values, so a fresh checkout inside the district
already points at a working server.

BookStack publishing is off by default: set `bookstack_enabled: true` and put a
`bookstack.token` file in the project root (it is deliberately not a config key —
see `HANDOFF.md`).

## What is not in this repo

The repo is public. If one of these is missing from your checkout, that's correct.

| Path | What it is | How to get it |
|---|---|---|
| `config.json` | Live config | `copy config.example.json config.json` |
| `bookstack.token` | BookStack bot credential | From the maintainer; publishing only |
| `models/base.pt` | Whisper weights, ~150 MB | Downloaded on first run |
| `sessions/`, `output/` | Recordings and generated docs | Created at runtime. May contain student data — never commit. |
| `dist/`, `build/`, `releases/` | Build output | `pyinstaller autodoc.spec`, then `scripts/make_release.py` |
| `venv/` | Virtual environment | Created during setup |

## Runtime shape

```
  tray icon (tray/tray_app.py)
        |  start recording
        v
  recorder/          clicks -> screenshot + UI Automation click target
                     microphone -> audio buffer
        v
  transcription/     Whisper, entirely local
        v
  sessions/<id>/     session.json + screenshots/   <- the durable record
        |  "Generate Documentation"
        v
  generation/run.py  the ONE generate-then-write pipeline
        +--> content.py    prompt + image blocks
        +--> backends/     -> AI server, http://172.20.28.203:8080
        +--> renderers.py  normalize, then write md/html/docx
        +--> publish/      optional BookStack draft page
        v
  output/<session>/  the finished documents
```

Everything visual runs on the Qt main thread; recording, transcription,
generation and the hotkey listener run on workers and reach the UI only through
`ui_service.call_soon()`. That is invariant #1 and the most expensive one to get
wrong.

## Repo map

| Path | What lives there |
|---|---|
| `main.py` | Entry: DPI awareness, QApplication, theme, config, hand off to tray |
| `config.py` | `AutoDocConfig` — single source of truth for every setting |
| `ui_service.py` | Worker-thread → main-thread seam |
| `recorder/` | Click capture, screenshots, audio, hotkeys, UI Automation |
| `transcription/` | Whisper runner and model cache |
| `generation/` | Prompt building, AI-server backend, render/write pipeline |
| `publish/` | BookStack client, publisher, tag vocabulary, token loading |
| `sessions_window/` | Sessions list, session editor, Settings tab, redaction dialog |
| `tray/` | Tray icon and menu |
| `theme/` | Palettes, `app.qss`, icons. All styling goes through here. |
| `scripts/make_release.py` | Packages `dist/AutoDoc/` into a release zip |
| `autodoc.spec` | PyInstaller spec; its comments say why each exclude exists |
| `updater.py` | Releases check, download, file-swap |

Original scope: `AutoDoc-Design-PRD.md`, `PRD-v1.1-features.md`,
`PRD-v1.3-bookstack.md`.

## Before you change anything

- **Read `HANDOFF.md`'s invariants.** Ten rules, each from something that broke.
  Several look like cleanup opportunities and are not.
- **UI work follows `pyside6-ui-style-guide.md`.** No `setStyleSheet`, no colors
  or fonts in widget files. Both themes need a visual pass; static checks cannot
  see the defects that reach users.
- **Adding a config key is a two-place edit:** the `AutoDocConfig` dataclass and
  `config.example.json` (documentation only, read by no code).
- **No automated test suite.** `generation/` and `publish/` are testable and
  worth testing; the Qt and PyInstaller layers aren't. `python main.py` from
  source is the fastest real check.
- **AutoDoc can be misconfigured in ways that produce perfect documentation and
  wrong usage metrics,** with no error message. Read `HANDOFF.md` on the AI
  server before touching `generation/backends/`.

### Comments

Comments are 1–3 sentences that clarify genuinely unclear design. Not essays.

- No duplicating the code, no dated changelog entries, no
  alternatives-considered narrative, no arguing with a future maintainer.
- Module docstring: one line. Function docstring: only where the signature
  doesn't already say it.
- Inline comments earn their place by explaining unidiomatic code, marking a bug
  fix that looks wrong without them, or flagging incomplete work.
- If a comment needs to be long, the code is probably unclear — fix the code.
- The same applies to UI helptext: prefer a self-explanatory label over a caption
  explaining it.

## Building and releasing

```bash
git status                      # must be clean — see below
pyinstaller autodoc.spec        # -> dist/AutoDoc/  (onedir)
python scripts/make_release.py  # -> releases/AutoDoc-v<version>.zip
```

`make_release.py` prints the remaining steps (merge, tag, publish) — follow that
output rather than memory. The build also prints what it is shipping and warns
about the known foot-guns; read it before handing out a build.

**The zip is built from your working tree, not from git.** Nothing consults a
commit, so a release can contain uncommitted code that exists nowhere in history.
`git status` first is the only safeguard. The flip side: a tag mistake can't ship
broken code, because the bytes come from the local build.

Bump `version.py` and tag the matching commit together — `updater.py` compares
the two. `version.py` is the only place the version lives.

The exe is unsigned, so SmartScreen flags it on first launch per machine.

## Privacy

Student records must not leave the institution's network.

- Audio is transcribed locally and never transmitted.
- Transcripts and screenshots go only to the on-premises AI server, which cannot
  persist a request or response body.
- Screenshots are stored raw on disk and can contain whatever was on screen — an
  accepted trade, and what makes privacy modes retroactively editable. Treat
  `sessions/` as sensitive; it is gitignored.

## Is this useful outside JCC?

The recording, transcription, editing and rendering layers are general-purpose,
but the config defaults, server address, BookStack instance and FERPA framing are
specific to one institution and there is no installer for anyone else. Treat it
as a reference implementation. The repo is public so release zips can be
distributed over plain HTTPS; nothing FERPA-sensitive is in it.
