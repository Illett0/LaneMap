#!/usr/bin/env python3
"""車線接続図ジェネレーター（LaneMap）

CSV に書いた車線構成と接続から、車線のつながり（合流・側道への離脱・
新しく現れる車線）を描いた自己完結の HTML を作る。標準ライブラリのみ。

    python render.py horikawa-lanes.csv -o lanes.html

読み込み・検証（parse_csv_text / analyze）と描画（render_svg / render_html）は
関数で分けてあり、editor.py も同じ関数を使う。
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 仕様の定数

COLUMNS = ["方向", "順", "地点", "種別", "車線", "接続", "出典", "確度", "メモ"]
OPTIONAL_COLUMNS = ["交差道路"]
DIRECTIONS = ["南行き", "北行き"]
KINDS = ["手前", "直後", "区間中"]
LANE_CODES = ["L", "S", "R", "SL", "SR", "LSR", "B", "?"]
CONF_RANK = {"確": 3, "資": 2, "推": 1, "": 0}
LEVELS = {"エラー": 0, "警告": 1, "情報": 2}


# ---------------------------------------------------------------- データ構造

@dataclass
class Row:
    line: int                 # CSV の行番号（見出し行が 1）
    direction: str
    order: int
    point: str
    kind: str
    lanes: list[str] | None   # None は未入力
    conn: str
    source: str
    conf: str
    memo: str
    cross: str
    invalid: set[int] = field(default_factory=set)   # 不正なコードだった車線（"?" として扱う）

    @property
    def where(self) -> str:
        return f"{self.direction} 順{self.order} {self.point or '（地点なし）'} {self.kind}"


@dataclass
class Issue:
    level: str                # エラー / 警告 / 情報
    line: int | None
    message: str
    where: str = ""
    direction: str = ""

    def format(self) -> str:
        loc = f"{self.line}行目" if self.line else "全体"
        where = f" [{self.where}]" if self.where else ""
        return f"[{self.level}] {loc}{where}: {self.message}"

    def to_dict(self) -> dict:
        return {"level": self.level, "line": self.line, "message": self.message,
                "where": self.where, "direction": self.direction}


@dataclass
class Link:
    src: int                  # この行の車線（0 始まり）
    kind: str                 # lane（次の行の車線へ）/ exit（側道へ離脱）/ cut（途切れ）
    dst: int | None = None    # 次の行の車線（0 始まり）
    name: str = ""            # 離脱先の通り名（分からなければ空）
    side: str = ""            # 離脱の向き L / R（進行方向に向かって）
    auto: bool = False        # 自動補完で決めたか


@dataclass
class Transition:
    """ある行から次の行へのつながり。"""
    cur: Row
    nxt: Row | None
    mode: str = "blank"       # explicit / auto / undetermined / blank / end
    links: list[Link] = field(default_factory=list)
    unresolved: list[int] = field(default_factory=list)      # 行き先が決まらない車線
    new_lanes: list[int] = field(default_factory=list)       # 次の行で新しく現れる車線
    unknown_origin: list[int] = field(default_factory=list)  # 次の行の、どこから来たか決まらない車線


@dataclass
class Route:
    direction: str
    rows: list[Row]
    transitions: list[Transition]    # rows と同じ長さ。最後は次の行なし
    crosses: dict[str, str]          # 地点 → 交差道路


@dataclass
class Model:
    routes: dict[str, Route]
    issues: list[Issue]

    def counts(self) -> Counter:
        return Counter(i.level for i in self.issues)


# ---------------------------------------------------------------- 読み込み

def read_csv(path: str | Path) -> tuple[list[tuple[int, dict]], list[Issue]]:
    """CSV ファイルを読む。BOM があっても読めるように utf-8-sig で開く。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        return parse_csv_text(f.read())


def parse_csv_text(text: str) -> tuple[list[tuple[int, dict]], list[Issue]]:
    """CSV の文字列を (行番号, {列名: 値}) の並びにする。# で始まる行と空行は飛ばす。"""
    if text.startswith("﻿"):
        text = text[1:]
    issues: list[Issue] = []
    records: list[tuple[int, dict]] = []
    header: list[str] | None = None
    reader = csv.reader(io.StringIO(text, newline=""))
    prev = 0
    for fields in reader:
        start, prev = prev + 1, reader.line_num
        if not fields or all(not f.strip() for f in fields):
            continue
        if fields[0].lstrip().startswith("#"):
            continue
        if header is None:
            header = [f.strip() for f in fields]
            missing = [c for c in COLUMNS if c not in header]
            if missing:
                issues.append(Issue("エラー", start, f"見出しに列 {'、'.join(missing)} がありません"))
                return [], issues
            continue
        if len(fields) > len(header) and any(f.strip() for f in fields[len(header):]):
            issues.append(Issue("警告", start, "列の数が見出しより多いので、余った列は無視します"))
        records.append((start, {h: (fields[i] if i < len(fields) else "") for i, h in enumerate(header)}))
    if header is None:
        issues.append(Issue("エラー", None, "見出し行がありません"))
    return records, issues


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def _suggest(tok: str) -> str:
    up = tok.upper()
    if up in LANE_CODES:
        return f"（大文字の {up} のことですか）"
    for code in LANE_CODES:
        if sorted(up) == sorted(code):
            return f"（{code} のことですか。L→S→R の順に書きます）"
    return "（使えるのは " + " ".join(LANE_CODES) + "）"


def cross_sides(text: str) -> dict[str, str] | None:
    """交差道路を {"東": 名, "西": 名} にする。

    「七条通」のように東西の指定がなければ両側とも同じ名前。「東:二条通」のように
    片側だけなら、もう片側には道路がない（T字路）。空欄は {}（不明）。書き方が不正なら None。
    """
    t = _nfkc(text).strip()
    if not t:
        return {}
    if ":" not in t:
        return {"東": t, "西": t}
    out = {}
    for tok in t.split():
        key, _, val = tok.partition(":")
        if key not in ("東", "西") or not val or key in out:
            return None
        out[key] = val
    return out


def left_right(sides: dict[str, str], direction: str) -> tuple[str | None, str | None]:
    """東西の道路名を、進行方向に向かって (左, 右) に読み替える。南行きは左が東。"""
    east, west = sides.get("東"), sides.get("西")
    return (east, west) if direction == "南行き" else (west, east)


def _make_row(line: int, rec: dict, issues: list[Issue]) -> Row | None:
    def get(key: str) -> str:
        return (rec.get(key) or "").strip()

    direction = get("方向")
    if direction not in DIRECTIONS:
        issues.append(Issue("エラー", line, f"方向 '{direction}' が不正です（南行き／北行き）"))
        return None
    order_s = _nfkc(get("順"))
    try:
        order = int(order_s)
    except ValueError:
        issues.append(Issue("エラー", line, f"順 '{order_s}' が整数ではありません", direction=direction))
        return None

    row = Row(line=line, direction=direction, order=order, point=get("地点"), kind=get("種別"),
              lanes=None, conn="", source=get("出典"), conf=get("確度"), memo=get("メモ"),
              cross=get("交差道路"))

    def add(level: str, msg: str) -> None:
        issues.append(Issue(level, line, msg, row.where, direction))

    if not row.point:
        add("警告", "地点が空欄です")
    if row.kind not in KINDS:
        add("エラー", f"種別 '{row.kind}' が不正です（手前／直後／区間中）")

    lanes_raw = get("車線")
    lanes_norm = _nfkc(lanes_raw)
    if lanes_norm != lanes_raw:
        add("警告", "車線に全角の文字があったので半角として読みました")
    if lanes_norm:
        codes = []
        for j, tok in enumerate(lanes_norm.split()):
            if tok in LANE_CODES:
                codes.append(tok)
            else:
                add("エラー", f"車線コード '{tok}'（左から{j + 1}番目）が不正です{_suggest(tok)}")
                codes.append("?")
                row.invalid.add(j)
        row.lanes = codes

    conn_raw = get("接続")
    row.conn = _nfkc(conn_raw)
    if row.conn != conn_raw:
        add("警告", "接続に全角の文字があったので半角として読みました")

    if row.conf not in CONF_RANK:
        add("エラー", f"確度 '{row.conf}' が不正です（確／資／推／空欄）")
        row.conf = ""
    if cross_sides(row.cross) is None:
        add("エラー", f"交差道路 '{row.cross}' の書き方が不正です（例: 七条通　東:紫明通 西:鞍馬口通　東:二条通）")
        row.cross = ""
    return row


# ---------------------------------------------------------------- 検証と接続の解決

def analyze(records: list[tuple[int, dict]], issues: list[Issue] | None = None) -> Model:
    """行を方向ごとに並べ、接続を解決し、検証結果をまとめる。データは補完しない。"""
    issues = list(issues or [])
    rows = [r for r in (_make_row(line, rec, issues) for line, rec in records) if r]
    routes = {}
    for d in DIRECTIONS:
        rs = sorted((r for r in rows if r.direction == d), key=lambda r: (r.order, r.line))
        _check_order(rs, issues)
        crosses = _cross_names(rs)
        trans = [_resolve(rs[i], rs[i + 1] if i + 1 < len(rs) else None, crosses, issues)
                 for i in range(len(rs))]
        routes[d] = Route(d, rs, trans, crosses)
    _summarize_blanks(routes, issues)
    issues.sort(key=lambda i: (i.line is None, i.line or 0, LEVELS[i.level]))
    return Model(routes, issues)


def load(path: str | Path) -> Model:
    records, issues = read_csv(path)
    return analyze(records, issues)


def _check_order(rs: list[Row], issues: list[Issue]) -> None:
    """順と地点・種別の並びが矛盾していないか。"""
    for a, b in zip(rs, rs[1:]):
        if a.order == b.order:
            issues.append(Issue("エラー", b.line, f"順 {b.order} が {a.line}行目と重複しています",
                                b.where, b.direction))
    blocks: list[list[Row]] = []
    for r in rs:
        if blocks and blocks[-1][0].point == r.point:
            blocks[-1].append(r)
        else:
            blocks.append([r])
    seen: dict[str, int] = {}
    allowed = {("手前",), ("直後",), ("手前", "直後"), ("区間中",)}
    for block in blocks:
        head = block[0]
        if head.point and head.point in seen:
            issues.append(Issue("警告", head.line,
                                f"地点 '{head.point}' が {seen[head.point]}行目のあたりにも出てきます。"
                                "同じ地点の行は順を続けてください", head.where, head.direction))
        seen.setdefault(head.point, head.line)
        kinds = tuple(r.kind for r in block)
        if all(k in KINDS for k in kinds) and kinds not in allowed:
            r = block[1] if len(block) > 1 else head
            issues.append(Issue("警告", r.line,
                                f"地点 '{head.point}' の種別の並び（{'→'.join(kinds)}）が順と矛盾しています。"
                                "交差点は 手前→直後 の順に1行ずつ、区間中は単独で書きます",
                                r.where, r.direction))


def _cross_names(rs: list[Row]) -> dict[str, str]:
    names: dict[str, str] = {}
    for r in sorted(rs, key=lambda r: r.kind != "手前"):
        if r.cross and r.point not in names:
            names[r.point] = r.cross
    return names


def infer_side(code: str, j: int, n: int) -> str:
    """離脱の向き。L だけなら左、R だけなら右、それ以外は車線の位置で決める。"""
    has_l, has_r = "L" in code, "R" in code
    if has_l and not has_r:
        return "L"
    if has_r and not has_l:
        return "R"
    return "L" if j + 0.5 < n / 2 else "R"


_TOKEN = re.compile(r"(\d+)>(.+)")


def parse_conn(raw: str) -> tuple[list[tuple[str, int, str, int | None, str, str]], list[str]]:
    """接続の文字列を (元の書き方, 元の車線, 種類, 行き先, 通り名, 向き) の並びにする。"""
    tokens, bad = [], []
    for tok in raw.split():
        m = _TOKEN.fullmatch(tok)
        if not m:
            bad.append(tok)
            continue
        src, tgt = int(m.group(1)), m.group(2)
        if tgt.isdigit():
            tokens.append((tok, src, "lane", int(tgt), "", ""))
        elif tgt in ("x", "X"):
            tokens.append((tok, src, "cut", None, "", ""))
        elif tgt.startswith("@"):
            rest, side = tgt[1:], ""
            if rest[:1] in ("<", ">"):
                side, rest = ("L" if rest[0] == "<" else "R"), rest[1:]
            tokens.append((tok, src, "exit", None, rest.strip(), side))
        else:
            bad.append(tok)
    return tokens, bad


def links_to_text(links: list[Link]) -> str:
    """Link の並びを接続の書き方に戻す（エディタで自動補完の結果を見せる用）。"""
    out = []
    for l in sorted(links, key=lambda l: (l.src, l.dst if l.dst is not None else -1)):
        if l.kind == "lane":
            out.append(f"{l.src + 1}>{l.dst + 1}")
        elif l.kind == "exit":
            out.append(f"{l.src + 1}>@{l.name}")
        else:
            out.append(f"{l.src + 1}>x")
    return " ".join(out)


def _numbers(idx: list[int]) -> str:
    return "・".join(str(j + 1) for j in idx)


def _resolve(cur: Row, nxt: Row | None, crosses: dict[str, str], issues: list[Issue]) -> Transition:
    def add(level: str, msg: str) -> None:
        issues.append(Issue(level, cur.line, msg, cur.where, cur.direction))

    sides = cross_sides(crosses.get(cur.point, "")) or {}
    lname, rname = left_right(sides, cur.direction)

    def side_name(side: str) -> str:
        return (lname if side == "L" else rname) or ""

    def check_side(side: str, j: int) -> None:
        if sides and not side_name(side):
            east_west = "東" if (side == "L") == (cur.direction == "南行き") else "西"
            add("警告", f"車線{j + 1}が{'左' if side == 'L' else '右'}へ離脱していますが、"
                        f"交差道路では{east_west}側に道路がありません（T字路）")

    if cur.lanes is None:
        if cur.conn:
            add("警告", f"車線が未入力のため、接続 '{cur.conn}' は使えません")
        return Transition(cur, nxt, "blank")
    n = len(cur.lanes)

    # ---- 接続が書いてある
    if cur.conn:
        t = Transition(cur, nxt, "explicit")
        tokens, bad = parse_conn(cur.conn)
        for b in bad:
            add("エラー", f"接続 '{b}' の書き方が不正です（例: 1>1　2>@五条通　3>x）")
        covered: set[int] = set()
        seen: set[tuple] = set()
        unused_last = []
        for tok, src, kind, dst, name, side in tokens:
            if not 1 <= src <= n:
                add("エラー", f"接続 '{tok}': 車線{src}はありません（この行の車線は{n}本）")
                continue
            if (src, kind, dst, name) in seen:
                add("警告", f"接続 '{tok}' が重複しています")
                continue
            seen.add((src, kind, dst, name))
            covered.add(src - 1)
            if kind == "lane":
                if nxt is None:
                    unused_last.append(tok)
                elif nxt.lanes is None:
                    add("警告", f"接続 '{tok}': 次の行（{nxt.line}行目）の車線が未入力のため確かめられません")
                elif not 1 <= dst <= len(nxt.lanes):
                    add("エラー", f"接続 '{tok}': 次の行（{nxt.line}行目）に車線{dst}はありません"
                                  f"（次の行の車線は{len(nxt.lanes)}本）")
                else:
                    t.links.append(Link(src - 1, "lane", dst - 1))
            elif kind == "exit":
                side = side or infer_side(cur.lanes[src - 1], src - 1, n)
                if not name:
                    check_side(side, src - 1)
                t.links.append(Link(src - 1, "exit", name=name or side_name(side), side=side))
            else:
                add("警告", f"車線{src}は行き先を指定せずに途切れます（{tok}）")
                t.links.append(Link(src - 1, "cut"))
        if unused_last:
            add("警告", f"最後の行なので、次の行への接続 {' '.join(unused_last)} は使いません")
        if nxt is not None:
            missing = [j for j in range(n) if j not in covered]
            if missing:
                add("警告", f"車線{_numbers(missing)}の行き先が指定されていません")
            t.unresolved = missing
            if nxt.lanes:
                hit = {l.dst for l in t.links if l.kind == "lane"}
                t.new_lanes = [k for k in range(len(nxt.lanes)) if k not in hit]
        return t

    # ---- 接続が空欄: 自動補完
    if nxt is None:
        return Transition(cur, None, "end")
    if nxt.lanes is None:
        return Transition(cur, nxt, "blank")
    m = len(nxt.lanes)
    t = Transition(cur, nxt, "auto")
    if cur.kind == "手前" and nxt.kind == "直後" and cur.point == nxt.point:
        cs = [j for j, c in enumerate(cur.lanes) if "S" in c]
        ns = [k for k, c in enumerate(nxt.lanes) if "S" in c]
        other = []
        for j, c in enumerate(cur.lanes):
            if c in ("L", "R"):
                check_side(c, j)
                t.links.append(Link(j, "exit", name=side_name(c), side=c, auto=True))
            elif "S" not in c:
                other.append(j)
        if other:
            add("警告", f"車線{_numbers(other)}（{' '.join(cur.lanes[j] for j in other)}）は"
                        "自動で行き先を決められません。接続を入力してください")
        if len(cs) == len(ns):
            t.links += [Link(j, "lane", k, auto=True) for j, k in zip(cs, ns)]
        else:
            add("警告", f"直進できる車線が 手前{len(cs)}本 → 直後{len(ns)}本 で数が合わないため、"
                        "対応を決められません。接続を入力してください")
            other += cs
        t.unresolved = sorted(other)
    elif cur.kind in ("直後", "区間中") and nxt.kind in ("手前", "区間中"):
        if n == m:
            t.links = [Link(j, "lane", j, auto=True) for j in range(n)]
        else:
            add("警告", f"車線数が {n}本 → {m}本 と変わるため、対応を決められません。接続を入力してください")
            t.unresolved = list(range(n))
    else:
        if cur.kind == "手前" and nxt.kind == "直後":
            reason = f"手前（{cur.point}）と直後（{nxt.point}）の地点が違う"
        elif cur.kind == "手前":
            reason = f"{cur.point} の直後の行がなく、交差点をまたぐ"
        elif nxt.kind == "直後":
            reason = f"{nxt.point} の手前の行がなく、交差点をまたぐ"
        else:
            reason = "種別が不正な"
        add("警告", f"{reason}ため自動補完できません。接続を入力してください")
        t.unresolved = list(range(n))

    hit = {l.dst for l in t.links if l.kind == "lane"}
    rest = [k for k in range(m) if k not in hit]
    if t.unresolved:
        # 行き先の決まらない車線がある間は、次の行の車線を「新しく現れた」とは言えない
        t.unknown_origin = rest
        if not t.links:
            t.mode = "undetermined"
    else:
        t.new_lanes = rest
    return t


def _ranges(lines: list[int]) -> str:
    out, start, prev = [], None, None
    for x in lines:
        if start is None:
            start = prev = x
        elif x == prev + 1:
            prev = x
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = x
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(out)


def _summarize_blanks(routes: dict[str, Route], issues: list[Issue]) -> None:
    for d, route in routes.items():
        total = len(route.rows)
        if not total:
            continue
        blank = sorted(r.line for r in route.rows if r.lanes is None)
        if blank:
            issues.append(Issue("警告", None, f"車線が未入力の行が {len(blank)}/{total} 行あります"
                                             f"（{_ranges(blank)}行目）", d, d))
        noconf = sorted(r.line for r in route.rows if r.lanes is not None and not r.conf)
        if noconf:
            issues.append(Issue("情報", None, f"車線は入力済みで確度が空欄の行が {len(noconf)} 行あります"
                                             f"（{_ranges(noconf)}行目）", d, d))


def format_issues(model: Model) -> str:
    lines = [i.format() for i in model.issues]
    c = model.counts()
    lines.append(f"検証結果: エラー {c['エラー']} 件 / 警告 {c['警告']} 件 / 情報 {c['情報']} 件")
    return "\n".join(lines)


# ---------------------------------------------------------------- 描画

class G:
    """描画の寸法（px）。"""
    LW = 38          # 車線の幅
    LG = 4           # 車線の間
    ROW_H = 50       # 行（車線の帯）の高さ
    GAP = 84         # 行の間
    GAP_X = 116      # 交差点をはさむ行の間
    EDGE = 76        # 先頭・末尾の交差点や離脱のための余白
    LABEL_W = 210    # 左の地点名の欄
    SIDE = 160       # 道路の左右の余白（離脱と通り名）
    TOP = 78
    BOTTOM = 24
    EXIT_OUT = 44    # 離脱の矢印が道路の端からはみ出す長さ

    @classmethod
    def pitch(cls) -> float:
        return cls.LW + cls.LG


CONF_CLASS = {"確": "k", "資": "s", "推": "p", "": "u"}
CONF_TEXT = {"確": "確", "資": "資", "推": "推", "": "不明"}
GLYPH = {"L": "↰", "S": "↑", "R": "↱", "SL": "↰↑", "SR": "↑↱", "LSR": "↰↑↱"}
KIND_COLOR = {"手前": "#c2410c", "直後": "#0f766e", "区間中": "#6b7280"}
CAPTION = "進行方向が上（運転席から前を見た向き。左の車線が画面の左）"

SVG_STYLE = """<style>
.lm text{font-family:"Yu Gothic UI","Meiryo","Hiragino Sans",sans-serif;fill:#1f2933}
.lm .ttl{font-size:17px;font-weight:700}
.lm .cap{font-size:12px;fill:#52606d}
.lm .pt{font-size:14px;font-weight:700}
.lm .sub{font-size:11px;fill:#52606d}
.lm .badge{font-size:11px;font-weight:700;fill:#fff}
.lm .strip{fill:#e9ecef}
.lm .stripedge{stroke:#adb5bd;stroke-width:1}
.lm .stripname{font-size:12px;fill:#495057;font-weight:700}
.lm .stripend{font-size:11px;fill:#495057}
.lm .stop{stroke:#1f2933;stroke-width:3.5}
.lm .ln,.lm .rb{stroke-width:1.5}
.lm .ln.c-k,.lm .rb.c-k{fill:#2f5f98;stroke:#2f5f98}
.lm .ln.c-s,.lm .rb.c-s{fill:#a8c0df;stroke:#87a6cf}
.lm .ln.c-p,.lm .rb.c-p{fill:#e7eef8;stroke:#2f5f98;stroke-dasharray:5 3}
.lm .ln.c-u,.lm .rb.c-u{fill:#f3f3f3;stroke:#9aa0a6;stroke-dasharray:1.5 3}
.lm .ln.bad{stroke:#d92d20;stroke-width:2.5;stroke-dasharray:none}
.lm .ex{fill:none;stroke-width:12}
.lm .ex.c-k{stroke:#2f5f98}.lm .ex.c-s{stroke:#a8c0df}
.lm .ex.c-p{stroke:#2f5f98;stroke-dasharray:7 5}.lm .ex.c-u{stroke:#9aa0a6;stroke-dasharray:2 4}
.lm .ah.c-k,.lm .ah.c-p{fill:#2f5f98}.lm .ah.c-s{fill:#a8c0df}.lm .ah.c-u{fill:#9aa0a6}
.lm .gl{font-size:20px;text-anchor:middle;dominant-baseline:central;fill:#17324d}
.lm .gl.on-k{fill:#fff}.lm .gl.on-u{fill:#5f6368}
.lm .gl.t2{font-size:15px}.lm .gl.t3{font-size:12px}.lm .gl.bus{font-size:12px;font-weight:700}
.lm .exname{font-size:12px;font-weight:700;fill:#1f2933;paint-order:stroke;stroke:#fff;stroke-width:3px}
.lm .ph{fill:#fafafa;stroke:#9aa0a6;stroke-width:1.2;stroke-dasharray:2 3}
.lm .phtext{font-size:12px;fill:#7b8794;text-anchor:middle;dominant-baseline:central}
.lm .stub{fill:none;stroke:#9aa0a6;stroke-width:3;stroke-dasharray:3 3}
.lm .blank{fill:none;stroke:#c3c7cc;stroke-width:2;stroke-dasharray:2 5}
.lm .q{font-size:13px;font-weight:700;fill:#b54708;text-anchor:middle}
.lm .warn{font-size:12px;font-weight:700;fill:#b54708;text-anchor:middle;paint-order:stroke;stroke:#fff;stroke-width:4px}
.lm .cut{font-size:18px;font-weight:700;fill:#d92d20;text-anchor:middle}
.lm .flow{font-size:12px;fill:#52606d;text-anchor:middle}
.lm .sel{stroke:#f08c00 !important;stroke-width:3.5 !important;stroke-dasharray:none !important}
.lm text.sel{fill:#f08c00;stroke:none !important}
</style>"""


def _f(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _e(s: str) -> str:
    return html.escape(str(s), quote=True)


def _weaker(a: str, b: str) -> str:
    return a if CONF_RANK[a] <= CONF_RANK[b] else b


def _strips(a: Row | None, b: Row | None) -> list[str]:
    """2つの行の間にある交差点（地点名）。"""
    s = []
    if a is not None and a.kind == "手前":
        s.append(a.point)
    if b is not None and b.kind == "直後" and b.point not in s:
        s.append(b.point)
    return s


@dataclass
class _Layout:
    counts: list[int]                 # 各行の車線数（未入力は前後から借りた仮の幅）
    offs: list[float]                 # 各行の左端の位置（車線いくつ分）
    ys: list[float]                   # 各行の上端
    gaps: dict[int, tuple[float, float, list[str]]]   # 行 k の後ろの余白（-1 は先頭の前）
    x0: float
    road_w: float
    width: float
    height: float

    @property
    def cx(self) -> float:
        return self.x0 + self.road_w / 2


def _layout(route: Route) -> _Layout:
    rows, trans = route.rows, route.transitions
    known = [len(r.lanes) if r.lanes else None for r in rows]
    counts = []
    for i, c in enumerate(known):
        if c is None:
            before = next((x for x in reversed(known[:i]) if x), None)
            after = next((x for x in known[i + 1:] if x), None)
            c = before or after or 2
        counts.append(c)

    # 直進でつながる車線がまっすぐ並ぶように、行ごとに左右へずらす
    offs = [0.0]
    for k in range(len(rows) - 1):
        lanes = [l for l in trans[k].links if l.kind == "lane"]
        if lanes:
            sc, dc = Counter(l.src for l in lanes), Counter(l.dst for l in lanes)
            straight = [l for l in lanes if sc[l.src] == 1 and dc[l.dst] == 1]
            anchor = min(straight or lanes, key=lambda l: (l.src, l.dst))
            offs.append(offs[k] + anchor.src - anchor.dst)
        else:
            offs.append(offs[k])
    low = min(offs)
    offs = [o - low for o in offs]
    content = max(o + c for o, c in zip(offs, counts))

    gaps: dict[int, tuple[float, float, list[str]]] = {}
    top = _strips(None, rows[0])
    y = G.TOP + (G.EDGE if top else 0)
    if top:
        gaps[-1] = (y - G.EDGE, y, top)
    ys = []
    for k, row in enumerate(rows):
        ys.append(y)
        y += G.ROW_H
        if k + 1 < len(rows):
            strips = _strips(row, rows[k + 1])
            h = G.GAP_X if strips else G.GAP
        else:
            strips = _strips(row, None)
            h = G.EDGE if (strips or trans[k].links) else 0
        if h:
            gaps[k] = (y, y + h, strips)
        y += h
    x0 = G.LABEL_W + G.SIDE
    road_w = content * G.pitch() - G.LG
    return _Layout(counts, offs, ys, gaps, x0, road_w, x0 + road_w + G.SIDE, y + G.BOTTOM)


def _ribbon(a1: float, a2: float, y1: float, b1: float, b2: float, y2: float) -> str:
    ym = (y1 + y2) / 2
    return (f"M{_f(a1)},{_f(y1)} C{_f(a1)},{_f(ym)} {_f(b1)},{_f(ym)} {_f(b1)},{_f(y2)} "
            f"L{_f(b2)},{_f(y2)} C{_f(b2)},{_f(ym)} {_f(a2)},{_f(ym)} {_f(a2)},{_f(y1)} Z")


def _row_tip(row: Row, extra: str = "") -> str:
    lanes = " ".join(row.lanes) if row.lanes else "未入力"
    parts = [f"{row.line}行目 {row.where}", f"車線: {lanes}", f"接続: {row.conn or '（空欄）'}",
             f"確度: {CONF_TEXT[row.conf]}", f"出典: {row.source or '—'}"]
    if row.cross:
        parts.append(f"交差道路: {row.cross}")
    if row.memo:
        parts.append(f"メモ: {row.memo}")
    if extra:
        parts.insert(1, extra)
    return "\n".join(parts)


def render_svg(route: Route) -> str:
    """1方向分の車線接続図を SVG で返す。

    南行き・北行きとも進行方向を上にした、運転席から前を見た向きの図にする
    （左の車線が画面の左、矢印は路面標示どおり上向き）。
    """
    rows, trans = route.rows, route.transitions
    if not rows:
        return (f'<svg class="lm" xmlns="http://www.w3.org/2000/svg" width="480" height="70" '
                f'viewBox="0 0 480 70">{SVG_STYLE}<text class="ttl" x="16" y="40">'
                f'{_e(route.direction)}：行がありません</text></svg>')
    L = _layout(route)
    P, LW = G.pitch(), G.LW
    # 配置は上から下へ並べて計算し、図形は最後に上下反転して下から上へ進む図にする。
    # 文字は反転させず、位置だけ Y() で読み替えて置く
    K = G.TOP + L.height - G.BOTTOM

    def Y(y: float) -> float:
        return K - y

    def span(k: int, j: int) -> tuple[float, float]:
        a = L.x0 + (L.offs[k] + j) * P
        return (a, a + LW)

    def row_span(k: int) -> tuple[float, float]:
        a, b = span(k, 0), span(k, L.counts[k] - 1)
        return (min(a[0], b[0]), max(a[1], b[1]))

    def out_dir(side: str) -> int:
        return -1 if side == "L" else 1

    road_l, road_r = L.x0, L.x0 + L.road_w
    bg, fg, labels, glyphs = [], [], [], []

    # ---- 見出し
    labels.append(f'<text class="ttl" x="16" y="28">{_e(route.direction)}：'
                  f'{_e(rows[0].point)} → {_e(rows[-1].point)}</text>')
    labels.append(f'<text class="cap" x="16" y="48">{_e(CAPTION)}　線の見た目は確度（凡例）</text>')
    labels.append(f'<text class="flow" x="{_f(L.cx)}" y="{_f(G.TOP - 10)}">進行方向 ↑（下から上へ）</text>')

    # ---- 交差点（横の帯）
    # 画面の右が東か: 上空から見た向きの南行き（北が上）と、CSV の並び順の北行き
    # 画面の右が東か: 進行方向が上なので、北行きなら右が東、南行きなら右が西
    east_right = route.direction == "北行き"
    strip_names: dict[str, set[str]] = {}
    for k, (gy1, gy2, strips) in L.gaps.items():
        for i, p in enumerate(strips):
            yc = gy1 + (gy2 - gy1) * (i + 1) / (len(strips) + 1)
            sides = cross_sides(route.crosses.get(p, "")) or {}
            strip_names[p] = set(sides.values()) | {p}
            east, west = sides.get("東"), sides.get("西")
            scr_l, scr_r = (west, east) if east_right else (east, west)
            x1 = G.LABEL_W if (not sides or scr_l) else road_l - 10
            x2 = L.width - 8 if (not sides or scr_r) else road_r + 10
            bg.append(f'<rect class="strip" x="{_f(x1)}" y="{_f(yc - 15)}" '
                      f'width="{_f(x2 - x1)}" height="30"/>')
            for yy in (yc - 15, yc + 15):
                bg.append(f'<line class="stripedge" x1="{_f(x1)}" y1="{_f(yy)}" '
                          f'x2="{_f(x2)}" y2="{_f(yy)}"/>')
            if not sides:
                name = p
            elif east == west:
                name = east
            else:
                name = f"東:{east or 'なし'} 西:{west or 'なし'}"
            labels.append(f'<text class="stripname" x="24" y="{_f(Y(yc) + 4)}">┄ {_e(name)}'
                          f'{"" if name == p else "（" + _e(p) + "）"}</text>')
            if sides and east != west:
                for nm, ew, at_right in ((east, "東", east_right), (west, "西", not east_right)):
                    if nm:
                        x = x2 - 8 if at_right else x1 + 8
                        labels.append(f'<text class="stripend" x="{_f(x)}" y="{_f(Y(yc) + 4)}" '
                                      f'text-anchor="{"end" if at_right else "start"}">'
                                      f'{_e(nm)}（{ew}）</text>')

    # ---- 行と行のあいだ
    for k, t in enumerate(trans):
        row, nxt = t.cur, t.nxt
        y1 = L.ys[k] + G.ROW_H
        gap = L.gaps.get(k)
        y2 = L.ys[k + 1] if nxt is not None else (gap[1] if gap else y1)
        gh = y2 - y1
        tip = _e(_row_tip(row, f"→ {nxt.line}行目へ" if nxt else ""))

        if t.mode == "blank" and nxt is not None:
            a, b = row_span(k), row_span(k + 1)
            fg.append(f'<path class="blank" d="M{_f((a[0] + a[1]) / 2)},{_f(y1)} '
                      f'L{_f((b[0] + b[1]) / 2)},{_f(y2)}"/>')

        # 車線どうしのつながり（合流は細くして寄せる）
        lanes = [l for l in t.links if l.kind == "lane"]
        if nxt is not None and lanes:
            conf = _weaker(row.conf, nxt.conf)
            by_dst: dict[int, list[Link]] = {}
            for l in lanes:
                by_dst.setdefault(l.dst, []).append(l)
            for dst, group in by_dst.items():
                b1, b2 = span(k + 1, dst)
                bc = (b1 + b2) / 2
                main = min(group, key=lambda l: abs(sum(span(k, l.src)) / 2 - bc))
                for l in sorted(group, key=lambda l: l is main):
                    a1, a2 = span(k, l.src)
                    ac = (a1 + a2) / 2
                    # 合流する側は行間の途中で細くなって消える（同じ位置に現れる車線と重ならない）
                    ym = y1 + gh * 0.55
                    if l is main:
                        d = _ribbon(a1, a2, y1, b1, b2, y2)
                    elif ac < bc:
                        d = _ribbon(a1, a2, y1, b1, b1 + 3, ym)
                    else:
                        d = _ribbon(a1, a2, y1, b2 - 3, b2, ym)
                    what = "（合流）" if len(group) > 1 else ""
                    fg.append(f'<path class="rb c-{CONF_CLASS[conf]}" data-line="{row.line}" d="{d}">'
                              f'<title>{row.line}行目 車線{l.src + 1} → {nxt.line}行目 車線{dst + 1}'
                              f'{what}{"（自動）" if l.auto else ""}</title></path>')

        # 新しく現れる車線（先端を細く）
        if nxt is not None and nxt.lanes:
            for dst in t.new_lanes:
                b1, b2 = span(k + 1, dst)
                new = set(t.new_lanes)
                left_ok = dst - 1 >= 0 and dst - 1 not in new
                right_ok = dst + 1 < len(nxt.lanes) and dst + 1 not in new
                # 隣の既存車線の側から生える（画面上の左右は向きで入れ替わる）
                if left_ok == right_ok:
                    tipx = (b1 + b2) / 2
                elif left_ok:
                    tipx = b1
                else:
                    tipx = b2
                ys0 = y1 + gh * 0.6
                d = _ribbon(tipx - 1, tipx + 1, ys0, b1, b2, y2)
                fg.append(f'<path class="rb c-{CONF_CLASS[nxt.conf]}" data-line="{nxt.line}" d="{d}">'
                          f'<title>{nxt.line}行目 車線{dst + 1}：ここから現れる車線</title></path>')

        # 側道への離脱
        exits = [l for l in t.links if l.kind == "exit"]
        if exits:
            if gap and gap[2]:
                yc = gap[0] + (gap[1] - gap[0]) / (len(gap[2]) + 1)
                here = strip_names.get(gap[2][0], set())
            else:
                yc = y1 + max(gh, G.EDGE) * 0.55
                here = None
            by_side: dict[int, list[Link]] = {}
            for l in exits:
                by_side.setdefault(out_dir(l.side), []).append(l)
            for d_, group in by_side.items():
                group.sort(key=lambda l: -d_ * sum(span(k, l.src)))   # 外側の車線から
                labelled = set()
                for i, l in enumerate(group):
                    a1, a2 = span(k, l.src)
                    cx = (a1 + a2) / 2
                    ye = yc + (i - (len(group) - 1) / 2) * 15
                    xe = (road_r if d_ > 0 else road_l) + d_ * G.EXIT_OUT
                    cls = CONF_CLASS[row.conf]
                    turn = "左" if l.side == "L" else "右"
                    if l.name and (here is None or l.name not in here):
                        label = l.name
                    elif l.name:
                        label = ""
                    else:
                        label = f"{turn}折" if here is not None else f"{turn}の側道"
                    fg.append(f'<path class="ex c-{cls}" data-line="{row.line}" '
                              f'd="M{_f(cx)},{_f(y1)} C{_f(cx)},{_f(ye)} {_f(cx)},{_f(ye)} '
                              f'{_f(xe)},{_f(ye)}"><title>{row.line}行目 車線{l.src + 1}：'
                              f'{_e(l.name or turn + "の側道")}へ離脱{"（自動）" if l.auto else ""}'
                              f'</title></path>')
                    fg.append(f'<polygon class="ah c-{cls}" points="{_f(xe)},{_f(ye - 9)} '
                              f'{_f(xe + d_ * 14)},{_f(ye)} {_f(xe)},{_f(ye + 9)}"/>')
                    if label and label not in labelled:
                        labelled.add(label)
                        anchor = "start" if d_ > 0 else "end"
                        labels.append(f'<text class="exname" x="{_f(xe + d_ * 19)}" y="{_f(Y(ye) + 4)}" '
                                      f'text-anchor="{anchor}">{_e(label)}</text>')

        # 途切れ（x）
        for l in t.links:
            if l.kind == "cut":
                a1, a2 = span(k, l.src)
                cx = (a1 + a2) / 2
                fg.append(f'<path class="stub" d="M{_f(cx)},{_f(y1)} L{_f(cx)},{_f(y1 + 18)}"/>')
                labels.append(f'<text class="cut" x="{_f(cx)}" y="{_f(Y(y1 + 30) + 6)}">×'
                              f'<title>{row.line}行目 車線{l.src + 1}：行き先を指定せず途切れる</title></text>')

        # 行き先が決まらない車線・どこから来たか決まらない車線
        if t.unresolved or t.unknown_origin:
            for j in t.unresolved:
                a1, a2 = span(k, j)
                cx = (a1 + a2) / 2
                fg.append(f'<path class="stub" d="M{_f(cx)},{_f(y1)} L{_f(cx)},{_f(y1 + 18)}"/>')
                labels.append(f'<text class="q" x="{_f(cx)}" y="{_f(Y(y1 + 28) + 5)}">?</text>')
            if nxt is not None and nxt.lanes:
                for dst in t.unknown_origin:
                    b1, b2 = span(k + 1, dst)
                    cx = (b1 + b2) / 2
                    fg.append(f'<path class="stub" d="M{_f(cx)},{_f(y2 - 18)} L{_f(cx)},{_f(y2)}"/>')
                    labels.append(f'<text class="q" x="{_f(cx)}" y="{_f(Y(y2 - 27) + 5)}">?</text>')
            if nxt is not None:
                labels.append(f'<text class="warn" x="{_f(L.cx)}" y="{_f(Y(y1 + gh / 2) + 4)}">'
                              f'対応未定<title>{tip}</title></text>')

    # ---- 行（車線の帯）
    for k, row in enumerate(rows):
        y = L.ys[k]
        tip = _e(_row_tip(row))
        if row.lanes is None:
            a, b = row_span(k)
            fg.append(f'<rect class="ph" data-line="{row.line}" x="{_f(a)}" y="{_f(y)}" '
                      f'width="{_f(b - a)}" height="{G.ROW_H}" rx="4"><title>{tip}</title></rect>')
            labels.append(f'<text class="phtext" x="{_f((a + b) / 2)}" y="{_f(Y(y + G.ROW_H / 2))}">'
                          f'未入力</text>')
        else:
            cls = CONF_CLASS[row.conf]
            for j, code in enumerate(row.lanes):
                a1, a2 = span(k, j)
                cx, cy = (a1 + a2) / 2, Y(y + G.ROW_H / 2)
                bad = " bad" if j in row.invalid else ""
                fg.append(f'<rect class="ln c-{cls}{bad}" data-line="{row.line}" x="{_f(a1)}" y="{_f(y)}" '
                          f'width="{LW}" height="{G.ROW_H}" rx="3"><title>車線{j + 1}: {_e(code)}\n'
                          f'{tip}</title></rect>')
                if code == "B":
                    glyphs.append(f'<text class="gl bus on-{cls}" x="{_f(cx)}" y="{_f(cy)}">バス</text>')
                elif code == "?":
                    glyphs.append(f'<text class="gl on-{cls}" x="{_f(cx)}" y="{_f(cy)}">?</text>')
                else:
                    g = GLYPH[code]
                    glyphs.append(f'<text class="gl on-{cls} t{len(g)}" x="{_f(cx)}" y="{_f(cy)}">{g}</text>')
            if row.kind == "手前":
                a, b = row_span(k)
                fg.append(f'<line class="stop" x1="{_f(a - 3)}" y1="{_f(y + G.ROW_H + 2)}" '
                          f'x2="{_f(b + 3)}" y2="{_f(y + G.ROW_H + 2)}"><title>停止線</title></line>')

        # 左の欄: 地点名と種別
        cy = Y(y + G.ROW_H / 2)
        color = KIND_COLOR.get(row.kind, "#d92d20")
        kind = row.kind or "種別なし"
        bw = 12 * len(kind) + 10
        labels.append(f'<g data-line="{row.line}"><title>{tip}</title>'
                      f'<text class="pt" x="16" y="{_f(cy - 5)}">{_e(row.point or "（地点なし）")}</text>'
                      f'<rect x="16" y="{_f(cy + 3)}" width="{bw}" height="16" rx="3" fill="{color}"/>'
                      f'<text class="badge" x="{16 + bw / 2}" y="{_f(cy + 15)}" text-anchor="middle">'
                      f'{_e(kind)}</text>'
                      f'<text class="sub" x="{16 + bw + 6}" y="{_f(cy + 15)}">'
                      f'確度:{CONF_TEXT[row.conf]}　{row.line}行目</text></g>')

    return (f'<svg class="lm" xmlns="http://www.w3.org/2000/svg" width="{_f(L.width)}" '
            f'height="{_f(L.height)}" viewBox="0 0 {_f(L.width)} {_f(L.height)}" '
            f'data-direction="{_e(route.direction)}">{SVG_STYLE}'
            f'<rect width="100%" height="100%" fill="#fff"/>'
            + f'<g transform="matrix(1 0 0 -1 0 {_f(K)})">'
            + "".join(bg) + "".join(fg) + "</g>" + "".join(glyphs) + "".join(labels) + "</svg>")


def render_legend() -> str:
    """凡例（小さな SVG を並べた HTML）。"""
    def sw(body: str, w: int = 44) -> str:
        return (f'<svg class="lm" xmlns="http://www.w3.org/2000/svg" width="{w}" height="34" '
                f'viewBox="0 0 {w} 34">{body}</svg>')

    items = [(sw(f'<rect class="ln c-{CONF_CLASS[c]}" x="12" y="2" width="20" height="30" rx="3"/>'), t)
             for c, t in (("確", "確：現地・ストリートビューで確認"), ("資", "資：資料にある"),
                          ("推", "推：推定"), ("", "空欄：確度不明"))]
    items += [
        (sw(f'<path class="rb c-k" d="{_ribbon(4, 18, 2, 22, 25, 32)}"/>'
            f'<path class="rb c-k" d="{_ribbon(22, 36, 2, 22, 36, 32)}"/>'), "合流（細くして寄せる）"),
        (sw('<path class="ex c-k" d="M10,2 C10,22 10,22 28,22"/>'
            '<polygon class="ah c-k" points="28,14 40,22 28,30"/>'), "側道へ離脱"),
        (sw(f'<path class="rb c-k" d="{_ribbon(13, 15, 2, 14, 32, 32)}"/>'), "新しく現れる車線"),
        (sw('<rect class="ph" x="8" y="2" width="28" height="30" rx="3"/>'), "未入力"),
        (sw('<path class="stub" d="M22,2 L22,16"/><text class="q" x="22" y="30">?</text>'),
         "対応未定（接続を入力）"),
        (sw('<line class="stop" x1="6" y1="17" x2="38" y2="17"/>'), "停止線（手前の行）"),
    ]
    return '<div class="legend">' + "".join(
        f'<span class="lg">{svg}<span>{_e(text)}</span></span>' for svg, text in items) + "</div>"


PAGE_STYLE = """
:root{--bg:#f5f6f8;--card:#fff;--ink:#1f2933;--muted:#52606d;--line:#d9dee4;--accent:#2f5f98}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Yu Gothic UI","Meiryo","Hiragino Sans",sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--card);border-bottom:1px solid var(--line);
  padding:10px 16px;display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center}
h1{font-size:17px;margin:0}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{border:0;background:var(--card);padding:6px 14px;font:inherit;font-size:13px;cursor:pointer;color:var(--ink)}
.seg button+button{border-left:1px solid var(--line)}
.seg button.on{background:var(--accent);color:#fff}
.lbl{font-size:12px;color:var(--muted);margin-right:4px}
.count{font-size:13px;color:var(--muted)}
main{padding:16px;display:grid;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto}
.card h2{font-size:14px;margin:0 0 8px}
.pane[hidden],.lint[hidden]{display:none}
.lint ul{margin:0;padding-left:0;list-style:none;font-size:13px;line-height:1.7}
.lint li{padding:1px 6px;border-radius:4px}
.lv-エラー{color:#b42318;background:#fef3f2}
.lv-警告{color:#93370d}
.lv-情報{color:var(--muted)}
.note{font-size:12px;color:var(--muted);margin:6px 0 0}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12px}
.lg{display:inline-flex;align-items:center;gap:4px}
"""

PAGE_SCRIPT = """
(function(){
  var dir = document.body.dataset.dir;
  function apply(){
    document.querySelectorAll('.pane').forEach(function(p){ p.hidden = p.dataset.dir !== dir; });
    document.querySelectorAll('.lint').forEach(function(p){ p.hidden = p.dataset.dir !== dir; });
    document.querySelectorAll('[data-set-dir]').forEach(function(b){ b.classList.toggle('on', b.dataset.setDir === dir); });
  }
  document.querySelectorAll('[data-set-dir]').forEach(function(b){ b.onclick = function(){ dir = b.dataset.setDir; apply(); }; });
  apply();
})();
"""


def render_html(model: Model, title: str = "車線接続図") -> str:
    """南行き／北行きを切り替えられる自己完結の HTML を返す。"""
    c = model.counts()
    panes, lints = [], []
    for d in DIRECTIONS:
        route = model.routes[d]
        panes.append(f'<div class="pane card" data-dir="{d}">{render_svg(route)}</div>')
        items = [i for i in model.issues if i.direction in (d, "")]
        lis = "".join(f'<li class="lv-{i.level}">{_e(i.format())}</li>' for i in items) or "<li>問題はありません</li>"
        lints.append(f'<section class="lint card" data-dir="{d}"><h2>検証結果（{d}）</h2><ul>{lis}</ul></section>')
    first = next((d for d in DIRECTIONS if model.routes[d].rows), DIRECTIONS[0])
    dir_buttons = "".join(f'<button data-set-dir="{d}">{d}</button>' for d in DIRECTIONS)
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title><style>{PAGE_STYLE}</style></head>
<body data-dir="{first}">
<header><h1>{_e(title)}</h1>
<span class="seg">{dir_buttons}</span>
<span class="count">エラー {c['エラー']} / 警告 {c['警告']} / 情報 {c['情報']}</span>
</header>
<main>
<section class="card"><h2>凡例</h2>{render_legend()}
<p class="note">南行き・北行きとも進行方向が上（下から上へ進む）、運転席から前を見た向きの図です。矢印は路面標示（↰ ↑ ↱）のとおりです。空欄のデータは推測で埋めず「未入力」「対応未定」と描きます。</p></section>
{''.join(panes)}
{''.join(lints)}
</main>
<script>{PAGE_SCRIPT}</script>
</body></html>
"""


def default_title(path: str | Path) -> str:
    return f"車線接続図（{Path(path).name}）"


# ---------------------------------------------------------------- コマンドライン

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CSV から車線接続図（HTML）を作る")
    ap.add_argument("csv", help="入力 CSV（UTF-8）")
    ap.add_argument("-o", "--output", default="lanes.html", help="出力 HTML（既定: lanes.html）")
    ap.add_argument("--title", help="図のタイトル")
    ap.add_argument("--strict", action="store_true", help="エラーがあれば終了コード 1 を返す")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")

    model = load(args.csv)
    Path(args.output).write_text(render_html(model, args.title or default_title(args.csv)),
                                 encoding="utf-8")
    print(format_issues(model))
    print(f"出力: {args.output}")
    return 1 if args.strict and model.counts()["エラー"] else 0


if __name__ == "__main__":
    sys.exit(main())
