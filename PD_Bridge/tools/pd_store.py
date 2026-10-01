#!/usr/bin/env python3
"""pd_store.py - kho project trên MÁY NGOÀI (không đồng bộ OneDrive) + sổ tay + kiểm chứng lệnh. 0 token.

Kho (mặc định %USERPROFILE%\\PD_Bridge_Kho):
  du_an/<project>/
    hoi_dap/        Qnnn_<tên>.md (câu hỏi), Qnnn_traloi.md (bản mới nhất), Qnnn_traloi_v1.md… (bản cũ)
    du_lieu/        Qnnn_<tên>/ file người hỏi gửi kèm
    BOI_CANH.md     bối cảnh project (sửa tay; Claude đọc ở mọi câu hỏi của project)
    KIEN_THUC.md    kiến thức của project; phần ◻️ "chưa xác nhận" do script tự thêm sau 14 ngày
    MUC_LUC.md      bảng mọi câu hỏi (tự sinh)
    DONG_THOI_GIAN.md  chỉ số chính mỗi lần gửi report/log (tự sinh)
  .chi_muc.db       chỉ mục toàn văn toàn bộ kho
Sổ tay (trên OneDrive để đọc học): <PD_Bridge>/so_tay/so_lenh.md, thuat_ngu.md
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sqlite3
import unicodedata
from pathlib import Path

import pd_index
import pd_summarize

AUTO_START = "<!-- tu_dong:bat_dau -->"
AUTO_END = "<!-- tu_dong:ket_thuc -->"
DEFAULT_PROJECT = "chung"

TCL_BUILTINS = set("""set unset if else elseif for foreach while proc return puts source expr incr lappend
list lindex llength lsort lsearch lrange concat join split string regexp regsub array dict append
format global upvar uplevel catch error eval info file open close gets read exec after switch break
continue namespace variable package exit cd pwd glob clock rename subst trace interp scan binary
lassign lreplace linsert lset apply try throw tailcall yield echo setenv getenv redirect""".split())


def slug(name: str) -> str:
    s = pd_index.strip_accents(name).strip().lower()
    s = re.sub(r"[^a-z0-9_.\-]+", "_", s).strip("_.")
    return s or DEFAULT_PROJECT


def _read(p: Path) -> str:
    return pd_index.decode_bytes(p.read_bytes())


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name("." + p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(p)


class Store:
    def __init__(self, root: Path, data_dir: Path | None = None):
        self.root = Path(root)
        self.data_dir = data_dir
        self.du_an = self.root / "du_an"
        self.du_an.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ project
    def projects(self) -> list[str]:
        return sorted(p.name for p in self.du_an.iterdir() if p.is_dir() and not p.name.startswith("."))

    def block_names(self) -> list[str]:
        """Tên block/project đã biết: project trong kho + thư mục cấp 1-2 của thư mục dữ liệu."""
        names = set(self.projects())
        if self.data_dir and self.data_dir.is_dir():
            try:
                for p in self.data_dir.iterdir():
                    if p.is_dir() and not p.name.startswith("."):
                        names.add(p.name)
            except OSError:
                pass
        names.discard(DEFAULT_PROJECT)
        return sorted((n for n in names if len(n) >= 3), key=len, reverse=True)

    def detect(self, text: str, filename: str = "") -> str:
        """du_an: <tên> trong nội dung > @tên trong tên file > tên block xuất hiện trong câu hỏi > 'chung'."""
        m = re.search(r"^\s*(?:du_an|dự án|du an|project)\s*[:=]\s*(\S+)", text[:600], re.I | re.M)
        if m:
            return slug(m.group(1))
        m = re.search(r"@([\w.\-]+)", filename)
        if m:
            return slug(m.group(1))
        hay = (filename + " " + text).lower()
        for name in self.block_names():
            if re.search(r"(?<![\w])" + re.escape(name.lower()) + r"(?![\w])", hay):
                return slug(name)
        return DEFAULT_PROJECT

    def pdir(self, proj: str) -> Path:
        d = self.du_an / slug(proj)
        if not d.exists():
            (d / "hoi_dap").mkdir(parents=True, exist_ok=True)
            (d / "du_lieu").mkdir(parents=True, exist_ok=True)
            hint = ""
            if self.data_dir and (self.data_dir / proj).is_dir():
                hint = f"- Dữ liệu trong physical design: `{self.data_dir / proj}`\n"
            _write(d / "BOI_CANH.md", f"# Bối cảnh project {proj}\n\nSửa tay: block, flow, phiên bản tool, "
                                      f"đường dẫn run, mục tiêu timing/power… Claude đọc file này ở mọi câu hỏi "
                                      f"của project.\n\n{hint}")
            _write(d / "KIEN_THUC.md", f"# Kiến thức project {proj}\n\nGhi tay các điều đã chắc chắn (✅).\n\n"
                                       f"## ◻️ Chưa xác nhận (tự động sau 14 ngày, chưa bị đánh giá sai)\n"
                                       f"{AUTO_START}\n{AUTO_END}\n")
        return d

    def du_lieu_dir(self, proj: str, qid: str, stem: str) -> Path:
        safe = re.sub(r'[<>:"/\\|?*]', "_", stem)
        d = self.pdir(proj) / "du_lieu" / f"{qid}_{safe}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def boi_canh(self, proj: str, limit: int = 2000) -> str:
        f = self.pdir(proj) / "BOI_CANH.md"
        try:
            t = _read(f).strip()
        except OSError:
            return ""
        return t if len(t) <= limit else t[:limit] + f"\n… (đọc tiếp: {f})"

    # ------------------------------------------------------------------ hỏi đáp (lưu vĩnh viễn)
    def archive(self, proj: str, qid: str, stem: str, question_raw: str, answer_text: str, v: int) -> Path:
        hd = self.pdir(proj) / "hoi_dap"
        hd.mkdir(parents=True, exist_ok=True)
        _write(hd / f"{qid}_{stem}.md", question_raw)
        a = hd / f"{qid}_traloi.md"
        if a.exists() and v > 1:
            old = _read(a)
            m = re.search(r"pd_bridge q=\S+ v=(\d+)", old)
            ov = int(m.group(1)) if m else v - 1
            if ov != v:
                _write(hd / f"{qid}_traloi_v{ov}.md", old)
        _write(a, answer_text)
        return a

    def answer_path(self, proj: str, qid: str) -> Path | None:
        p = self.du_an / slug(proj) / "hoi_dap" / f"{qid}_traloi.md"
        return p if p.exists() else None

    def render_muc_luc(self, proj: str, journal: list[dict]) -> None:
        latest: dict[str, dict] = {}
        ratings: dict[str, str] = {}
        for r in journal:
            if r.get("type") in ("answer",) and r.get("proj", DEFAULT_PROJECT) == proj:
                latest[r["q"]] = r
            elif r.get("type") == "rating":
                ratings[f"{r['q']}:{r['v']}"] = r.get("danh_gia", "chua")
        icon = {"dung": "✅", "mot_phan": "◑", "sai": "❌"}
        rows = ["| Q | Ngày | Chủ đề | Mode | Đánh giá | Kết luận | File |", "|---|---|---|---|---|---|---|"]
        for q in sorted(latest, key=lambda x: int(x[1:]), reverse=True):
            r = latest[q]
            rt = ratings.get(f"{q}:{r['v']}", "chua")
            concl = re.sub(r"\s+", " ", re.sub(r"[#*`|>-]", " ", r.get("summary", "")))[:140]
            rows.append(f"| {q} | {r.get('ts', '')[:10]} | {r.get('name', '').replace('|', '/')} | {r.get('mode', '')} "
                        f"| {icon.get(rt, '·')} {rt} | {concl} | hoi_dap/{q}_traloi.md |")
        _write(self.pdir(proj) / "MUC_LUC.md",
               f"# Mục lục project {proj}\n\nTự sinh (0 token). {len(latest)} câu hỏi, mới nhất ở trên.\n\n"
               + "\n".join(rows) + "\n")

    def add_timeline(self, proj: str, qid: str, files: list[Path]) -> None:
        f = self.pdir(proj) / "DONG_THOI_GIAN.md"
        if f.exists() and f"| {qid} |" in _read(f):
            return
        lines = []
        for p in files:
            if not p.is_file():
                continue
            try:
                info = pd_summarize.one_line(p) if not pd_index.looks_binary(p.read_bytes()[:4096]) else "nhị phân"
            except Exception:  # noqa: BLE001
                info = "-"
            lines.append(f"| {dt.date.today().isoformat()} | {qid} | {p.name} | {info[:150].replace('|', '/')} |")
        if not lines:
            return
        if not f.exists():
            _write(f, f"# Dòng thời gian project {proj}\n\nChỉ số chính mỗi lần gửi report/log (tự sinh, 0 token).\n\n"
                      "| Ngày | Q | File | Chỉ số |\n|---|---|---|---|\n")
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    # ------------------------------------------------------------------ chỉ mục kho
    def db(self) -> Path:
        return self.root / ".chi_muc.db"

    def reindex(self) -> None:
        pd_index.build(self.du_an, self.db())

    def search(self, query: str, proj: str | None, exclude: list[str] | None = None, n: int = 6) -> list[str]:
        if not self.db().exists():
            return []
        try:
            hits = pd_index.search(self.db(), query, n=n * 3 + 6)
        except BaseException:  # noqa: BLE001
            return []
        ex = [e.replace("\\", "/") for e in (exclude or [])]
        scored = []
        for h in hits:
            path = h["path"]
            if any(e and e in path for e in ex) or path.endswith(("MUC_LUC.md", "DONG_THOI_GIAN.md")):
                continue
            same = proj and path.startswith(slug(proj) + "/")
            scored.append((h["score"] - (5 if same else 0), h))
        out = []
        for _, h in sorted(scored, key=lambda x: x[0])[:n]:
            loc = (" [" + ", ".join(h["locs"][:2]) + "]") if h["locs"] else ""
            snip = (" — " + h["snippet"][:140]) if h["snippet"] else ""
            out.append(f"- kho/du_an/{h['path']}{loc}{snip}")
        return out

    # ------------------------------------------------------------------ kiến thức project + tự học 14 ngày
    def kien_thuc_files(self, proj: str | None) -> list[Path]:
        fs = []
        if proj:
            fs.append(self.pdir(proj) / "KIEN_THUC.md")
        fs += [d / "KIEN_THUC.md" for d in self.du_an.iterdir()
               if d.is_dir() and (not proj or d.name != slug(proj)) and (d / "KIEN_THUC.md").exists()]
        return [f for f in fs if f.exists()]

    def set_auto_learned(self, proj: str, entries: list[str]) -> None:
        f = self.pdir(proj) / "KIEN_THUC.md"
        t = _read(f)
        block = AUTO_START + "\n" + "\n".join(entries) + ("\n" if entries else "") + AUTO_END
        if AUTO_START in t and AUTO_END in t:
            t = re.sub(re.escape(AUTO_START) + r".*?" + re.escape(AUTO_END), lambda _: block, t, flags=re.S)
        else:
            t += f"\n## ◻️ Chưa xác nhận (tự động sau 14 ngày, chưa bị đánh giá sai)\n{block}\n"
        _write(f, t)

    def auto_learn(self, journal: list[dict], now_t: float, days: int = 14) -> int:
        """Câu trả lời ≥14 ngày, chưa bị đánh giá 'sai' -> mục ◻️ chưa xác nhận trong KIEN_THUC.md của project."""
        latest: dict[str, dict] = {}
        ratings: dict[str, str] = {}
        for r in journal:
            if r.get("type") == "answer" and r.get("status") == "done":
                latest[r["q"]] = r
            elif r.get("type") == "rating":
                ratings[f"{r['q']}:{r['v']}"] = r.get("danh_gia", "chua")
        per: dict[str, list[str]] = {}
        for q in sorted(latest, key=lambda x: int(x[1:])):
            r = latest[q]
            rt = ratings.get(f"{q}:{r['v']}", "chua")
            if rt in ("sai", "dung") or now_t - float(r.get("t", now_t)) < days * 86400:
                continue          # sai: bỏ; dung: đã thành ✅ qua tổng hợp tuần
            s = re.sub(r"\s+", " ", re.sub(r"^[-*]\s*", "", r.get("summary", ""), flags=re.M)).strip()[:260]
            per.setdefault(r.get("proj", DEFAULT_PROJECT), []).append(
                f"- ◻️ ({q}, {r.get('ts', '')[:10]}{', một phần' if rt == 'mot_phan' else ''}) {r.get('name', '')}: {s}")
        n = 0
        for proj in set(per) | {p for p in self.projects()}:
            entries = per.get(proj, [])
            f = self.du_an / proj / "KIEN_THUC.md"
            if not entries and not f.exists():
                continue
            self.set_auto_learned(proj, entries)
            n += len(entries)
        return n


# ============================================================================ bài học / sổ tay
def lesson_items(answer: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Lấy thuật ngữ + lệnh từ mục '## Bài học' (### Thuật ngữ / ### Lệnh)."""
    m = re.search(r"^##\s*Bài học.*?$(.*?)(?=^##\s|\Z)", answer, re.M | re.S | re.I)
    if not m:
        return [], []
    sec = m.group(1)
    terms, cmds = [], []

    def sub(title: str) -> str:
        mm = re.search(r"^###\s*" + title + r".*?$(.*?)(?=^###\s|\Z)", sec, re.M | re.S | re.I)
        return mm.group(1) if mm else ""
    for line in sub("Thuật ngữ").splitlines():
        mm = re.match(r"^\s*[-*]\s*\*\*(.+?)\*\*\s*(?:\(([^)]*)\))?\s*[:—–-]\s*(.+)$", line)
        if mm:
            t = mm.group(1).strip()
            terms.append((t + (f" ({mm.group(2).strip()})" if mm.group(2) else ""), mm.group(3).strip()))
    for line in sub("Lệnh").splitlines():
        mm = re.match(r"^\s*[-*]\s*`([^`]+)`\s*[:—–-]?\s*(.*)$", line)
        if mm:
            cmds.append((mm.group(1).strip(), mm.group(2).strip()))
    return terms, cmds


class Notebook:
    """Sổ tay lệnh + thuật ngữ: dữ liệu json trong kho, bản đọc .md trên OneDrive."""

    def __init__(self, data_file: Path, out_dir: Path):
        self.data_file = data_file
        self.out_dir = out_dir
        try:
            self.d = json.loads(data_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self.d = {"terms": {}, "cmds": {}}

    @staticmethod
    def key(s: str) -> str:
        return unicodedata.normalize("NFC", re.sub(r"\s+", " ", s.split("(")[0].strip().lower()))

    def add(self, qid: str, terms, cmds, verified: dict[str, bool] | None = None) -> int:
        n = 0
        for t, desc in terms:
            e = self.d["terms"].setdefault(self.key(t), {"term": t, "desc": desc, "q": []})
            if qid not in e["q"]:
                e["q"].append(qid)
                n += 1
            if len(desc) > len(e.get("desc", "")):
                e["desc"] = desc
        for c, desc in cmds:
            k = self.key(c)
            e = self.d["cmds"].setdefault(k, {"cmd": c, "desc": desc, "q": []})
            if qid not in e["q"]:
                e["q"].append(qid)
                n += 1
            if desc and len(desc) > len(e.get("desc", "")):
                e["desc"] = desc
            if verified is not None:
                name = c.split()[0]
                if name in verified:
                    e["doc"] = verified[name]
        return n

    def render(self, ratings: dict[str, str]) -> None:
        """ratings: Qnnn -> dung/mot_phan/sai/chua (bản mới nhất)."""
        def status(qs):
            rs = [ratings.get(q, "chua") for q in qs]
            if any(r == "dung" for r in rs):
                return "✅"
            if rs and all(r == "sai" for r in rs):
                return None
            return "◻️"
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.data_file.write_text(json.dumps(self.d, ensure_ascii=False, indent=1), encoding="utf-8")
        out = ["# Sổ tay lệnh", "", "Tự gom từ mục **Bài học** của các câu trả lời (0 token). "
               "✅ = câu trả lời nguồn đã được bạn xác nhận đúng · ◻️ = chưa xác nhận · "
               "📘 = có trong tài liệu tool trên máy · ⚠️ = chưa thấy trong tài liệu tool (cần `help <lệnh>`).", ""]
        groups: dict[str, list] = {}
        for k, e in sorted(self.d["cmds"].items()):
            st = status(e["q"])
            if st is None:
                continue
            g = re.split(r"[_\s]", e["cmd"])[0].lower()
            groups.setdefault(g, []).append((st, e))
        for g in sorted(groups):
            out.append(f"## {g}")
            for st, e in groups[g]:
                doc = {True: " 📘", False: " ⚠️"}.get(e.get("doc"), "")
                out.append(f"- {st}{doc} `{e['cmd']}` — {e.get('desc', '')} _({', '.join(e['q'][-4:])})_")
            out.append("")
        _write(self.out_dir / "so_lenh.md", "\n".join(out))
        out = ["# Sổ tay thuật ngữ", "", "Tự gom từ mục **Bài học** (0 token). ✅ đã xác nhận · ◻️ chưa xác nhận.", ""]
        for k, e in sorted(self.d["terms"].items()):
            st = status(e["q"])
            if st is None:
                continue
            out.append(f"- {st} **{e['term']}** — {e.get('desc', '')} _({', '.join(e['q'][-4:])})_")
        _write(self.out_dir / "thuat_ngu.md", "\n".join(out) + "\n")


# ============================================================================ kiểm chứng lệnh (0 token)
CMD_RX = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+$|^[a-z]+[A-Z][A-Za-z0-9]*$")


def extract_commands(answer: str) -> dict[str, set[str]]:
    """Lệnh Tcl (+ tuỳ chọn) trong code block và `inline code` của câu trả lời."""
    cmds: dict[str, set[str]] = {}
    chunks = re.findall(r"```[^\n]*\n(.*?)```", answer, re.S)
    rest = re.sub(r"```.*?```", " ", answer, flags=re.S)
    chunks += re.findall(r"`([^`\n]{3,120})`", rest)
    for ch in chunks:
        for line in ch.splitlines():
            line = line.split(";#")[0].split(" #")[0].strip()
            for part in re.split(r"[\[;]", line):
                toks = part.strip().split()
                if not toks:
                    continue
                c = toks[0].strip("[]{}$\"'")
                if c.lower() in TCL_BUILTINS or not CMD_RX.match(c):
                    continue
                opts = {t.lstrip("-").rstrip("]})") for t in toks[1:]
                        if re.match(r"^-[A-Za-z][A-Za-z0-9_]*$", t.rstrip("]})"))}
                cmds.setdefault(c, set()).update(opts)
    return cmds


def verify_commands(answer: str, db: Path | None, limit: int = 25) -> tuple[str, dict[str, bool]]:
    cmds = extract_commands(answer)
    if not cmds:
        return "", {}
    if not db or not db.exists():
        return ("\n\n## Kiểm chứng tự động (script)\n\n_Chưa có chỉ mục tài liệu trên máy — chưa kiểm được "
                f"{len(cmds)} lệnh._\n"), {}
    con = sqlite3.connect(str(db), timeout=30)
    rows, verified = [], {}
    try:
        for c in sorted(cmds)[:limit]:
            def hit(q, kind):
                try:
                    return con.execute("SELECT path, loc FROM chunks WHERE chunks MATCH ? AND kind=? LIMIT 1",
                                       (q, kind)).fetchone()
                except sqlite3.OperationalError:
                    return None
            h = hit(f'"{c}"', "doc")
            verified[c] = bool(h)
            if h:
                missing = [o for o in sorted(cmds[c]) if not hit(f'"{c}" AND "{o}"', "doc")]
                where = f"{h[0]}{' ' + h[1] if h[1] else ''}"
                note = f"tuỳ chọn chưa thấy: {', '.join('-' + o for o in missing)}" if missing else "đủ tuỳ chọn"
                rows.append(f"| `{c}` | 📘 {where} | {'⚠️ ' + note if missing else note} |")
                continue
            sh = hit(f'"{c}"', "script")
            if sh:
                rows.append(f"| `{c}` | 📄 chỉ thấy trong script dự án ({sh[0]}), chưa thấy trong tài liệu tool "
                            f"| ⚠️ kiểm bằng `help {c}` |")
            else:
                rows.append(f"| `{c}` | ⚠️ không thấy trong tài liệu trên máy | cần `help {c}` trong tool |")
    finally:
        con.close()
    head = ("\n\n## Kiểm chứng tự động (script, 0 token)\n\n"
            "Tra từng lệnh trong chỉ mục tài liệu/script trên máy. ⚠️ = chưa kiểm chứng được, nên chạy "
            "`help <lệnh>` trước khi dùng.\n\n| Lệnh | Tài liệu trên máy | Tuỳ chọn |\n|---|---|---|\n")
    return head + "\n".join(rows) + "\n", verified


def migrate_old_attachments(old_dir: Path, store: Store) -> int:
    """Chuyển du_lieu_gui/ (bản cũ, trên OneDrive) vào kho máy ngoài."""
    if not old_dir.is_dir():
        return 0
    n = 0
    dest = store.pdir(DEFAULT_PROJECT) / "du_lieu"
    for p in old_dir.iterdir():
        if p.name == "MUC_LUC.md":
            continue
        try:
            shutil.move(str(p), str(dest / p.name))
            n += 1
        except OSError:
            pass
    try:
        for p in old_dir.iterdir():
            p.unlink()
        old_dir.rmdir()
    except OSError:
        pass
    return n
