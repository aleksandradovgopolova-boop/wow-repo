#!/usr/bin/env python3
"""_bootstrap.py — shared sys.path setup for tools/ and validation/ modules."""
from __future__ import annotations
import sys
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
# корень тоже в путях: из него импортируется пакетная поверхность ai_ops_kit
# R-39: в `.ai/managed` дочки байткода быть не должно (слой checksummed — свой же `.pyc` кит
# принимал за правку владельца). Свой файл записан ДО исполнения тела, поэтому убираем его явно.
if PKG.name == "managed" and PKG.parent.name == ".ai":
    sys.dont_write_bytecode = True
    _c = globals().get("__cached__")
    if _c:
        try:
            Path(_c).unlink(missing_ok=True)
            Path(_c).parent.rmdir()
        except OSError:
            pass

for _p in (str(PKG / "tools"), str(PKG / "validation"), str(PKG)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
