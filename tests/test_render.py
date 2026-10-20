"""render.py / editor.py のテスト（python -m unittest discover -s tests）"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import editor  # noqa: E402
import render  # noqa: E402

HEAD = "方向,順,地点,種別,車線,接続,出典,確度,メモ\n"


def model_of(body: str, head: str = HEAD) -> render.Model:
    records, issues = render.parse_csv_text(head + body)
    return render.analyze(records, issues)


def messages(model: render.Model, level: str | None = None) -> list[str]:
    return [i.message for i in model.issues if level is None or i.level == level]


class DummyDataTest(unittest.TestCase):
    """依頼書のダミーデータ。"""

    def setUp(self):
        records, issues = render.read_csv(ROOT / "samples" / "dummy-lanes.csv")
        self.model = render.analyze(records, issues)
        self.t = self.model.routes["南行き"].transitions

    def test_row1_new_right_lane(self):
        t = self.t[0]
        self.assertEqual(t.mode, "explicit")
        self.assertEqual([(l.src, l.dst) for l in t.links], [(0, 0), (1, 1), (2, 2)])
        self.assertEqual(t.new_lanes, [3])

    def test_row2_exits(self):
        exits = [(l.src, l.side, l.name) for l in self.t[1].links if l.kind == "exit"]
        self.assertEqual(exits, [(0, "L", "左の通り"), (3, "R", "右の通り")])

    def test_row3_count_change_is_not_guessed(self):
        t = self.t[2]
        self.assertEqual(t.mode, "undetermined")
        self.assertEqual(t.links, [])
        self.assertEqual(t.unknown_origin, [0, 1, 2])
        self.assertEqual(t.new_lanes, [])

    def test_last_row_connection_warned(self):
        self.assertTrue(any("最後の行" in m for m in messages(self.model, "警告")))
        self.assertEqual(self.model.counts()["エラー"], 0)

    def test_html_renders(self):
        html = render.render_html(self.model)
        self.assertIn("<svg", html)
        self.assertIn("左の通り", html)


class AutoRuleTest(unittest.TestCase):
    def test_before_after_same_intersection(self):
        m = model_of("南行き,1,X,手前,L SL S SR R,,,確,\n南行き,2,X,直後,S S S,,,確,\n")
        t = m.routes["南行き"].transitions[0]
        lanes = [(l.src, l.dst) for l in t.links if l.kind == "lane"]
        exits = [(l.src, l.side) for l in t.links if l.kind == "exit"]
        self.assertEqual(lanes, [(1, 0), (2, 1), (3, 2)])
        self.assertEqual(exits, [(0, "L"), (4, "R")])
        self.assertTrue(all(l.auto for l in t.links))

    def test_straight_count_mismatch_warns(self):
        m = model_of("南行き,1,X,手前,S S S,,,確,\n南行き,2,X,直後,S S,,,確,\n")
        t = m.routes["南行き"].transitions[0]
        self.assertEqual(t.unresolved, [0, 1, 2])
        self.assertFalse([l for l in t.links if l.kind == "lane"])
        self.assertTrue(any("数が合わない" in s for s in messages(m, "警告")))

    def test_between_intersections_same_count(self):
        m = model_of("南行き,1,X,直後,S S,,,確,\n南行き,2,Y,手前,S SR,,,確,\n")
        t = m.routes["南行き"].transitions[0]
        self.assertEqual([(l.src, l.dst) for l in t.links], [(0, 0), (1, 1)])

    def test_crossing_without_rows_warns(self):
        m = model_of("南行き,1,X,手前,S S,,,確,\n南行き,2,Y,手前,S S,,,確,\n")
        self.assertTrue(any("直後の行がなく" in s for s in messages(m, "警告")))

    def test_bus_lane_not_auto(self):
        m = model_of("南行き,1,X,手前,B S,,,確,\n南行き,2,X,直後,B S,,,確,\n")
        t = m.routes["南行き"].transitions[0]
        self.assertEqual(t.unresolved, [0])
        self.assertEqual(t.unknown_origin, [0])

    def test_cross_street_used_for_auto_exit(self):
        head = HEAD.strip() + ",交差道路\n"
        m = model_of("南行き,1,七条堀川,手前,L S,,,確,,七条通\n南行き,2,七条堀川,直後,S,,,確,,\n", head)
        ex = [l for l in m.routes["南行き"].transitions[0].links if l.kind == "exit"]
        self.assertEqual(ex[0].name, "七条通")


class LintTest(unittest.TestCase):
    def test_invalid_code(self):
        m = model_of("南行き,1,X,直後,S LS Q,,,確,\n")
        errs = messages(m, "エラー")
        self.assertTrue(any("'LS'" in e and "SL" in e for e in errs))
        self.assertTrue(any("'Q'" in e for e in errs))

    def test_connection_out_of_range(self):
        m = model_of("南行き,1,X,直後,S S,1>1 3>1 2>5,,確,\n南行き,2,Y,手前,S S,,,確,\n")
        errs = messages(m, "エラー")
        self.assertTrue(any("車線3はありません" in e for e in errs))
        self.assertTrue(any("車線5はありません" in e for e in errs))

    def test_missing_destination(self):
        m = model_of("南行き,1,X,直後,S S S,1>1,,確,\n南行き,2,Y,手前,S S,,,確,\n")
        self.assertTrue(any("車線2・3の行き先が指定されていません" in s for s in messages(m, "警告")))

    def test_cut_and_merge(self):
        m = model_of("南行き,1,X,直後,S S S,1>1 2>2 3>2,,確,\n南行き,2,Y,手前,S S,,,確,\n"
                     "南行き,3,Y,直後,S,1>x,,確,\n")
        t = m.routes["南行き"].transitions[0]
        self.assertEqual([(l.src, l.dst) for l in t.links], [(0, 0), (1, 1), (2, 1)])
        self.assertTrue(any("途切れ" in s for s in messages(m, "警告")))

    def test_order_contradiction(self):
        m = model_of("南行き,1,X,直後,S,,,確,\n南行き,2,X,手前,S,,,確,\n")
        self.assertTrue(any("矛盾" in s for s in messages(m, "警告")))

    def test_blank_rows_counted_and_not_filled(self):
        m = model_of("南行き,1,X,直後,,,,,\n南行き,2,Y,手前,,,,,\n# コメント\n北行き,1,Y,直後,S,,,,\n")
        self.assertIn("車線が未入力の行が 2/2 行あります（2-3行目）", messages(m))
        self.assertIsNone(m.routes["南行き"].rows[0].lanes)
        self.assertEqual(m.routes["南行き"].transitions[0].mode, "blank")

    def test_fullwidth_is_read_with_warning(self):
        m = model_of("南行き,1,X,直後,Ｓ　Ｓ,１＞１,,確,\n南行き,2,Y,手前,S,,,確,\n")
        self.assertEqual(m.routes["南行き"].rows[0].lanes, ["S", "S"])
        self.assertTrue(any("全角" in s for s in messages(m, "警告")))

    def test_bom(self):
        records, issues = render.parse_csv_text("﻿" + HEAD + "南行き,1,X,直後,S,,,確,\n")
        self.assertEqual(len(records), 1)
        self.assertFalse(issues)

    def test_explicit_side_prefix(self):
        m = model_of("南行き,1,X,手前,S S,1>@<側道 2>1,,確,\n南行き,2,X,直後,S,,,確,\n")
        ex = [l for l in m.routes["南行き"].transitions[0].links if l.kind == "exit"][0]
        self.assertEqual((ex.side, ex.name), ("L", "側道"))


class EditorTest(unittest.TestCase):
    def test_roundtrip_keeps_comments_and_lines(self):
        text = HEAD + "# メモ行\n南行き,1,X,直後,S S,,,確,\"改行\nあり\"\n南行き,2,Y,手前,S,,,確,\n"
        header, entries = editor.parse_entries(text)
        self.assertIn("交差道路", header)
        out, lines = editor.to_csv_text(header, entries, "\n")
        self.assertIn("# メモ行\n", out)
        records, _ = render.parse_csv_text(out)
        self.assertEqual([l for l, _ in records], sorted(lines.values()))
        self.assertEqual(records[0][1]["メモ"], "改行\nあり")

    def test_diff(self):
        header, entries = editor.parse_entries(HEAD + "南行き,1,X,直後,,,,,\n")
        old = {e["_id"]: dict(e) for e in entries if editor.is_row(e)}
        row = next(e for e in entries if editor.is_row(e))
        row["車線"] = "S S"
        changes = editor.diff_entries(header, old, entries)
        self.assertEqual([(c[0], c[2], c[3], c[4]) for c in changes], [("変更", "車線", "", "S S")])


if __name__ == "__main__":
    unittest.main()
