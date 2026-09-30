#!/usr/bin/env python3
"""pd_index.py - chỉ mục toàn văn cho thư mục physical design (chạy trên máy, 0 token).

Lệnh:
  build   [--data-dir D] [--db F] [--full] [-v]     làm mới chỉ mục (tăng dần theo size/mtime)
  search  "từ khoá" [--kind doc|script|report|log|data] [-n 15] [--all] [--json]
  show    "đường dẫn" [--page N | --chunk N | --lines A-B | --grep TXT [-C 2]] [--max 120]
  stats

Mặc định data-dir/db lấy từ config.json của PD_Bridge (xem bridge.py) hoặc biến
PD_DATA_DIR. Chỉ mục nằm ở <data-dir>/.pd_index/index.db.

Kết quả search trả về đường dẫn + vị trí (trang PDF / dải dòng) + đoạn trích ngắn,
để Claude chỉ đọc đúng phần cần thiết thay vì mở cả file lớn.
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
import zipfile
from html.parser import HTMLParser
from pathlib import Path

try:  # Windows console mặc định cp1252 -> in tiếng Việt lỗi
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCHEMA_VERSION = "2"
CHUNK_CHARS = 3000
MAX_TEXT_BYTES = 8 * 1024 * 1024       # file text lớn hơn: chỉ lấy phần đầu
HEAD_BYTES_BIG = 1 * 1024 * 1024
MAX_PDF_BYTES = 300 * 1024 * 1024

DOC_EXT = {".pdf", ".html", ".htm", ".docx", ".pptx", ".xlsx", ".md", ".txt", ".rst", ".csv"}
SCRIPT_EXT = {".tcl", ".sdc", ".py", ".pl", ".sh", ".csh", ".tcsh", ".bash", ".mk", ".cmd",
              ".upf", ".cpf", ".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl", ".tf", ".cfg",
              ".conf", ".yaml", ".yml", ".json", ".ini", ".view", ".mmmc", ".globals", ".tcl_"}
REPORT_EXT = {".rpt", ".rep", ".report", ".summary", ".sum", ".timing", ".drc", ".lvs",
              ".erc", ".ant", ".violations", ".err", ".qor", ".tarpt", ".slk", ".cap", ".tran",
              ".fanout", ".length", ".glitch", ".rpt_", ".viol"}
LOG_EXT = {".log", ".logv", ".out", ".cmd_log"}
DATA_EXT = {".lef", ".def", ".lib", ".db", ".gds", ".gds2", ".oas", ".oasis", ".spef",
            ".sdf", ".ndm", ".mw", ".enc", ".dat", ".gz", ".tar", ".zip", ".7z", ".rar",
            ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".ico", ".exe", ".dll", ".so",
            ".bin", ".o", ".a", ".pyc", ".mp4", ".avi", ".mov", ".xls", ".ppt", ".doc",
            ".vsdx", ".lnk", ".chm", ".odt"}
NAME_KIND = {"makefile": "script", "readme": "doc", ".cshrc": "script", "gnumakefile": "script"}
# Thư mục bỏ qua hoàn toàn
SKIP_DIRS = {".pd_index", ".git", ".svn", "__pycache__", "node_modules", ".claude",
             "$recycle.bin", "system volume information", ".idea", ".vscode"}
# Thư mục chỉ lấy metadata (database Innovus/ICC rất lớn)
META_ONLY_DIR_SUFFIX = (".dat", ".enc.dat", ".dbs", ".ndm", ".nlib", ".mw", ".cdb")

EN_STOP = set("""a an the of to in on for and or is are be by with from at as it this that these
those what which how why when where who whom can could should would will do does did not no
yes if then than so into over under about between after before during vs via per""".split())
VI_STOP = set("""va la co cho cac nhung khi sau truoc trong mot nay thi de ve voi nhu the nao
gi bao nhieu tai sao o cua duoc bi hay hoac neu vi nen ma rang tu den len xuong ra vao con
cung da dang se khong chua toi ban minh anh em chi hoi giup xem lam sao the nao giai thich
so sanh cach dung lenh file thu muc mode nhanh chuan sau""".split())
VI_TERMS = {  # vài thuật ngữ tiếng Việt thường gặp -> từ khoá tiếng Anh trong tài liệu
    "độ trễ": "delay", "xung nhịp": "clock", "cây clock": "clock tree", "công suất": "power",
    "diện tích": "area", "tắc nghẽn": "congestion", "đệm": "buffer", "độ lệch": "skew",
    "mật độ": "density", "đi dây": "routing", "định tuyến": "routing", "đặt cell": "placement",
    "rò rỉ": "leakage", "sụt áp": "ir drop", "nhiễu": "noise crosstalk", "chân": "pin",
    "bất định": "uncertainty", "vi phạm": "violation", "tối ưu": "optimization",
}


# ----------------------------------------------------------------------------- config
def _bridge_home() -> Path:
    if os.environ.get("PD_BRIDGE_HOME"):
        return Path(os.environ["PD_BRIDGE_HOME"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PD_Bridge"
    return Path.home() / ".local" / "share" / "PD_Bridge"


def default_paths(data_dir: str | None = None, db: str | None = None) -> tuple[Path | None, Path | None]:
    cfg = {}
    try:
        cfg = json.loads((_bridge_home() / "config.json").read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    d = data_dir or os.environ.get("PD_DATA_DIR") or cfg.get("data_dir")
    dp = Path(d) if d else None
    if db:
        return dp, Path(db)
    idx = os.environ.get("PD_INDEX_DB") or cfg.get("index_db")
    if idx:
        return dp, Path(idx)
    if dp:
        return dp, dp / ".pd_index" / "index.db"
    return None, None


# ----------------------------------------------------------------------------- text utils
def strip_accents(s: str) -> str:
    s = s.replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def has_vi_diacritics(tok: str) -> bool:
    return strip_accents(tok) != tok


def extract_terms(query: str, limit: int = 14) -> list[str]:
    """Rút từ khoá kỹ thuật từ câu hỏi (bỏ từ tiếng Việt, stopword)."""
    q = query
    extra = []
    low = q.lower()
    for vi, en in VI_TERMS.items():
        if vi in low:
            extra.extend(en.split())
    terms: list[str] = []
    for m in re.finditer(r"[A-Za-z0-9_À-ỹ][A-Za-z0-9_.\-/À-ỹ]*", q):
        tok = m.group(0).strip(".-/")
        if not tok:
            continue
        if has_vi_diacritics(tok):
            continue
        parts = [tok]
        if "/" in tok or "-" in tok:
            parts = [p for p in re.split(r"[/\-]", tok) if p]
        for p in parts:
            lp = p.lower()
            if lp.endswith((".tcl", ".sdc", ".rpt", ".log", ".v", ".pdf", ".md")):
                terms.append(lp)          # tên file: giữ nguyên để khớp đường dẫn
                lp = lp.rsplit(".", 1)[0]
            lp = lp.strip(".")
            if len(lp) < 2 or lp.isdigit() and len(lp) < 3:
                continue
            if lp in EN_STOP or lp in VI_STOP:
                continue
            terms.append(lp)
    terms.extend(extra)
    out, seen = [], set()
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def decode_bytes(b: bytes) -> str:
    if b.startswith(b"\xef\xbb\xbf"):
        b = b[3:]
    if b.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return b.decode("utf-16")
        except Exception:
            pass
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", errors="replace")


def looks_binary(b: bytes) -> bool:
    if not b:
        return False
    if b"\x00" in b[:4096] and not b.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    return False


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "pre", "table", "dt", "dd"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in ("td", "th"):
            self.parts.append("\t")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        self.parts.append(data)


def html_to_text(raw: str) -> tuple[str, str]:
    p = _HTMLText()
    try:
        p.feed(raw)
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw), ""
    txt = "".join(p.parts)
    txt = re.sub(r"[ \t\r\f\v]+\n", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt, p.title.strip()


def xml_text(raw: bytes) -> str:
    s = decode_bytes(raw)
    s = re.sub(r"</w:p>|</a:p>|<w:br/>|</row>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(re.sub(r"[ \t]+", " ", s))


def office_text(path: Path) -> str:
    out = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if path.suffix.lower() == ".docx":
            sel = [n for n in names if n.startswith("word/") and n.endswith(".xml")
                   and ("document" in n or "footnotes" in n)]
        elif path.suffix.lower() == ".pptx":
            sel = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                         key=lambda n: int(re.findall(r"\d+", n)[-1]))
        else:  # xlsx
            sel = [n for n in names if n == "xl/sharedStrings.xml"]
        for n in sel:
            out.append(xml_text(z.read(n)))
    return "\n".join(out)


def pdf_pages(path: Path) -> list[str]:
    """Trả về list text theo trang. Thử PyMuPDF -> pypdf -> pdftotext."""
    try:
        import fitz  # type: ignore
        with fitz.open(str(path)) as doc:
            return [p.get_text() for p in doc]
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001  (thư viện hỏng có thể ném PanicException)
        pass
    try:
        from pypdf import PdfReader  # type: ignore
        import logging
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        r = PdfReader(str(path))
        pages = []
        for p in r.pages:
            try:
                pages.append(p.extract_text() or "")
            except Exception:
                pages.append("")
        return pages
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        pass
    exe = shutil.which("pdftotext")
    if exe:
        try:
            res = subprocess.run([exe, "-layout", str(path), "-"], capture_output=True, timeout=900)
            return decode_bytes(res.stdout).split("\f")
        except Exception:
            pass
    raise RuntimeError("không có thư viện đọc PDF (cài: pip install pypdf)")


def chunk_lines(text: str, chunk_chars: int = CHUNK_CHARS) -> list[tuple[str, str]]:
    lines = text.splitlines()
    chunks, buf, size, start = [], [], 0, 1
    for i, line in enumerate(lines, 1):
        buf.append(line)
        size += len(line) + 1
        if size >= chunk_chars:
            chunks.append((f"L{start}-{i}", "\n".join(buf)))
            buf, size, start = [], 0, i + 1
    if buf:
        chunks.append((f"L{start}-{len(lines)}", "\n".join(buf)))
    return chunks


# ----------------------------------------------------------------------------- classify
def classify(path: Path, meta_only_dir: bool) -> str:
    name = path.name.lower()
    ext = path.suffix.lower()
    if name.endswith((".rpt.gz", ".log.gz", ".txt.gz", ".tcl.gz")):
        inner = Path(name[:-3]).suffix
        return {".rpt": "report", ".log": "log", ".txt": "doc", ".tcl": "script"}[inner]
    if meta_only_dir:
        return "data"
    if name in NAME_KIND:
        return NAME_KIND[name]
    if ext in DOC_EXT:
        return "doc"
    if ext in SCRIPT_EXT:
        return "script"
    if ext in REPORT_EXT:
        return "report"
    if ext in LOG_EXT:
        return "log"
    if ext in DATA_EXT:
        return "data"
    if re.search(r"(timing|report|summary|qor|drc|power|area|skew|clock|violat)", name):
        return "report"
    return "other"


def extract(path: Path, kind: str, size: int) -> tuple[list[tuple[str, str]], str, int]:
    """-> (chunks[(loc, text)], title, flag) ; flag 1=đầy đủ, 2=bị cắt."""
    name = path.name.lower()
    ext = path.suffix.lower()
    flag = 1
    if ext == ".pdf":
        if size > MAX_PDF_BYTES:
            return [], "", 0
        pages = pdf_pages(path)
        chunks = [(f"p.{i}", t) for i, t in enumerate(pages, 1) if t and t.strip()]
        return chunks, path.stem, 1
    if ext in (".docx", ".pptx", ".xlsx"):
        return chunk_lines(office_text(path)), path.stem, 1
    if name.endswith(".gz"):
        with gzip.open(path, "rb") as f:
            raw = f.read(MAX_TEXT_BYTES + 1)
        if len(raw) > MAX_TEXT_BYTES:
            raw, flag = raw[:HEAD_BYTES_BIG], 2
        return chunk_lines(decode_bytes(raw)), "", flag
    with open(path, "rb") as f:
        if size > MAX_TEXT_BYTES:
            raw, flag = f.read(HEAD_BYTES_BIG), 2
        else:
            raw = f.read()
    if looks_binary(raw):
        return [], "", 0
    text = decode_bytes(raw)
    title = ""
    if ext in (".html", ".htm"):
        text, title = html_to_text(text)
    elif ext == ".md":
        m = re.search(r"^#\s+(.+)$", text, re.M)
        title = m.group(1).strip() if m else ""
    return chunk_lines(text), title, flag


# ----------------------------------------------------------------------------- db
def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db), timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY, path TEXT UNIQUE, ext TEXT, kind TEXT,
            size INTEGER, mtime REAL, indexed INTEGER, nchunks INTEGER, title TEXT, err TEXT);
        """
    )
    try:
        con.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
            "path, title, body, file_id UNINDEXED, loc UNINDEXED, kind UNINDEXED, "
            "tokenize=\"unicode61 remove_diacritics 2 tokenchars '_'\")"
        )
        con.execute("INSERT OR REPLACE INTO meta VALUES('fts','1')")
    except sqlite3.OperationalError:
        con.execute("CREATE TABLE IF NOT EXISTS chunks(path, title, body, file_id, loc, kind)")
        con.execute("INSERT OR REPLACE INTO meta VALUES('fts','0')")
    return con


def has_fts(con) -> bool:
    r = con.execute("SELECT value FROM meta WHERE key='fts'").fetchone()
    return bool(r and r[0] == "1")


def walk(data_dir: Path):
    for root, dirs, files in os.walk(data_dir):
        rp = Path(root)
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith("~$")]
        rel_parts = [p.lower() for p in rp.relative_to(data_dir).parts]
        meta_only = any(p.endswith(META_ONLY_DIR_SUFFIX) for p in rel_parts)
        for fn in files:
            if fn.startswith(("~$", ".~lock")) or fn.lower() in ("desktop.ini", "thumbs.db"):
                continue
            yield rp / fn, meta_only


def build(data_dir: Path, db: Path, full: bool = False, verbose: bool = False) -> dict:
    t0 = time.time()
    con = connect(db)
    if full or con.execute("SELECT value FROM meta WHERE key='schema'").fetchone() != (SCHEMA_VERSION,):
        con.execute("DELETE FROM files")
        con.execute("DELETE FROM chunks")
        con.execute("INSERT OR REPLACE INTO meta VALUES('schema',?)", (SCHEMA_VERSION,))
        con.commit()
    known = {r[0]: (r[1], r[2], r[3], r[4]) for r in con.execute("SELECT path, id, size, mtime, err FROM files")}
    seen = set()
    stats = {"files": 0, "new_or_changed": 0, "fulltext": 0, "removed": 0, "errors": 0}
    for path, meta_only in walk(data_dir):
        try:
            st = path.stat()
        except OSError:
            continue
        rel = path.relative_to(data_dir).as_posix()
        seen.add(rel)
        stats["files"] += 1
        old = known.get(rel)
        if old and old[1] == st.st_size and abs((old[2] or 0) - st.st_mtime) < 1e-3 and not old[3]:
            continue      # không đổi (file lỗi lần trước thì đọc lại)
        stats["new_or_changed"] += 1
        kind = classify(path, meta_only)
        if old:
            con.execute("DELETE FROM chunks WHERE file_id=?", (old[0],))
            con.execute("DELETE FROM files WHERE id=?", (old[0],))
        chunks, title, flag, err = [], "", 0, None
        want_text = kind in ("doc", "script", "report", "log") or (
            kind == "other" and st.st_size <= 2 * 1024 * 1024)
        if want_text and st.st_size > 0:
            try:
                if kind == "other":
                    with open(path, "rb") as f:
                        if looks_binary(f.read(4096)):
                            raise ValueError("binary")
                chunks, title, flag = extract(path, kind, st.st_size)
                if kind == "other" and chunks:
                    kind = "doc" if path.suffix.lower() in ("", ".readme") else "other"
            except ValueError:
                pass
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:  # noqa: BLE001
                err = str(e)[:200]
                stats["errors"] += 1
                if verbose:
                    print(f"  ! {rel}: {err}", file=sys.stderr)
        cur = con.execute(
            "INSERT INTO files(path, ext, kind, size, mtime, indexed, nchunks, title, err) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (rel, path.suffix.lower(), kind, st.st_size, st.st_mtime, flag, len(chunks), title, err))
        fid = cur.lastrowid
        if chunks:
            stats["fulltext"] += 1
            con.executemany(
                "INSERT INTO chunks(path, title, body, file_id, loc, kind) VALUES(?,?,?,?,?,?)",
                [(rel, title, body, fid, loc, kind) for loc, body in chunks])
        else:
            # vẫn cho phép tìm theo đường dẫn
            con.execute("INSERT INTO chunks(path, title, body, file_id, loc, kind) VALUES(?,?,?,?,?,?)",
                        (rel, title, "", fid, "", kind))
        if verbose:
            print(f"  + [{kind}] {rel} ({len(chunks)} đoạn)")
        if stats["new_or_changed"] % 50 == 0:
            con.commit()
    for rel, (fid, _, _, _) in known.items():
        if rel not in seen:
            con.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
            con.execute("DELETE FROM files WHERE id=?", (fid,))
            stats["removed"] += 1
    con.execute("INSERT OR REPLACE INTO meta VALUES('data_dir',?)", (str(data_dir),))
    con.execute("INSERT OR REPLACE INTO meta VALUES('built_at',?)", (time.strftime("%Y-%m-%d %H:%M:%S"),))
    con.commit()
    if has_fts(con) and stats["new_or_changed"] > 200:
        con.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
        con.commit()
    total_ft = con.execute("SELECT COUNT(*) FROM files WHERE nchunks>0").fetchone()[0]
    con.close()
    stats["fulltext_total"] = total_ft
    stats["seconds"] = round(time.time() - t0, 1)
    return stats


def _fts_quote(t: str) -> str:
    return '"' + t.replace('"', '""') + '"'


def search(db: Path, query: str, kind: str | None = None, n: int = 15, all_terms: bool = False) -> list[dict]:
    if not db.exists():
        raise SystemExit(f"Chưa có chỉ mục: {db}. Chạy: pd_index.py build")
    con = sqlite3.connect(str(db), timeout=30)
    terms = extract_terms(query) or [w for w in re.findall(r"\w+", query) if len(w) > 1][:8]
    if not terms:
        return []
    hits: dict[int, dict] = {}
    kind_sql = " AND kind=?" if kind else ""
    kind_arg = (kind,) if kind else ()
    if has_fts(con):
        fts_terms = []
        for t in terms:
            words = re.findall(r"[\w]+", t)
            if not words:
                continue
            fts_terms.append(_fts_quote(" ".join(words)))
        joiner = " AND " if all_terms else " OR "
        q = joiner.join(fts_terms)
        sql = ("SELECT file_id, path, loc, kind, bm25(chunks, 6.0, 3.0, 1.0) AS s, "
               "snippet(chunks, 2, '«', '»', ' … ', 14) FROM chunks WHERE chunks MATCH ?"
               + kind_sql + " ORDER BY s LIMIT 400")
        try:
            rows = con.execute(sql, (q, *kind_arg)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for fid, path, loc, k, score, snip in rows:
            h = hits.get(fid)
            if h is None:
                hits[fid] = {"path": path, "kind": k, "score": score, "locs": [loc] if loc else [],
                             "snippet": re.sub(r"\s+", " ", snip or "").strip()}
            else:
                if loc and len(h["locs"]) < 4 and loc not in h["locs"]:
                    h["locs"].append(loc)
                h["score"] += score * 0.15  # nhiều đoạn khớp -> tăng nhẹ
    else:  # không có FTS5: LIKE chậm nhưng vẫn chạy
        for t in terms:
            for fid, path, loc, k, body in con.execute(
                    "SELECT file_id, path, loc, kind, body FROM chunks WHERE (body LIKE ? OR path LIKE ?)"
                    + kind_sql + " LIMIT 300", (f"%{t}%", f"%{t}%", *kind_arg)):
                h = hits.setdefault(fid, {"path": path, "kind": k, "score": 0.0, "locs": [], "snippet": ""})
                h["score"] -= 1
                if loc and len(h["locs"]) < 4 and loc not in h["locs"]:
                    h["locs"].append(loc)
                if not h["snippet"] and body:
                    i = body.lower().find(t.lower())
                    h["snippet"] = re.sub(r"\s+", " ", body[max(0, i - 60): i + 100])
    # khớp tên file/đường dẫn: tăng điểm mạnh
    for t in terms:
        if len(t) < 3:
            continue
        for fid, path, k in con.execute("SELECT id, path, kind FROM files WHERE lower(path) LIKE ?"
                                        + kind_sql + " LIMIT 50", (f"%{t.lower()}%", *kind_arg)):
            h = hits.setdefault(fid, {"path": path, "kind": k, "score": 0.0, "locs": [], "snippet": ""})
            base = Path(path).name.lower()
            h["score"] -= 8.0 if t.lower() in base else 3.0
    con.close()
    res = sorted(hits.values(), key=lambda h: h["score"])[:n]
    for h in res:
        h["score"] = round(h["score"], 2)
    return res


def show(db: Path, data_dir: Path | None, target: str, page: int | None, chunk: int | None,
         lines: str | None, grep: str | None, ctx: int, max_lines: int) -> str:
    rel = target.replace("\\", "/")
    full = None
    if data_dir and not Path(target).is_absolute():
        full = data_dir / rel
    elif Path(target).is_absolute():
        full = Path(target)
        if data_dir:
            try:
                rel = full.relative_to(data_dir).as_posix()
            except ValueError:
                pass
    out: list[str] = []
    if grep:
        if not full or not full.exists():
            return f"Không thấy file: {full}"
        opener = gzip.open if full.name.endswith(".gz") else open
        rx = re.compile(grep, re.I) if any(c in grep for c in ".*[]()|\\+?") else None
        needle = grep.lower()
        buf: list[str] = []
        after = 0
        nmatch = 0
        with opener(full, "rb") as f:
            for i, raw in enumerate(f, 1):
                line = decode_bytes(raw).rstrip("\r\n")
                hit = (rx.search(line) if rx else needle in line.lower())
                if hit:
                    nmatch += 1
                    out.extend(buf)
                    buf = []
                    out.append(f"{i}: {line}")
                    after = ctx
                elif after > 0:
                    out.append(f"{i}- {line}")
                    after -= 1
                else:
                    buf.append(f"{i}- {line}")
                    buf = buf[-ctx:] if ctx else []
                if len(out) >= max_lines:
                    out.append(f"... (dừng ở {max_lines} dòng; thu hẹp --grep)")
                    break
        return "\n".join(out) if out else f"Không có dòng khớp '{grep}' trong {rel}"
    if lines:
        a, _, b = lines.partition("-")
        a, b = int(a), int(b or a)
        if not full or not full.exists():
            return f"Không thấy file: {full}"
        opener = gzip.open if full.name.endswith(".gz") else open
        with opener(full, "rb") as f:
            for i, raw in enumerate(f, 1):
                if i > b or len(out) >= max_lines:
                    break
                if i >= a:
                    out.append(f"{i}: {decode_bytes(raw).rstrip()}")
        return "\n".join(out)
    if db.exists():
        con = sqlite3.connect(str(db), timeout=30)
        rows = con.execute("SELECT loc, body FROM chunks WHERE path=? ORDER BY rowid", (rel,)).fetchall()
        con.close()
        if rows:
            if page is not None:
                rows = [r for r in rows if r[0] == f"p.{page}"]
            elif chunk is not None:
                rows = rows[chunk - 1: chunk] if 0 < chunk <= len(rows) else []
            else:
                rows = rows[:1]
                out.append(f"(đoạn 1/{len(rows)} — dùng --chunk N hoặc --page N để xem tiếp)")
            for loc, body in rows:
                out.append(f"--- {rel} [{loc}]")
                bl = body.splitlines()
                out.extend(bl[:max_lines])
                if len(bl) > max_lines:
                    out.append(f"... (còn {len(bl) - max_lines} dòng)")
            return "\n".join(out) if out else "Không có đoạn/trang đó."
    if full and full.exists():
        return show(db, data_dir, str(full), None, None, f"1-{max_lines}", None, 0, max_lines)
    return f"Không thấy trong chỉ mục: {rel}"


def stats(db: Path) -> dict:
    con = sqlite3.connect(str(db), timeout=30)
    d = {k: v for k, v in con.execute("SELECT key, value FROM meta")}
    d["files"] = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    d["fulltext"] = con.execute("SELECT COUNT(*) FROM files WHERE nchunks>0").fetchone()[0]
    d["by_kind"] = dict(con.execute("SELECT kind, COUNT(*) FROM files GROUP BY kind").fetchall())
    d["errors"] = con.execute("SELECT COUNT(*) FROM files WHERE err IS NOT NULL").fetchone()[0]
    con.close()
    return d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Chỉ mục thư mục physical design")
    ap.add_argument("--data-dir")
    ap.add_argument("--db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--full", action="store_true")
    b.add_argument("-v", "--verbose", action="store_true")
    s = sub.add_parser("search")
    s.add_argument("query", nargs="+")
    s.add_argument("--kind", choices=["doc", "script", "report", "log", "data", "other"])
    s.add_argument("-n", type=int, default=15)
    s.add_argument("--all", action="store_true", help="bắt buộc khớp mọi từ khoá")
    s.add_argument("--json", action="store_true")
    sh = sub.add_parser("show")
    sh.add_argument("path")
    sh.add_argument("--page", type=int)
    sh.add_argument("--chunk", type=int)
    sh.add_argument("--lines")
    sh.add_argument("--grep")
    sh.add_argument("-C", type=int, default=1)
    sh.add_argument("--max", type=int, default=120)
    sub.add_parser("stats")
    a = ap.parse_args(argv)
    data_dir, db = default_paths(a.data_dir, a.db)
    if db is None:
        print("Thiếu --data-dir (hoặc data_dir trong config.json)", file=sys.stderr)
        return 2
    if a.cmd == "build":
        if not data_dir or not data_dir.is_dir():
            print(f"Không thấy thư mục dữ liệu: {data_dir}", file=sys.stderr)
            return 2
        try:
            st = build(data_dir, db, a.full, a.verbose)
        except sqlite3.OperationalError as e:
            if "readonly" in str(e).lower() or "unable to open" in str(e).lower():
                alt = _bridge_home() / ".pd_index" / "index.db"
                print(f"Không ghi được {db} ({e}); dùng {alt}", file=sys.stderr)
                st = build(data_dir, alt, a.full, a.verbose)
            else:
                raise
        print(json.dumps(st, ensure_ascii=False))
        return 0
    if a.cmd == "search":
        res = search(db, " ".join(a.query), a.kind, a.n, a.all)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        elif not res:
            print("Không có kết quả. Thử từ khoá tiếng Anh khác/ngắn hơn.")
        else:
            for i, h in enumerate(res, 1):
                loc = (" [" + ", ".join(h["locs"]) + "]") if h["locs"] else ""
                print(f"{i:2}. [{h['kind']}] {h['path']}{loc}")
                if h["snippet"]:
                    print(f"      {h['snippet'][:220]}")
        return 0
    if a.cmd == "show":
        print(show(db, data_dir, a.path, a.page, a.chunk, a.lines, a.grep, a.C, a.max))
        return 0
    if a.cmd == "stats":
        print(json.dumps(stats(db), ensure_ascii=False, indent=1))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
