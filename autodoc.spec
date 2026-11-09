# PyInstaller build spec: `pyinstaller autodoc.spec` -> dist/AutoDoc/ (onedir).
# config.json, assets/ and models/base.pt are copied beside the exe post-build,
# not via `datas`: PyInstaller puts `datas` inside _internal/, but the app
# resolves those paths relative to the exe.

import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# A missing PySide6 is only a PyInstaller warning; the exe would build and then
# die on launch with ModuleNotFoundError, so fail loudly here.
try:
    import PySide6  # noqa: F401
except ImportError:
    raise SystemExit(
        "[autodoc.spec] *** BUILD ABORTED: PySide6 is not installed in this "
        "Python environment. The frozen app would crash on startup with "
        "ModuleNotFoundError. Activate the build venv and run: "
        "pip install -r requirements.txt ***"
    )

# python-docx's default .docx template is package data; Document() fails without
# it in the frozen build only.
docx_datas = collect_data_files("docx")

# theme/ goes in `datas` (inside _internal/) because theme/loader.py::_theme_dir()
# resolves it relative to sys._MEIPASS; the target names must stay "theme" and
# "theme/icons/<theme>" or the app launches unstyled.
#
# Icons are per-theme: SVG cannot inherit a Qt palette colour, so every theme
# needs its own copy of all three files.
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

# Text check rather than an import, so the spec's own interpreter doesn't need
# PySide6 just to read THEMES.
_tokens_src = Path("theme/tokens.py").read_text(encoding="utf-8")
for _name in _THEME_NAMES:
    if f'"{_name}"' not in _tokens_src:
        raise SystemExit(
            f"[autodoc.spec] *** BUILD ABORTED: theme {_name!r} is bundled here "
            "but not present in theme/tokens.py. Reconcile _THEME_NAMES with "
            "THEMES. ***"
        )

# Whisper resolves mel_filters.npz and *.tiktoken relative to its own __file__,
# so `datas` is the correct mechanism for them.
whisper_datas = collect_data_files("whisper")

# BookStack bot token — resolved sys._MEIPASS-relative by publish/token.py.
# Optional by design: no token means publishing unavailable, not a build error.
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
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtSvg",  # registers Qt's SVG handler for url(*.svg) in app.qss
        # sounddevice internal data
        "sounddevice",
        "_sounddevice_data",
        # Whisper & tokenizer
        "whisper",
        "whisper.normalizers",
        "tiktoken",
        "tiktoken_ext",
        "tiktoken_ext.openai_public",
        # torch (CPU) — many lazy imports
        "torch",
        "torch.cuda",  # torch's C init imports it even on the CPU-only wheel
        "torch.nn",
        "torch.jit",
        "torch.utils",
        "torch.utils.data",
        # numpy
        "numpy",
        # UI Automation click-target capture (comtypes.gen is built at runtime)
        "comtypes",
        "comtypes.client",
        "comtypes.stream",
        # Word export
        "docx",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # torch.cuda is deliberately NOT excluded — see hiddenimports above.
        "torchvision",
        "torchaudio",
        # Other heavy packages not needed at runtime
        "matplotlib",
        "scipy",
        "IPython",
        "test",
        # tkinter — the UI is PySide6; excluded so no transitive import can drag
        # the tcl/tk runtime back in.
        "tkinter",
        "_tkinter",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.ttk",
        # PySide6 modules AutoDoc doesn't use; QtWebEngineCore alone is >100 MB
        # (it embeds Chromium).
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
        # designer/uic/rcc launchers, not runtime. Do NOT add shiboken6: it is
        # the binding layer every PySide6 import goes through.
        "PySide6.scripts",
        # unittest is deliberately NOT excluded: torch/utils/_config_module.py
        # imports it at import time, so excluding it breaks `import torch`.
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# exclude_binaries=True makes this an onedir build: COLLECT below places the
# binaries and datas in dist/AutoDoc/_internal/.
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

# Post-build: copy the exe-adjacent files. DISTPATH comes from PyInstaller's
# spec execution environment.
_dist_dir = Path(DISTPATH) / "AutoDoc"

shutil.copy2("config.json", _dist_dir / "config.json")
print(f"[autodoc.spec] copied config.json -> {_dist_dir / 'config.json'}")

# Report what shipped: the deployed app reads only the config.json beside the
# exe, copied verbatim from the project root at build time.
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
# The .ico is needed here in ADDITION to EXE(icon=): that copy is baked into the
# exe for Explorer, this one is what Qt loads at runtime for the window icon.
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
