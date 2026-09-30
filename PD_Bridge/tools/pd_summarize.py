#!/usr/bin/env python3
"""pd_summarize.py - tóm tắt report/log Physical Design thành vài chục dòng (0 token).

  python pd_summarize.py <file|thư mục> [--max-lines 80] [--top 10]

Nhận dạng tự động:
  * bảng tóm tắt timeDesign/report_timing_summary (WNS/TNS/Violating Paths, DRV, Density)
  * report_timing (Innovus / Tempus / PrimeTime / ICC2): số path, WNS, TNS, top path xấu,
    phân bố slack, nhóm theo hierarchy của endpoint
  * log Innovus/Genus/ICC2: đếm ERROR/WARN theo mã, chuỗi lệnh, runtime/mem, dòng cuối
  * report khác (clock/skew, power, area, DRC, congestion...): dòng có từ khoá, gộp dòng giống nhau
Thư mục: liệt kê từng report/log với một dòng chỉ số chính.
Hỗ trợ file .gz.
"""
from __future__ import annotations

import argparse
import gzip
import re
import sys
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

NUM = r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?"


def read_text(p: Path, limit: int = 200 * 1024 * 1024) -> str:
    opener = gzip.open if p.name.endswith(".gz") else open
    with opener(p, "rb") as f:
        raw = f.read(limit)
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def human(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def shape(line: str) -> str:
    s = re.sub(NUM, "#", line)
    s = re.sub(r"\[[^\]]*\]", "[]", s)
    return re.sub(r"\s+", " ", s).strip()


def compact(line: str, width: int = 160) -> str:
    s = re.sub(r"\s{2,}", "  ", line.strip())
    return s if len(s) <= width else s[: width - 1] + "…"


# ----------------------------------------------------------------------------- summary tables
SUMMARY_KEYS = re.compile(
    r"(Setup mode|Hold mode|WNS|TNS|Violating Paths|All Paths|max_cap|max_tran|max_fanout|"
    r"max_length|Density:|Routing Overflow|Real DRV|Total DRV|Worst Negative Slack|"
    r"Total Negative Slack|Number of Violating|Critical Path Slack|Total Power|Leakage Power|"
    r"Internal Power|Switching Power|Design Area|Cell Area|Utilization|Core Utilization|"
    r"Chip area|Total area)", re.I)


def summary_tables(text: str, max_lines: int) -> list[str]:
    out = []
    for line in text.splitlines():
        if SUMMARY_KEYS.search(line) and len(line) < 400:
            s = compact(line.replace("|", " | ").replace("  |", " |"))
            s = re.sub(r"\s*\|\s*", " | ", s).strip(" |")
            if s and (not out or out[-1] != s):
                out.append(s)
        if len(out) >= max_lines:
            break
    return out


# ----------------------------------------------------------------------------- timing paths
PATH_START = re.compile(
    r"^\s*(Path\s+\d+\s*:|Startpoint\s*:)", re.I)


def parse_timing_paths(text: str) -> list[dict]:
    lines = text.splitlines()
    starts = [i for i, l in enumerate(lines) if PATH_START.match(l)]
    # PrimeTime: mỗi path bắt đầu bằng Startpoint; Innovus: "Path N:" rồi mới Endpoint/Beginpoint.
    merged = []
    for i in starts:
        if merged and lines[i].strip().lower().startswith("startpoint") and i - merged[-1] < 6 \
                and lines[merged[-1]].strip().lower().startswith("path"):
            continue
        merged.append(i)
    merged.append(len(lines))
    paths = []
    for a, b in zip(merged, merged[1:]):
        blk = lines[a:b]
        joined = "\n".join(blk)
        d: dict = {}
        m = re.search(r"Path\s+\d+\s*:\s*(\w+)\s+(Setup|Hold|Removal|Recovery|Clock Gating \w+|\w+)\s+Check", joined, re.I)
        if m:
            d["status"], d["check"] = m.group(1).upper(), m.group(2).capitalize()
        m = re.search(r"^\s*Endpoint\s*:\s*(\S+)", joined, re.M | re.I)
        if m:
            d["end"] = m.group(1)
        m = re.search(r"^\s*(?:Beginpoint|Startpoint)\s*:\s*(\S+)", joined, re.M | re.I)
        if m:
            d["start"] = m.group(1)
        m = re.search(r"Path Groups?\s*:\s*\{?\s*([^}\n]+?)\s*\}?\s*$", joined, re.M | re.I)
        if m:
            d["group"] = m.group(1).strip()
        m = re.search(r"Path Type\s*:\s*(max|min)", joined, re.I)
        if m:
            d["check"] = d.get("check") or ("Setup" if m.group(1).lower() == "max" else "Hold")
        m = re.search(r"(?:=\s*)?Slack(?:\s+Time)?\s*[:=]?\s*(" + NUM + r")", joined, re.I) or \
            re.search(r"^\s*slack\s*\((?:VIOLATED|MET)[^)]*\)\s*(" + NUM + r")", joined, re.M | re.I)
        if m:
            d["slack"] = float(m.group(1))
        m = re.search(r"slack\s*\((VIOLATED|MET)", joined, re.I)
        if m:
            d["status"] = m.group(1).upper()
        for key, rx in (("skew", r"Clock\s+Skew\s*[:=]?\s*(" + NUM + ")"),
                        ("uncert", r"(?:Clock\s+)?Uncertainty\s*[:=]?\s*(" + NUM + ")"),
                        ("launch_lat", r"Beginpoint Arrival Time\s*(" + NUM + ")"),
                        ("capture_lat", r"Other End Arrival Time\s*(" + NUM + ")")):
            m = re.search(rx, joined, re.I)
            if m:
                d[key] = float(m.group(1))
        if "slack" in d and ("end" in d or "start" in d):
            paths.append(d)
    return paths


def hier_prefix(pin: str, depth: int = 2) -> str:
    """u_core/u_win/q_reg_1/D -> u_core/u_win (bỏ tên cell + pin)."""
    parts = pin.split("/")
    inst = parts[:-2] if len(parts) >= 3 else []
    return "/".join(inst[:depth]) if inst else "(top)"


def summarize_timing(paths: list[dict], top: int) -> list[str]:
    out = []
    by_check = defaultdict(list)
    for p in paths:
        by_check[p.get("check", "?")].append(p)
    for chk, ps in by_check.items():
        sl = [p["slack"] for p in ps]
        neg = [s for s in sl if s < 0]
        out.append(f"[{chk}] paths={len(ps)}  vi_pham={len(neg)}  WNS={min(sl):.4f}  "
                   f"TNS(trong report)={sum(neg):.4f}")
        bins = [(-1e9, -0.5), (-0.5, -0.2), (-0.2, -0.1), (-0.1, -0.05), (-0.05, 0), (0, 1e9)]
        hist = []
        for lo, hi in bins:
            c = sum(1 for s in sl if lo <= s < hi)
            if c:
                lab = f"<{hi}" if lo < -1e8 else (f">={lo}" if hi > 1e8 else f"[{lo},{hi})")
                hist.append(f"{lab}:{c}")
        out.append("   phân bố slack: " + "  ".join(hist))
        if neg:
            ends = Counter(hier_prefix(p.get("end", "?")) for p in ps if p["slack"] < 0)
            out.append("   endpoint vi phạm theo hierarchy: " +
                       ", ".join(f"{k}({v})" for k, v in ends.most_common(6)))
            starts = Counter(hier_prefix(p.get("start", "?")) for p in ps if p["slack"] < 0)
            out.append("   startpoint vi phạm theo hierarchy: " +
                       ", ".join(f"{k}({v})" for k, v in starts.most_common(6)))
            grp = Counter(p.get("group", "?") for p in ps if p["slack"] < 0)
            if len(grp) > 1 or "?" not in grp:
                out.append("   path group: " + ", ".join(f"{k}({v})" for k, v in grp.most_common(6)))
        for k in ("skew", "uncert"):
            vals = [p[k] for p in ps if k in p]
            if vals:
                out.append(f"   {k}: min={min(vals):.4f} max={max(vals):.4f} (n={len(vals)})")
        out.append(f"   top {min(top, len(ps))} path xấu nhất:")
        for p in sorted(ps, key=lambda p: p["slack"])[:top]:
            extra = "".join(f" {k}={p[k]}" for k in ("skew", "uncert") if k in p)
            out.append(f"   {p['slack']:>9.4f}  {p.get('start', '?')} -> {p.get('end', '?')}"
                       f"  [{p.get('group', '')}]{extra}")
    return out


# ----------------------------------------------------------------------------- logs
MSG_RX = re.compile(r"\*\*\s*(ERROR|WARN|WARNING|INFO)\s*:?\s*\(([A-Z0-9_]+-\d+)\)\s*:?\s*(.*)", re.I)
GEN_RX = re.compile(r"^\s*(Error|ERROR|Warning|WARNING|Fatal|FATAL)\s*[:\-]\s*(.*?)(?:\(([A-Z0-9_]+-\d+)\))?\s*$")
CMD_RX = re.compile(r"^\s*<CMD>\s*(.+)$")
RUNTIME_RX = re.compile(r"(real\s*[=:]\s*[\d:.]+|totcpu\s*=|mem\s*=\s*[\d.]+M|Total CPU|Elapsed|runtime)", re.I)


def summarize_log(text: str, top: int, max_lines: int) -> list[str]:
    lines = text.splitlines()
    counts: dict[str, Counter] = {"ERROR": Counter(), "WARN": Counter()}
    first_msg: dict[str, str] = {}
    cmds: list[str] = []
    runtime = []
    for l in lines:
        m = MSG_RX.search(l)
        if m:
            sev = "ERROR" if m.group(1).upper() == "ERROR" else ("WARN" if m.group(1).upper().startswith("WARN") else None)
            if sev:
                counts[sev][m.group(2)] += 1
                first_msg.setdefault(m.group(2), compact(m.group(3), 140))
            continue
        m = GEN_RX.match(l)
        if m:
            sev = "ERROR" if m.group(1).lower() in ("error", "fatal") else "WARN"
            key = m.group(3) or shape(m.group(2))[:70]
            counts[sev][key] += 1
            first_msg.setdefault(key, compact(m.group(2), 140))
            continue
        m = CMD_RX.match(l)
        if m:
            cmds.append(compact(m.group(1), 120))
        elif RUNTIME_RX.search(l) and len(runtime) < 400:
            runtime.append(compact(l, 150))
    out = [f"ERROR: {sum(counts['ERROR'].values())} ({len(counts['ERROR'])} loại)   "
           f"WARN: {sum(counts['WARN'].values())} ({len(counts['WARN'])} loại)"]
    for sev in ("ERROR", "WARN"):
        for k, c in counts[sev].most_common(top if sev == "WARN" else top * 2):
            out.append(f"  {sev:5} x{c:<5} {k}: {first_msg.get(k, '')}")
    if cmds:
        uniq = list(OrderedDict.fromkeys(c.split()[0] for c in cmds))
        out.append(f"Lệnh đã chạy ({len(cmds)} lệnh, {len(uniq)} loại): " + ", ".join(uniq[:40]))
        key_cmds = [c for c in cmds if re.match(r"(place_opt|ccopt|clock_opt|route|opt|time_design|timeDesign|"
                                                  r"optDesign|routeDesign|place_design|placeDesign|"
                                                  r"set_clock_uncertainty|set_interactive|create_ccopt|"
                                                  r"set_ccopt|setAnalysisMode|set_db|setOptMode|report_timing)", c)]
        for c in list(OrderedDict.fromkeys(key_cmds))[:25]:
            out.append(f"  > {c}")
    if runtime:
        out.append("Runtime/mem (cuối):")
        out.extend("  " + r for r in list(OrderedDict.fromkeys(runtime[-12:]))[-5:])
    out.append("Dòng cuối:")
    tail = [l for l in lines[-12:] if l.strip()]
    out.extend("  " + compact(l, 150) for l in tail[-8:])
    return out[:max_lines]


# ----------------------------------------------------------------------------- generic
KEY_RX = re.compile(
    r"(slack|violat|error|fail|wns|tns|skew|latency|insertion|uncertainty|transition|"
    r"capacitance|fanout|util|density|overflow|congest|short|spacing|antenna|open|drc|"
    r"total|power|leakage|area|worst|max|min|target|clock|hold|setup|count|summary)", re.I)


def summarize_generic(text: str, max_lines: int) -> list[str]:
    lines = text.splitlines()
    out = ["Đầu file:"]
    out.extend("  " + compact(l) for l in [l for l in lines[:40] if l.strip()][:8])
    groups: "OrderedDict[str, list]" = OrderedDict()
    for l in lines:
        if not l.strip() or len(l) > 500 or not KEY_RX.search(l):
            continue
        k = shape(l)
        g = groups.setdefault(k, [0, l])
        g[0] += 1
    out.append(f"Dòng có từ khoá ({len(groups)} dạng):")
    budget = max_lines - len(out) - 8
    for k, (c, ex) in list(groups.items())[:max(budget, 10)]:
        out.append(f"  {'x' + str(c) + ' ' if c > 1 else ''}{compact(ex)}")
    out.append("Cuối file:")
    out.extend("  " + compact(l) for l in [l for l in lines[-20:] if l.strip()][-6:])
    return out


# ----------------------------------------------------------------------------- dispatcher
def detect(p: Path, text: str) -> str:
    n = p.name.lower()
    head = text[:200000]
    if re.search(r"\.(log|logv)(\.gz)?$", n) or re.search(r"^\s*<CMD>", head, re.M) and n.endswith((".log", ".logv")):
        return "log"
    if len(re.findall(r"^\s*(Path\s+\d+\s*:|Startpoint\s*:)", head, re.M | re.I)) >= 1 and \
            re.search(r"slack", head, re.I):
        return "timing"
    if re.search(r"(WNS|TNS)\s*\(ns\)|Setup mode|Hold mode", head):
        return "summary"
    if len(MSG_RX.findall(head)) >= 3:
        return "log"
    return "generic"


def summarize_file(p: Path, max_lines: int = 80, top: int = 10) -> str:
    try:
        text = read_text(p)
    except Exception as e:  # noqa: BLE001
        return f"== {p} : không đọc được ({e})"
    size = p.stat().st_size
    kind = detect(p, text)
    nlines = text.count("\n") + 1
    out = [f"== {p.name}  ({human(size)}, {nlines} dòng, loại: {kind})"]
    if kind == "log":
        out += summarize_log(text, top, max_lines)
        tbl = summary_tables(text, 20)
        if tbl:
            out.append("Bảng tóm tắt trong log (cuối):")
            out += ["  " + t for t in tbl[-16:]]
    elif kind == "timing":
        tbl = summary_tables(text, 30)
        if tbl:
            out += ["  " + t for t in tbl]
        paths = parse_timing_paths(text)
        if paths:
            out += summarize_timing(paths, top)
        else:
            out += summarize_generic(text, max_lines)
    elif kind == "summary":
        out += ["  " + t for t in summary_tables(text, max_lines)]
        paths = parse_timing_paths(text)
        if paths:
            out += summarize_timing(paths, top)
    else:
        out += summarize_generic(text, max_lines)
    return "\n".join(out[: max_lines + 1])


def one_line(p: Path) -> str:
    try:
        text = read_text(p, 30 * 1024 * 1024)
    except Exception:
        return "không đọc được"
    kind = detect(p, text)
    if kind == "log":
        e = len(re.findall(r"\*\*\s*ERROR", text)) + len(re.findall(r"^\s*(?:Error|ERROR)\s*:", text, re.M))
        w = len(re.findall(r"\*\*\s*WARN", text))
        return f"log  ERROR={e} WARN={w}"
    if kind in ("timing", "summary"):
        paths = parse_timing_paths(text)
        bits = []
        m = re.search(r"WNS\s*\(ns\)\s*:?\s*\|\s*(" + NUM + ")", text)
        if m:
            bits.append(f"WNS(all)={m.group(1)}")
        m = re.search(r"TNS\s*\(ns\)\s*:?\s*\|\s*(" + NUM + ")", text)
        if m:
            bits.append(f"TNS(all)={m.group(1)}")
        if paths:
            sl = [q["slack"] for q in paths]
            bits.append(f"paths={len(paths)} vi_pham={sum(s < 0 for s in sl)} worst={min(sl):.4f}")
        return f"{kind}  " + " ".join(bits)
    keys = [compact(l, 90) for l in text.splitlines()
            if re.search(r"(total|summary|viol|density|util|skew)", l, re.I) and len(l) < 200][:2]
    return "  |  ".join(keys) or "-"


REPORT_LIKE = re.compile(r"\.(rpt|rep|log|logv|summary|sum|timing|tarpt|slk|cap|tran|fanout|txt|out|drc|qor|viol)(\.gz)?$", re.I)


def summarize_dir(d: Path, limit: int = 120) -> str:
    files = sorted((f for f in d.rglob("*") if f.is_file() and REPORT_LIKE.search(f.name)),
                   key=lambda f: str(f).lower())
    out = [f"== Thư mục {d} : {len(files)} report/log"]
    for f in files[:limit]:
        out.append(f"{f.relative_to(d).as_posix():60} {human(f.stat().st_size):>6}  {one_line(f)}")
    if len(files) > limit:
        out.append(f"... còn {len(files) - limit} file")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tóm tắt report/log PD")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--max-lines", type=int, default=80)
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args(argv)
    rc = 0
    for s in a.paths:
        p = Path(s)
        if p.is_dir():
            print(summarize_dir(p))
        elif p.is_file():
            print(summarize_file(p, a.max_lines, a.top))
        else:
            print(f"Không thấy: {p}")
            rc = 1
        print()
    return rc


if __name__ == "__main__":
    sys.exit(main())
