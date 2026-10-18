"""回帰テスト一覧。1 項目 = 1 テストで R01 から順に積む。"""
import re
import unittest
from pathlib import Path

BAT = Path(__file__).resolve().parent.parent / "run_watchlist.bat"


def _bat_bytes():
    return BAT.read_bytes()


class RegressionTests(unittest.TestCase):
    def test_R01_bat_has_no_stray_carriage_return(self):
        # 2026-09-20 のコミットで "logs\run_history.log" の "\r" が CR に化け、
        # 実発注の行のログが logs<CR>un_history.log という別名ファイルに出ていた
        stray = [m.start() for m in re.finditer(rb"\r(?!\n)", _bat_bytes())]
        assert stray == [], f"改行以外の CR が残っている（バイト位置 {stray}）"

    def test_R02_bat_has_no_other_control_characters(self):
        # \t \n \r 以外の制御文字（\b \f など、エスケープ化けの別パターン）
        ctrl = re.findall(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", _bat_bytes())
        assert ctrl == []

    def test_R03_bat_lines_end_with_crlf(self):
        # cmd.exe は LF だけの改行だと日本語コメントの行を読み違えることがある
        lines = _bat_bytes().split(b"\n")
        assert lines[-1] == b"", "最終行が改行で終わっていない"
        lf_only = [i + 1 for i, line in enumerate(lines[:-1]) if not line.endswith(b"\r")]
        assert lf_only == [], f"CRLF でない行: {lf_only}"

    def test_R04_bat_every_redirect_goes_to_run_history_log(self):
        text = _bat_bytes().decode("utf-8")
        targets = re.findall(r">>\s*(\S+)", text)
        assert targets, "リダイレクトが 1 つも見つからない"
        wrong = sorted({t for t in targets if t != r"logs\run_history.log"})
        assert wrong == [], f"ログの出力先が違う: {wrong}"
