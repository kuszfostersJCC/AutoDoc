# autodoc.spec — PyInstaller build specification
#
# Build with:
#   pyinstaller autodoc.spec
#
# One-time prerequisites on the build machine (Foster's machine only —
# coworkers never touch any of this):
#   1. pip install torch --index-url https://download.pytorch.org/whl/cpu
#   2. pip install -r requirements.txt
#   3. Check the AI-server block in config.json (local_endpoint,
#      local_model, local_api_key) — it's copied into the build verbatim
#      (see the post-build step at the bottom of this file), whatever is in
#      it when you run `pyinstaller autodoc.spec`. The build prints what it
#      shipped and warns about a missing metering key.
#   4. Make sure models/base.pt exists locally (run the app once via
#      `python main.py` and let it download the Whisper model — after
#      that it's cached at models/base.pt and gets copied into the build
#      automatically too, so coworkers' first run never needs internet
#      access just to get the app running).
#
# Output: dist/AutoDoc/  — a whole FOLDER, not a single exe. config.json,
# models/, sessions/, and output/ all live beside AutoDoc.exe by design
# (see config.py's get_app_dir()), so an onedir build matches how the app
# actually reads/writes its own files.
#
# IMPORTANT — PyInstaller 6+ quirk: onedir builds place everything passed
# via Analysis(datas=...) inside a dist/AutoDoc/_internal/ subfolder, not
# beside AutoDoc.exe. But get_app_dir() (config.py) and get_model_cache_dir()
# (transcription/whisper_runner.py) both resolve paths relative to the real
# exe location — Path(sys.executable).parent — which is dist/AutoDoc/, the
# PARENT of _internal/. So anything bundled via `datas` is invisible to the
# app at runtime: it silently falls back to "not configured" / "not cached"
# and either fails or re-downloads. That's why this spec does NOT put
# config.json, assets/, or models/base.pt in `datas` — instead they're
# copied directly into dist/AutoDoc/ in a post-build step below, which is
# unaffected by wherever PyInstaller decides to put _internal/ in future
# versions.
#
#   dist/AutoDoc/AutoDoc.exe
#   dist/AutoDoc/config.json      ← AI server + metering key, copied automatically
#   dist/AutoDoc/models/base.pt   ← pre-downloaded, copied automatically (if present at build time)
#   dist/AutoDoc/assets/*.png     ← tray icons, copied automatically
#   dist/AutoDoc/_internal/...    ← PyInstaller runtime DLLs/libs, Qt, and
#                                   theme/ (app.qss + SVG icons). Ignore.
#
# UI stack note (2026-07-24): the app is PySide6/Qt Widgets. tkinter and
# pystray are both gone, and tkinter is explicitly EXCLUDED below so the
# tcl/tk runtime can't sneak back into the build. Qt is large, so the
# excludes list strips the modules AutoDoc doesn't use (QtWebEngine is the
# big one — it embeds Chromium). AutoDoc needs QtCore, QtGui, QtWidgets and
# QtSvg, nothing more.
#
# Zip the whole dist/AutoDoc/ folder (including _internal/) to hand to
# coworkers — they unzip anywhere and double-click AutoDoc.exe. No Python,
# no pip, no downloads, no config editing on their end.
#
# Still on you (not something this spec can automate):
#   - Windows SmartScreen will flag the unsigned exe on first launch —
#     be there to walk each coworker through "More info -> Run anyway" once.
#   - Run a Windows Defender scan on dist/AutoDoc/AutoDoc.exe before
#     handing it out (global mouse hooks can trigger false positives).

import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# ------------------------------------------------------------------ #
# Guard: PySide6 must actually be installed in the build environment. #
# A missing module is only a WARNING to PyInstaller — the build still  #
# reports "Build complete!" and produces an exe that dies instantly    #
# with ModuleNotFoundError on launch. That exact trap cost a build      #
# cycle on 2026-07-24 (a UI dependency was added to requirements.txt   #
# but never pip-installed into the venv), so fail loudly instead.      #
# ------------------------------------------------------------------ #
try:
    import PySide6  # noqa: F401
except ImportError:
    raise SystemExit(
        "[autodoc.spec] *** BUILD ABORTED: PySide6 is not installed in this "
        "Python environment. The frozen app would crash on startup with "
        "ModuleNotFoundError. Activate the build venv and run: "
        "pip install -r requirements.txt ***"
    )

# python-docx ships a default .docx template as package data — without it,
# Document() raises at runtime in the frozen build only (same failure class
# as whisper's mel_filters.npz on 2026-07-08).
docx_datas = collect_data_files("docx")

# AutoDoc's own theme assets: theme/app.qss and theme/icons/<theme>/*.svg.
#
# These belong in `datas` (i.e. inside _internal/), NOT in the exe-adjacent
# post-build copy step at the bottom of this file, because
# theme/loader.py::_theme_dir() resolves them relative to sys._MEIPASS when
# frozen — same reasoning as whisper_datas below, and the opposite of
# config.json/models/assets, which our code resolves relative to the exe.
# The target directory names here MUST stay "theme" and "theme/icons/<theme>"
# or _theme_dir() won't find them and the app launches completely unstyled.
#
# They are code-adjacent, not user-editable: QSS is governed by
# pyside6-ui-style-guide.md and there is deliberately no supported way for a
# deployed machine to have a different stylesheet than the build it came from.
#
# ICONS ARE PER-THEME (2026-07-29, dark mode). Every theme in
# theme/tokens.py::THEMES needs its OWN copy of all three SVGs, because SVG has
# no way to inherit a Qt palette colour and the strokes are literal hex. The
# list is generated from THEMES rather than written out so that adding a palette
# can't ship a build whose new theme has no icons — the failure mode there is
# not a crash but silently missing dropdown arrows and checkmarks in one theme
# only, which is precisely the kind of thing a build check should catch.
_THEME_NAMES = ("light", "dark")   # must match theme/tokens.py::THEMES
_THEME_ICONS = ("chevron-down.svg", "chevron-down-disabled.svg", "check.svg")

theme_datas = [("theme/app.qss", "theme")]
for _name in _THEME_NAMES:
    for _icon in _THEME_ICONS:
        theme_datas.append((f"theme/icons/{_name}/{_icon}", f"theme/icons/{_name}"))

for _src, _dst in theme_datas:
    if not Path(_src).exists():
        raise SystemExit(
            f"[autodoc.spec] *** BUILD ABORTED: missing theme asset {_src!r}. "
            "The app would launch unstyled (app.qss) or with invisible "
            "dropdown arrows/checkmarks (icons) in that theme. ***"
        )

# Cheap guard against _THEME_NAMES drifting from the real THEMES tuple. Doing
# this by import would drag PySide6 into the spec's own interpreter; a text
# check is enough to catch a palette added without a matching icon set.
_tokens_src = Path("theme/tokens.py").read_text(encoding="utf-8")
for _name in _THEME_NAMES:
    if f'"{_name}"' not in _tokens_src:
        raise SystemExit(
            f"[autodoc.spec] *** BUILD ABORTED: theme {_name!r} is bundled here "
            "but not present in theme/tokens.py. Reconcile _THEME_NAMES with "
            "THEMES. ***"
        )

# Whisper's non-code package data: assets/mel_filters.npz (mel spectrogram
# filterbank) and assets/*.tiktoken (tokenizer vocabularies). Whisper resolves
# these relative to its own __file__ — i.e. inside _internal/whisper/assets/ —
# so `datas` is the CORRECT mechanism here, unlike config.json/models/ which
# our code resolves relative to the exe. Without this, transcription fails at
# runtime with FileNotFoundError: ..._internal\whisper\assets\mel_filters.npz
# (confirmed on a target PC 2026-07-08 15:28; the .tiktoken files would have
# been the very next failure).
whisper_datas = collect_data_files("whisper")

# BookStack bot token (v1.3). Goes in `datas` — i.e. inside _internal/ — for two
# reasons, and BOTH matter:
#
#   1. publish/token.py resolves it sys._MEIPASS-relative when frozen, same as
#      theme/ above and the opposite of config.json/models/assets.
#   2. _internal/ is exactly what the updater's non-mirroring robocopy
#      overwrites, so a token shipped here reaches every machine via "Check for
#      Updates" — no USB pass to rotate a credential. config.json can NEVER be
#      delivered that way (make_release.py strips it, by design), which is
#      precisely why the token is not a config key.
#
# OPTIONAL, deliberately — this is only a WARNING, never an abort. A dev checkout
# legitimately has no token (it's gitignored; the repo is public), and the app
# treats a missing token as "publishing unavailable" rather than an error. Note
# the non-mirroring copy also means a MANUALLY placed _internal/bookstack.token
# survives updates, so hand-placing one is a valid deployment too.
_token_src = Path("bookstack.token")
token_datas = [("bookstack.token", ".")] if _token_src.exists() else []

_icon_path = Path("assets/autodoc.ico")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[*whisper_datas, *docx_datas, *theme_datas, *token_datas],
    hiddenimports=[
        # pynput Win32 backends (auto-detection fails under PyInstaller)
        "pynput.keyboard._win32",
        "pynput.mouse._win32",
        # UI — PySide6. QtSvg is the one that's easy to lose: theme/loader.py
        # imports it solely to register Qt's SVG image handler, so an
        # "unused import" cleanup that drops it makes every url(*.svg) in
        # app.qss silently resolve to nothing — dropdown arrows and checkbox
        # ticks simply disappear, with no error anywhere.
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtSvg",
        # sounddevice internal data
        "sounddevice",
        "_sounddevice_data",
        # Whisper & tokenizer
        "whisper",
        "whisper.normalizers",
        "tiktoken",
        "tiktoken_ext",
        "tiktoken_ext.openai_public",
        # NOTE: "anthropic", "httpx" and "anyio" lived here until 2026-08-06,
        # when the Claude API backend was deleted. The AI server is spoken to
        # over stdlib urllib, so documentation generation bundles no HTTP stack
        # at all. If a dependency that speaks HTTP is ever re-added, re-add its
        # hiddenimports too — httpx's transports are lazy and PyInstaller does
        # not find them on its own.
        # torch (CPU) — many lazy imports
        "torch",
        "torch.cuda",  # must be importable even though no GPU is used — see excludes note above
        "torch.nn",
        "torch.jit",
        "torch.utils",
        "torch.utils.data",
        # numpy
        "numpy",
        # v1.1 — UI Automation click-target capture (PRD F1). comtypes
        # generates its comtypes.gen module cache at runtime; the frozen
        # build MUST be smoke-tested clicking around a real app.
        "comtypes",
        "comtypes.client",
        "comtypes.stream",
        # v1.1 — Word export (PRD F2)
        "docx",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # NOTE: torch.cuda is intentionally NOT excluded, even though we only
        # ever install/use the CPU-only torch wheel. torch/__init__.py's own
        # C-extension init unconditionally references torch.cuda regardless of
        # whether a GPU is present — excluding it doesn't save meaningful space
        # (the CPU-only wheel has no real CUDA binaries to strip out in the
        # first place) and instead breaks `import torch` entirely in the frozen
        # build. Confirmed via a real crash log (2026-07-08):
        # ModuleNotFoundError: No module named 'torch.cuda', raised from inside
        # torch/__init__.py's _C._initExtension() call — only in the packaged
        # exe, never when running from source, since this excludes list only
        # affects what PyInstaller bundles.
        "torchvision",
        "torchaudio",
        # Other heavy packages not needed at runtime
        "matplotlib",
        "scipy",
        "IPython",
        "test",
        # tkinter — AutoDoc's UI is PySide6 as of 2026-07-24. Excluded rather
        # than merely unused so PyInstaller can't drag the whole tcl/tk
        # runtime in via some transitive import: it's dead weight in a build
        # that no longer has a single tkinter widget. If a
        # ModuleNotFoundError: 'tkinter' ever appears, something reintroduced
        # a tkinter import (check sessions_window/ first) — fix the import,
        # don't delete this exclude.
        "tkinter",
        "_tkinter",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.ttk",
        # PySide6 modules AutoDoc does not use. Qt ships an enormous amount of
        # surface area and PyInstaller's PySide6 hook is greedy by default;
        # these are the big wins (QtWebEngineCore alone is >100 MB — it embeds
        # Chromium). AutoDoc needs exactly QtCore/QtGui/QtWidgets/QtSvg.
        #
        # If a build ever fails with an ImportError naming one of these,
        # something in the UI started using it — remove that specific line
        # rather than the whole block, and note why here.
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebChannel",
        "PySide6.QtWebSockets",
        "PySide6.QtQuick",
        "PySide6.QtQuick3D",
        "PySide6.QtQuickWidgets",
        "PySide6.QtQml",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic",
        "PySide6.Qt3DAnimation",
        "PySide6.Qt3DExtras",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtGraphs",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtBluetooth",
        "PySide6.QtNfc",
        "PySide6.QtPositioning",
        "PySide6.QtLocation",
        "PySide6.QtSerialPort",
        "PySide6.QtSql",
        "PySide6.QtTest",
        "PySide6.QtDesigner",
        "PySide6.QtHelp",
        "PySide6.QtUiTools",
        # Qt's Python-side tooling (designer/uic/rcc launchers, not runtime).
        # NOTE: do NOT add shiboken6 here. It looks like an optional extra but
        # it's the C++/Python binding layer every PySide6 import goes through —
        # excluding it breaks `import PySide6` outright in the frozen build.
        "PySide6.scripts",
        # NOTE: "unittest" is intentionally NOT excluded, same story as
        # torch.cuda above. torch/utils/_config_module.py does
        # `import unittest` at import time (reached via torch.__init__ →
        # torch.utils.data → torch.distributed.config), so excluding it
        # kills `import torch` in the frozen build with
        # ModuleNotFoundError: No module named 'unittest'. Confirmed via a
        # real target-PC log (2026-07-08 15:11). unittest is pure-Python
        # stdlib — excluding it saved nothing meaningful anyway.
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# onedir build: exclude_binaries=True hands binaries/datas off to COLLECT
# below, which places them in dist/AutoDoc/_internal/ (PyInstaller 6+
# default) rather than an ephemeral onefile temp-extraction dir.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoDoc",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,      # No terminal window — tray-only app
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_icon_path) if _icon_path.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AutoDoc",
)

# ------------------------------------------------------------------ #
# Post-build: copy admin-editable / runtime-discovered files directly #
# beside AutoDoc.exe. See the big comment block at the top of this    #
# file for why this can't just be a `datas` entry in Analysis().      #
# DISTPATH is provided by PyInstaller's spec execution environment.   #
# ------------------------------------------------------------------ #
_dist_dir = Path(DISTPATH) / "AutoDoc"

shutil.copy2("config.json", _dist_dir / "config.json")
print(f"[autodoc.spec] copied config.json -> {_dist_dir / 'config.json'}")

# ------------------------------------------------------------------ #
# Guard: print the AI-server settings actually being shipped.         #
# The deployed app reads ONLY the config.json beside the exe, and it  #
# comes verbatim from the project root at build time — editing        #
# config.py defaults or a dist copy that a rebuild overwrites does    #
# nothing (field incident 2026-07-16: app "reverted" to the cloud     #
# backend because the repo config.json still said so at build time).  #
#                                                                     #
# The METERING KEY gets the same treatment as of 2026-08-06, and it   #
# is the one that most needs it: shipping without it is not a build   #
# failure or a runtime failure. Every machine from that build         #
# generates documentation perfectly and files its tokens under        #
# `chat-ui`, and the only way to find out is to notice `doc-generator`#
# flatlining on the Grafana dashboard weeks later. A line at build    #
# time is the cheapest place to catch it.                             #
# ------------------------------------------------------------------ #
import json as _json

with open("config.json", encoding="utf-8") as _f:
    _shipped = _json.load(_f)
_endpoint = (_shipped.get("local_endpoint") or "").strip()
_key = (_shipped.get("local_api_key") or "").strip()
print(
    f"[autodoc.spec] SHIPPING local_endpoint={_endpoint!r} "
    f"local_model={_shipped.get('local_model')!r} "
    f"metering_key={'present' if _key else 'MISSING'}"
)

# BookStack publishing: report both halves, because they fail independently and
# the runtime symptom of either being wrong is identical — publishing just never
# happens, with only a log line to say so.
_bs_on = bool(_shipped.get("bookstack_enabled"))
print(
    f"[autodoc.spec] SHIPPING bookstack_enabled={_bs_on} "
    f"base_url={_shipped.get('bookstack_base_url')!r} "
    f"book={_shipped.get('bookstack_book')!r} "
    f"token_bundled={bool(token_datas)}"
)
if _bs_on and not token_datas:
    print(
        "[autodoc.spec] *** WARNING: bookstack_enabled is true but no "
        "bookstack.token was bundled. Publishing will be silently unavailable "
        "unless the token is placed in _internal/bookstack.token by hand. ***"
    )
if not _endpoint:
    print(
        "[autodoc.spec] *** WARNING: local_endpoint is empty — Generate "
        "Documentation will fail on every machine this build is deployed to. ***"
    )
if not _key:
    print(
        "[autodoc.spec] *** WARNING: local_api_key is empty. This build will "
        "generate documentation fine and report ALL of its token usage as "
        "'chat-ui' instead of 'doc-generator', which silently corrupts the AI "
        "server's per-app accounting. Set local_api_key in the PROJECT-ROOT "
        "config.json and rebuild. ***"
    )
if ":8081" in _endpoint:
    print(
        "[autodoc.spec] *** WARNING: local_endpoint points at port 8081, the "
        "llama-server router. It answers requests but BYPASSES metering "
        "entirely — every token this build spends will be invisible to the "
        "dashboards. Use port 8080 (the metering proxy). ***"
    )

_assets_src = Path("assets")
_assets_dest = _dist_dir / "assets"
_assets_dest.mkdir(exist_ok=True)
# .png = tray state icons; .ico = the window/taskbar icon loaded at runtime by
# main.py::_app_icon(); .svg = optional crisper tray icons (tray_app.py prefers
# them over .png when present). The .ico is needed here in ADDITION to the
# `icon=` argument passed to EXE() above — that one is baked into the exe file
# for Explorer, while this copy is what Qt reads at runtime for the title bar
# and taskbar button. Copying only *.png (the original behaviour) silently left
# the frozen build falling back to the low-res tray PNG for its window icon.
_asset_count = 0
for _pattern in ("*.png", "*.ico", "*.svg"):
    for _asset in _assets_src.glob(_pattern):
        shutil.copy2(_asset, _assets_dest / _asset.name)
        _asset_count += 1
print(f"[autodoc.spec] copied {_asset_count} asset file(s) -> {_assets_dest}")
if not (_assets_dest / "autodoc.ico").exists():
    print(
        "[autodoc.spec] WARNING: assets/autodoc.ico is missing — the app will "
        "fall back to a tray PNG for its window/taskbar icon, which looks soft "
        "at small sizes."
    )

_model_src = Path("models/base.pt")
if _model_src.exists():
    _models_dest = _dist_dir / "models"
    _models_dest.mkdir(exist_ok=True)
    shutil.copy2(_model_src, _models_dest / "base.pt")
    print(f"[autodoc.spec] copied models/base.pt -> {_models_dest / 'base.pt'}")
else:
    print(
        "[autodoc.spec] WARNING: models/base.pt not found locally — skipping. "
        "Coworkers' first transcription will need internet access to download it. "
        "Run `python main.py` once yourself first to cache it, then rebuild."
    )
