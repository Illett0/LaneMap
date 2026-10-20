#!/usr/bin/env python3
"""LaneMap 入力補助エディタ（ローカルサーバー、標準ライブラリのみ）

    python editor.py horikawa-lanes.csv [--port 8765] [--html lanes.html] [--no-browser]

ブラウザで CSV を行ごとに編集し、図と検証結果をその場で確かめられる。
描画と検証は render.py と同じ関数を使う。保存のたびに
  - 元のファイルを（CSV と同じフォルダの）history/backups/ に残す
  - 変更点を history/changes.csv に追記する
  - 図（既定は CSV と同じフォルダの lanes.html）を作り直す
PID ファイル（editor.pid）で重複起動を防ぐ。起動・停止は start-editor.ps1 / stop-editor.ps1。
"""
from __future__ import annotations

import argparse
import atexit
import codecs
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import render

ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / "editor.pid"
EDITOR_HTML = ROOT / "editor" / "index.html"
MAX_BODY = 5 * 1024 * 1024
CHANGE_COLUMNS = ["日時", "ファイル", "操作", "方向", "順", "地点", "種別", "項目", "変更前", "変更後"]


# ---------------------------------------------------------------- PID ファイル

def _pid_alive(pid: int) -> bool:
    """PID のプロセスが動いていて、python であるか。"""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:   # STILL_ACTIVE
            return False
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return "python" in os.path.basename(buf.value).lower()
        return True
    finally:
        k32.CloseHandle(handle)


def acquire_pid() -> None:
    if PID_FILE.exists():
        try:
            old = int(PID_FILE.read_text(encoding="ascii").strip())
        except ValueError:
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            sys.exit(f"エディタはすでに起動しています（PID {old}）。stop-editor.ps1 で止めてから起動してください。")
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    atexit.register(release_pid)


def release_pid() -> None:
    try:
        if PID_FILE.read_text(encoding="ascii").strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- CSV の読み書き

def parse_entries(text: str) -> tuple[list[str], list[dict]]:
    """CSV を見出しと「行」の並びにする。コメント行・空行も位置ごと残して、保存で書き戻す。"""
    raw_lines = re.split(r"\r\n|\r|\n", text)
    reader = csv.reader(io.StringIO(text, newline=""))
    header: list[str] | None = None
    entries: list[dict] = []
    prev = 0
    for n, fields in enumerate(reader):
        start, prev = prev + 1, reader.line_num
        eid = f"e{n}"
        if not fields or all(not f.strip() for f in fields) or fields[0].lstrip().startswith("#"):
            entries.append({"_id": eid, "_raw": "\n".join(raw_lines[start - 1:prev])})
        elif header is None:
            header = [f.strip() for f in fields]
            entries.append({"_id": eid, "_header": True})
        else:
            rec = {h: (fields[i] if i < len(fields) else "") for i, h in enumerate(header)}
            rec["_id"] = eid
            entries.append(rec)
    if header is None:
        header = []
        entries.insert(0, {"_id": "header", "_header": True})
    for col in render.COLUMNS + render.OPTIONAL_COLUMNS:
        if col not in header:
            header.append(col)
    return header, entries


def is_row(e: dict) -> bool:
    return "_raw" not in e and not e.get("_header")


def to_csv_text(header: list[str], entries: list[dict], newline: str) -> tuple[str, dict[str, int]]:
    """保存するときと同じ CSV の文字列と、各行の行番号を返す。"""
    if not any(e.get("_header") for e in entries):
        entries = [{"_id": "header", "_header": True}] + entries
    chunks, lines, line = [], {}, 1
    for e in entries:
        if "_raw" in e:
            chunk = str(e["_raw"]).replace("\r\n", "\n").replace("\n", newline) + newline
        else:
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator=newline)
            if e.get("_header"):
                writer.writerow(header)
            else:
                writer.writerow([e.get(c, "") for c in header])
                lines[e["_id"]] = line
            chunk = buf.getvalue()
        chunks.append(chunk)
        line += chunk.count("\n")
    return "".join(chunks), lines


def clean_entries(header: list[str], entries) -> list[dict]:
    """ブラウザから来た行を、見出しの列と文字列だけに整える。"""
    if not isinstance(entries, list):
        raise ValueError("entries がありません")
    out = []
    for e in entries:
        if not isinstance(e, dict) or not isinstance(e.get("_id"), str):
            raise ValueError("行の形式が不正です")
        if "_raw" in e:
            out.append({"_id": e["_id"], "_raw": str(e["_raw"])})
        elif e.get("_header"):
            out.append({"_id": e["_id"], "_header": True})
        else:
            rec = {c: str(e.get(c, "") or "") for c in header}
            rec["_id"] = e["_id"]
            out.append(rec)
    return out


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 状態

class Store:
    def __init__(self, csv_path: Path, html_path: Path):
        self.path = csv_path
        self.html_path = html_path
        self.lock = threading.Lock()
        self.load()

    def load(self) -> None:
        data = self.path.read_bytes() if self.path.exists() else b""
        self.hash = _sha(data)
        self.bom = data.startswith(codecs.BOM_UTF8)
        text = data.decode("utf-8-sig")
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.header, self.entries = parse_entries(text)
        self.snapshot = {e["_id"]: dict(e) for e in self.entries if is_row(e)}
        self.working = self.entries

    def title(self) -> str:
        return render.default_title(self.path)

    def analyze(self, entries: list[dict]):
        text, lines = to_csv_text(self.header, entries, self.newline)
        records, issues = render.parse_csv_text(text)
        return text, lines, render.analyze(records, issues)

    def preview(self, entries: list[dict]) -> dict:
        _, lines, model = self.analyze(entries)
        self.working = entries
        by_line = {v: k for k, v in lines.items()}
        auto = {}
        for route in model.routes.values():
            for t in route.transitions:
                if t.cur.conn or t.mode not in ("auto", "undetermined"):
                    continue
                eid = by_line.get(t.cur.line)
                if eid:
                    auto[eid] = {"text": render.links_to_text(t.links),
                                 "unresolved": [j + 1 for j in t.unresolved]}
        svgs = {d: render.render_svg(model.routes[d]) for d in render.DIRECTIONS}
        c = model.counts()
        return {"issues": [i.to_dict() for i in model.issues], "lines": lines, "auto": auto,
                "svgs": svgs, "counts": {k: c[k] for k in render.LEVELS}}

    def save(self, entries: list[dict], force: bool) -> tuple[int, dict]:
        current = self.path.read_bytes() if self.path.exists() else b""
        if _sha(current) != self.hash and not force:
            return 409, {"error": "ファイルがエディタの外で変更されています。再読み込みするか、上書き保存を選んでください。"}
        text, _, model = self.analyze(entries)
        data = (codecs.BOM_UTF8 if self.bom else b"") + text.encode("utf-8")
        changes = diff_entries(self.header, self.snapshot, entries)
        if data == current:
            return 200, {"ok": True, "changes": 0, "message": "変更はありません"}

        now = dt.datetime.now()
        backup = None
        if current:
            bdir = self.path.parent / "history" / "backups"
            bdir.mkdir(parents=True, exist_ok=True)
            backup = bdir / f"{self.path.stem}-{now:%Y%m%d-%H%M%S}.csv"
            i = 1
            while backup.exists():
                backup = bdir / f"{self.path.stem}-{now:%Y%m%d-%H%M%S}-{i}.csv"
                i += 1
            backup.write_bytes(current)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.path)
        append_changes(self.path.parent / "history", now, self.path.name, changes)
        self.html_path.write_text(render.render_html(model, self.title()), encoding="utf-8")

        self.hash = _sha(data)
        self.entries = self.working = entries
        self.snapshot = {e["_id"]: dict(e) for e in entries if is_row(e)}
        return 200, {"ok": True, "changes": len(changes), "backup": backup.name if backup else None,
                     "html": str(self.html_path)}


def _summary(header: list[str], e: dict) -> str:
    return " / ".join(f"{c}={e[c]}" for c in header if e.get(c))


def diff_entries(header: list[str], old: dict[str, dict], new_entries: list[dict]) -> list[tuple]:
    out = []
    new = {e["_id"]: e for e in new_entries if is_row(e)}
    for eid, e in new.items():
        o = old.get(eid)
        if o is None:
            out.append(("行追加", e, "", "", _summary(header, e)))
            continue
        for c in header:
            if o.get(c, "") != e.get(c, ""):
                out.append(("変更", e, c, o.get(c, ""), e.get(c, "")))
    for eid, o in old.items():
        if eid not in new:
            out.append(("行削除", o, "", _summary(header, o), ""))
    return out


def append_changes(history: Path, now: dt.datetime, filename: str, changes: list[tuple]) -> None:
    if not changes:
        return
    history.mkdir(parents=True, exist_ok=True)
    path = history / "changes.csv"
    new_file = not path.exists()
    with open(path, "a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(CHANGE_COLUMNS)
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        for op, e, col, before, after in changes:
            w.writerow([stamp, filename, op, e.get("方向", ""), e.get("順", ""), e.get("地点", ""),
                        e.get("種別", ""), col, before, after])


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "LaneMapEditor/1"
    store: Store
    allowed_hosts: set[str]

    def log_message(self, fmt, *args):   # アクセスログは出さない
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        if self.headers.get("Host", "") in self.allowed_hosts:
            return True
        self._send(403, "許可されていないホストです".encode(), "text/plain; charset=utf-8")
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        path = urlparse(self.path).path
        st = self.store
        if path in ("/", "/index.html"):
            self._send(200, EDITOR_HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/data":
            with st.lock:
                self._json(200, {"file": st.path.name, "path": str(st.path), "html": str(st.html_path),
                                 "header": st.header, "entries": st.entries,
                                 "codes": render.LANE_CODES, "kinds": render.KINDS,
                                 "directions": render.DIRECTIONS})
        elif path == "/view":
            with st.lock:
                _, _, model = st.analyze(st.working)
                body = render.render_html(model, st.title() + "（保存前の内容）")
            self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self._host_ok():
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._send(415, b"json only", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, b"too large", "text/plain")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "JSON が読めません"})
        path = urlparse(self.path).path
        st = self.store
        try:
            if path == "/api/preview":
                with st.lock:
                    self._json(200, st.preview(clean_entries(st.header, body.get("entries"))))
            elif path == "/api/save":
                with st.lock:
                    code, res = st.save(clean_entries(st.header, body.get("entries")), bool(body.get("force")))
                if code == 200 and res.get("changes"):
                    print(f"{dt.datetime.now():%H:%M:%S} 保存しました（変更 {res['changes']} 件）", flush=True)
                self._json(code, res)
            elif path == "/api/reload":
                with st.lock:
                    st.load()
                self._json(200, {"ok": True})
            elif path == "/api/shutdown":
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._send(404, b"not found", "text/plain")
        except ValueError as e:
            self._json(400, {"error": str(e)})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LaneMap 入力補助エディタ")
    ap.add_argument("csv", nargs="?", default="horikawa-lanes.csv", help="編集する CSV")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--html", help="保存のたびに作り直す図（既定: CSV と同じフォルダの lanes.html）")
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")

    csv_path = Path(args.csv).resolve()
    if not csv_path.exists():
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(",".join(render.COLUMNS + render.OPTIONAL_COLUMNS) + "\n", encoding="utf-8")
        print(f"CSV がなかったので、見出しだけの新しい CSV を作りました: {csv_path}", flush=True)
    html_path = Path(args.html).resolve() if args.html else csv_path.with_name("lanes.html")

    acquire_pid()
    Handler.store = Store(csv_path, html_path)
    Handler.allowed_hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"ポート {args.port} を開けません（{e}）。すでに起動していないか確かめてください。", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{args.port}/"
    print(f"{dt.datetime.now():%H:%M:%S} LaneMap エディタを起動しました: {url}（PID {os.getpid()}）", flush=True)
    print(f"編集中: {csv_path}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print(f"{dt.datetime.now():%H:%M:%S} 停止しました", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
