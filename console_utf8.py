"""
Windowsのコンソールで日本語が文字化けしないようにする共通処理。

複数のスクリプトから import されても、標準出力の差し替えは
1回だけ行うようにガードしている（二重に差し替えると
「I/O operation on closed file」エラーになるため）。
"""

from __future__ import annotations

import io
import sys


def setup() -> None:
    if sys.platform != "win32":
        return
    if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if not isinstance(sys.stderr, io.TextIOWrapper) or sys.stderr.encoding.lower() != "utf-8":
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
