#!/usr/bin/env python3
"""rt_analyze.py - Phân tích report_timing dạng text (định dạng kiểu PrimeTime).

Đọc một hoặc nhiều path trong file report, tính các đại lượng chính (latency, skew,
CRPR, uncertainty, setup/hold time, data path, cell/net delay, delta...) và chạy bộ
luật phát hiện bất thường (rule-of-thumb) để gợi ý chỗ cần xem kỹ.

Report nên được tạo với các cột đầy đủ, ví dụ:
    report_timing -path_type full_clock_expanded -input_pins -nets -transition_time \
                  -capacitance -crosstalk_delta -derate -significant_digits 4 -nosplit

Cách dùng:
    python3 rt_analyze.py report.rpt                 # in phân tích dạng text
    python3 rt_analyze.py report.rpt --csv out.csv   # thêm bảng tóm tắt CSV
    python3 rt_analyze.py report.rpt --stages        # in thêm bảng từng stage

Giới hạn: đây là công cụ triage. Định dạng report thay đổi theo tool/version/option;
mọi cảnh báo chỉ là gợi ý để kiểm chứng bằng report_delay_calculation, report_net,
report_clock_timing... chứ không phải kết luận.
"""
import argparse
import csv
import re
import statistics
import sys

# --------------------------------------------------------------------------- #
# Ngưỡng rule-of-thumb (chỉnh theo node/thư viện/methodology của dự án)
# --------------------------------------------------------------------------- #
TH = {
    "trans_vs_median": 3.0,     # trans tại driver > 3x median trans của data path
    "trans_abs": None,          # ns; đặt giá trị (vd 0.15) nếu có giới hạn tuyệt đối
    "fanout_high": 16,          # fanout coi là lớn
    "fanout_weak": 6,           # fanout tối đa hợp lý cho cell drive yếu (X0/X1)
    "net_vs_cell": 0.30,        # net delay > 30% cell delay của driver
    "net_abs": 0.010,           # ns; bỏ qua net delay nhỏ hơn mức này
    "slew_degrade": 0.25,       # trans tại tải lớn hơn tại driver > 25%
    "delta_vs_stage": 0.10,     # delta > 10% (cell + net) của stage
    "delta_abs": 0.005,         # ns
    "cell_vs_median": 2.0,      # cell delay > 2x median cell delay của path
    "skew_vs_slack": 0.5,       # skew bất lợi >= 50% |slack âm|
    "skew_vs_relation": 0.05,   # skew bất lợi > 5% khoảng cách cạnh (setup)
    "unc_vs_relation": 0.10,    # uncertainty > 10% khoảng cách cạnh
    "lib_vs_relation": 0.10,    # setup/hold time thư viện > 10% khoảng cách cạnh
    "io_vs_relation": 0.60,     # input/output external delay > 60% khoảng cách cạnh
    "clk_trans_ratio": 1.8,     # trans tại CP capture/launch chênh > 1.8 lần
    "clk_cell_trans": 2.0,      # trans cell clock > 2x median trans cây clock
    "depth_max": 30,            # số tầng logic coi là sâu
    "hier_max": 3,              # số hierarchy cấp 1 mà path đi qua
    "path_tol": 0.0006,         # sai số cộng dồn cho phép (ns)
}
WEAK_RE = re.compile(r"(_X0|_X1|X0P\d|D0|D1|_1)$|(_X0|_X1)[A-Z_]", re.I)
DELAY_CELL_RE = re.compile(r"(^|_)(DLY|DEL|DELAY)\w*", re.I)
NONINV_RE = re.compile(r"^(BUF|CKBUF|AN\d|AND\d|OR\d|DLY|DEL|CKAN)", re.I)
INV_RE = re.compile(r"^(INV|CKINV|ND\d|NAND\d|NR\d|NOR\d|AOI|OAI)", re.I)

NUM_RE = re.compile(r"(?<![\w\[\]/.])-?\d+(?:\.\d+)?(?![\w\[\]])")
COLS = ["Fanout", "Cap", "Trans", "Derate", "Delta", "Incr", "Path"]


def fmt(x, nd=4):
    return "-" if x is None else f"{x:.{nd}f}"


class Row:
    __slots__ = ("text", "label", "vals", "sym", "rf", "kind", "inst", "pin", "cell")

    def __init__(self, text):
        self.text = text
        self.label = ""
        self.vals = {}
        self.sym = ""
        self.rf = ""
        self.kind = "other"
        self.inst = self.pin = self.cell = None


def parse_row(line, col_ends):
    r = Row(line)
    s = line.rstrip()
    m = re.search(r"\s([rf])$", s)
    if m:
        r.rf = m.group(1)
        s = s[: m.start()]
    nums = list(NUM_RE.finditer(s))
    # nhãn = phần chữ trước con số đầu tiên đứng sau ít nhất 2 khoảng trắng
    label_end = len(s)
    for n in nums:
        if s[: n.start()].endswith("  "):
            label_end = n.start()
            break
    r.label = s[:label_end].strip()
    rest_nums = [n for n in nums if n.start() >= label_end]
    syms = re.findall(r"(?<=\d)\s+([&*H^$+@])(?=\s|$)", s[label_end:])
    r.sym = "".join(syms)
    for n in rest_nums:
        end = n.end()
        if col_ends:
            col = min(col_ends, key=lambda c: abs(col_ends[c] - end))
        else:
            col = None
        val = float(n.group())
        if col is None:
            r.vals.setdefault("_seq", []).append(val)
        else:
            r.vals[col] = val
    if not col_ends and "_seq" in r.vals:
        seq = r.vals.pop("_seq")
        if len(seq) >= 2:
            r.vals["Incr"], r.vals["Path"] = seq[-2], seq[-1]
        elif seq:
            r.vals["Path"] = seq[-1]
    lab = r.label
    if re.match(r"clock \S+ \((rise|fall) edge\)", lab):
        r.kind = "edge"
    elif lab.endswith("(net)"):
        r.kind = "net"
    elif lab.startswith("slack"):
        r.kind = "slack"
    elif lab in ("data arrival time", "data required time"):
        r.kind = lab.replace(" ", "_")
    else:
        pm = re.match(r"^(\S+) \(([^)]+)\)$", lab)
        if pm and not lab.startswith("clock "):
            r.kind = "pin"
            name, cell = pm.group(1), pm.group(2)
            r.cell = cell
            if cell in ("in", "out", "inout"):
                r.inst, r.pin = name, name
            else:
                r.inst, _, r.pin = name.rpartition("/")
        elif lab:
            r.kind = "adj"
    return r


def split_paths(text):
    blocks, cur = [], []
    for line in text.splitlines():
        if line.strip().startswith("Startpoint:") and cur:
            blocks.append(cur)
            cur = []
        cur.append(line)
    if cur:
        blocks.append(cur)
    return [b for b in blocks if any(l.strip().startswith("Startpoint:") for l in b)]


def header_info(lines):
    h = {}
    for i, l in enumerate(lines):
        t = l.strip()
        for key in ("Startpoint", "Endpoint", "Last common pin", "Path Group", "Path Type", "Scenario", "Sigma"):
            if t.startswith(key + ":"):
                val = t.split(":", 1)[1].strip()
                m = re.match(r"^(\S+)\s+\((.*)\)$", val)
                if m and key in ("Startpoint", "Endpoint"):
                    val = m.group(1)
                    h[key + "_desc"] = m.group(2)
                h[key] = val
                if key in ("Startpoint", "Endpoint") and i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    if nxt.startswith("("):
                        h[key + "_desc"] = nxt.strip("()")
    return h


def analyze_path(lines):
    h = header_info(lines)
    col_ends, rows = {}, []
    for l in lines:
        if l.strip().startswith("Point") and "Path" in l:
            col_ends = {c: l.index(c) + len(c) for c in COLS if re.search(r"\b" + c + r"\b", l)}
            continue
        if not l.strip() or set(l.strip()) <= set("-"):
            continue
        if l.strip().split(":")[0] in ("Startpoint", "Endpoint", "Last common pin", "Path Group",
                                      "Path Type", "Scenario", "Sigma"):
            continue
        if l.strip().startswith("(") or l.strip().startswith("#") or l.strip().startswith("*"):
            continue
        rows.append(parse_row(l, col_ends))

    P = {"hdr": h, "flags": [], "stages": []}
    check = "hold" if h.get("Path Type", "max").startswith("min") else "setup"
    P["check"] = check
    sp, ep = h.get("Startpoint", ""), h.get("Endpoint", "")

    # ---- chia khối ----
    sec, edges, adj = "pre", [], {}
    launch_rows, data_rows, cap_rows = [], [], []
    arrival = required = slack = None
    sp_seen = False
    for r in rows:
        if r.kind == "edge":
            edges.append((r.label, r.vals.get("Incr", r.vals.get("Path"))))
            sec = "launch" if sec == "pre" else ("capture" if sec == "after_arrival" else sec)
            (launch_rows if sec == "launch" else cap_rows).append(r)
            continue
        if r.kind == "data_arrival_time":
            if arrival is None:          # dòng tổng kết cuối report lặp lại (có thể mang dấu -)
                arrival = r.vals.get("Path")
            if sec == "launch":
                sec = "after_arrival"
            continue
        if r.kind == "data_required_time":
            if required is None:
                required = r.vals.get("Path")
            sec = "after_required"
            continue
        if r.kind == "slack":
            slack = r.vals.get("Path")
            continue
        if sec == "launch":
            if r.kind == "pin" and not sp_seen and (r.inst == sp or r.pin == sp):
                sp_seen = True
                launch_rows.append(r)
                data_rows.append(r)
                continue
            (data_rows if sp_seen else launch_rows).append(r)
        elif sec == "capture":
            cap_rows.append(r)
        if r.kind == "adj" and sec in ("launch", "capture"):
            adj[r.label] = r.vals.get("Incr")

    P.update(arrival=arrival, required=required, slack=slack, adj=adj)
    P["launch_edge"] = edges[0] if edges else None
    P["capture_edge"] = edges[1] if len(edges) > 1 else None
    le = edges[0][1] if edges else 0.0
    ce = edges[1][1] if len(edges) > 1 else None
    relation = (ce - le) if (ce is not None and le is not None) else None
    P["relation"] = relation

    # ---- latency ----
    sp_row = data_rows[0] if data_rows else None
    ext_in = adj.get("input external delay")

    def clock_latency(sec_rows, stop):
        # Cộng source latency + network delay + các cung trên cây clock cho tới clock pin
        # (không cộng CRPR/uncertainty) -> không phụ thuộc thứ tự in các dòng điều chỉnh.
        tot, found = 0.0, False
        for r in sec_rows:
            if r.kind == "edge":
                continue
            if r.kind == "adj":
                if r.label.startswith("clock source latency") or r.label.startswith("clock network delay"):
                    tot += r.vals.get("Incr") or 0.0
                continue
            if r.kind == "pin":
                tot += r.vals.get("Incr") or 0.0
                if stop(r):
                    found = True
                    break
        return tot if found else None

    if sp_row is not None and sp_row.cell not in ("in", "inout"):
        launch_lat = clock_latency(launch_rows, lambda r: r is sp_row)
    else:
        launch_lat = None
    capture_lat = clock_latency(cap_rows, lambda r: r.inst == ep or r.pin == ep)
    P.update(launch_lat=launch_lat, capture_lat=capture_lat)
    P["raw_skew"] = (capture_lat - launch_lat) if (capture_lat is not None and launch_lat is not None) else None
    P["crpr"] = adj.get("clock reconvergence pessimism")
    P["uncertainty"] = adj.get("clock uncertainty")
    libkey = next((k for k in adj if k.startswith("library ") or "gating" in k or "data check" in k), None)
    P["lib_check"] = (libkey, adj.get(libkey)) if libkey else (None, None)
    P["ext_in"], P["ext_out"] = ext_in, adj.get("output external delay")
    start_path = sp_row.vals.get("Path") if sp_row is not None else None
    P["data_delay"] = (arrival - start_path) if (arrival is not None and start_path is not None) else None

    # ---- stage (cell arc / net arc) ----
    stages, cur_net = [], None
    prev_pin = data_rows[0] if data_rows and data_rows[0].kind == "pin" else None
    for r in data_rows[1:] if data_rows else []:
        if r.kind == "net":
            cur_net = r
            continue
        if r.kind != "pin":
            continue
        incr = r.vals.get("Incr", 0.0) or 0.0
        if prev_pin is not None and r.inst == prev_pin.inst and r.cell not in ("in", "out"):
            stages.append(dict(kind="cell", inst=r.inst, cell=r.cell, from_pin=prev_pin.pin, to_pin=r.pin,
                               delay=incr, trans=r.vals.get("Trans"), in_trans=prev_pin.vals.get("Trans"),
                               rf_in=prev_pin.rf, rf_out=r.rf, derate=r.vals.get("Derate"), sym=r.sym))
        else:
            stages.append(dict(kind="net", net=cur_net.label.replace(" (net)", "") if cur_net else "?",
                               fanout=cur_net.vals.get("Fanout") if cur_net else None,
                               cap=cur_net.vals.get("Cap") if cur_net else None,
                               load=f"{r.inst}/{r.pin}" if r.cell not in ("in", "out") else r.inst,
                               load_cell=r.cell, delay=incr, delta=r.vals.get("Delta") or 0.0,
                               load_trans=r.vals.get("Trans"), sym=r.sym))
            cur_net = None
        prev_pin = r
    if stages and not any(x["kind"] == "cell" for x in stages):
        # Không có -input_pins: mỗi dòng output pin mang delay gộp (net vào + cell).
        for x in stages:
            if x["load_cell"] not in ("in", "out") and not (x["load"].endswith("/D") and x is stages[-1]):
                inst, _, pin = x["load"].rpartition("/")
                x.update(kind="cell", inst=inst, cell=x["load_cell"], from_pin="(net+cell)", to_pin=pin,
                         trans=x["load_trans"], in_trans=None, rf_in="", rf_out="", derate=None)
        P["merged_arcs"] = True
    P["stages"] = stages
    cells = [s for s in stages if s["kind"] == "cell"]
    nets = [s for s in stages if s["kind"] == "net"]
    P["sum_cell"] = sum(s["delay"] for s in cells)
    P["sum_net"] = sum(s["delay"] for s in nets)
    P["sum_delta"] = sum(s["delta"] for s in nets)
    P["n_cells"] = len(cells)

    # ---- kiểm tra cộng dồn ----
    flags = P["flags"]
    prev = None
    for r in launch_rows + data_rows[1:] + cap_rows:
        p, inc = r.vals.get("Path"), r.vals.get("Incr")
        if p is None:
            continue
        if prev is not None and inc is not None and r.kind != "edge":
            if abs(prev + inc - p) > TH["path_tol"]:
                flags.append(("SANITY", "Path không cộng dồn tại '%s': %s + %s != %s (PBA/POCV/làm tròn?)"
                              % (r.label, fmt(prev), fmt(inc), fmt(p))))
        prev = p
    if arrival is not None and required is not None and slack is not None:
        exp = (required - arrival) if check == "setup" else (arrival - required)
        if abs(exp - slack) > TH["path_tol"]:
            flags.append(("SANITY", f"slack {fmt(slack)} không khớp required/arrival ({fmt(exp)})"))

    # ---- luật bất thường ----
    rules(P, cells, nets, launch_rows, cap_rows, data_rows)
    return P


def rules(P, cells, nets, launch_rows, cap_rows, data_rows):
    f = P["flags"]
    check, rel, slack = P["check"], P["relation"], P["slack"]
    h = P["hdr"]

    # Header / constraint
    le, ce = P["launch_edge"], P["capture_edge"]
    if le and ce:
        lclk = re.match(r"clock (\S+) \((\w+) edge\)", le[0])
        cclk = re.match(r"clock (\S+) \((\w+) edge\)", ce[0])
        if lclk and cclk:
            io_path = bool(data_rows) and data_rows[0].cell in ("in", "inout") or h.get("Endpoint_desc", "").startswith("output port")
            if lclk.group(1) != cclk.group(1) and io_path:
                f.append(("INFO", f"Path IO: clock {lclk.group(1)} (thường là clock ảo) -> {cclk.group(1)}, quan hệ cạnh "
                                  f"{fmt(rel)} ns — đối chiếu với spec interface"))
            elif lclk.group(1) != cclk.group(1):
                f.append(("CONSTRAINT", f"Path chéo clock {lclk.group(1)} -> {cclk.group(1)}, quan hệ cạnh "
                                        f"{fmt(rel)} ns: xác nhận clock đồng bộ thật (không thiếu set_clock_groups/false path)"))
            if lclk.group(2) != cclk.group(2):
                f.append(("CONSTRAINT", f"Launch {lclk.group(2)} -> capture {cclk.group(2)}: path nửa chu kỳ (half-cycle), "
                                        "kiểm tra có chủ đích không"))
        if check == "hold" and rel is not None and abs(rel) > TH["path_tol"]:
            f.append(("CONSTRAINT", f"Hold check với khoảng cách cạnh {fmt(rel)} ns (khác 0): nghi multicycle -setup "
                                    "thiếu -hold tương ứng hoặc quan hệ clock đặc biệt"))
    if h.get("Path Group", "").strip("*") == "default":
        f.append(("CONSTRAINT", "Path group **default**: path không có capture clock thông thường, kiểm tra constraint"))
    sp_is_port = bool(data_rows) and data_rows[0].cell in ("in", "inout")
    if any("ideal" in r.label for r in (cap_rows if sp_is_port else launch_rows + cap_rows)):
        f.append(("CLOCK", "Clock network delay (ideal): clock chưa propagated — không hợp lệ cho signoff sau CTS"))

    # Clock
    skew = P["raw_skew"]
    if skew is not None:
        bad = -skew if check == "setup" else skew
        crpr = abs(P["crpr"] or 0.0)
        if bad > 0:
            cond = (slack is not None and slack < 0 and bad >= TH["skew_vs_slack"] * abs(slack))
            if check == "setup" and rel:
                cond = cond or bad > TH["skew_vs_relation"] * rel
            if check == "hold" and P["data_delay"]:
                cond = cond or bad > 0.5 * P["data_delay"]
            if cond:
                f.append(("CLOCK", f"Skew bất lợi {fmt(bad)} ns (raw, trước CRPR {fmt(crpr)}): clock tree góp phần "
                                   "lớn vào vi phạm — xem report_clock_timing, cân bằng nhánh trước khi ECO data"))
    if h.get("Last common pin") and (P["crpr"] is None or abs(P["crpr"]) < 1e-9):
        f.append(("CLOCK", "Có Last common pin nhưng CRPR = 0/không có: kiểm tra CRPR đã bật chưa"))
    lp = next((r for r in launch_rows if r.kind == "pin" and r is (data_rows[0] if data_rows else None)), None)
    cp = None
    for r in cap_rows:
        if r.kind == "pin" and (r.inst == h.get("Endpoint")):
            cp = r
    if lp is not None and cp is not None and lp.vals.get("Trans") and cp.vals.get("Trans"):
        a, b = lp.vals["Trans"], cp.vals["Trans"]
        if max(a, b) / max(min(a, b), 1e-9) > TH["clk_trans_ratio"]:
            f.append(("CLOCK", f"Trans tại clock pin launch {fmt(a)} vs capture {fmt(b)} chênh > {TH['clk_trans_ratio']}x: "
                               "nhánh clock yếu/không cân bằng (ảnh hưởng latency và setup/hold time)"))
    clk_cells = [r for r in launch_rows + cap_rows if r.kind == "pin" and r.vals.get("Trans") and r.cell not in ("in", "out")]
    if len(clk_cells) >= 4:
        med = statistics.median(r.vals["Trans"] for r in clk_cells)
        for r in clk_cells:
            if r.vals["Trans"] > TH["clk_cell_trans"] * med and r.pin not in ("CP", "CK", "CLK"):
                f.append(("CLOCK", f"Trans clock {fmt(r.vals['Trans'])} tại {r.inst}/{r.pin} ({r.cell}) > "
                                   f"{TH['clk_cell_trans']}x median cây clock {fmt(med)}"))
    for r in launch_rows + cap_rows:
        if r.kind == "pin" and r.cell and r.cell not in ("in", "out") and not re.match(r"(CK|CLK|DFF|SDF|LAT|ICG|CG)", r.cell, re.I):
            if r.inst not in (h.get("Startpoint"), h.get("Endpoint")):
                f.append(("CLOCK", f"Cell không phải clock cell trên đường clock: {r.inst} ({r.cell})"))
                break

    # Required side
    unc = P["uncertainty"]
    if unc is not None and rel:
        if abs(unc) > TH["unc_vs_relation"] * rel:
            f.append(("REQUIRED", f"Uncertainty {fmt(abs(unc))} > {int(TH['unc_vs_relation']*100)}% quan hệ cạnh {fmt(rel)}: "
                                  "kiểm tra còn chứa skew ước lượng pre-CTS không"))
    lk, lv = P["lib_check"]
    if lv is not None:
        if rel and abs(lv) > TH["lib_vs_relation"] * rel:
            f.append(("REQUIRED", f"{lk} {fmt(abs(lv))} lớn so với quan hệ cạnh: xem slew tại D và CP"))
        if (check == "setup" and lv > 0) or (check == "hold" and lv < 0):
            f.append(("REQUIRED", f"{lk} có dấu bất thường ({fmt(lv)}): setup/hold time âm theo lib — hợp lệ nhưng nên hiểu vì sao"))
    for key, name in (("ext_in", "input external delay"), ("ext_out", "output external delay")):
        v = P[key]
        if v is not None and rel and abs(v) > TH["io_vs_relation"] * rel:
            f.append(("IO", f"{name} {fmt(abs(v))} = {abs(v)/rel*100:.0f}% quan hệ cạnh: kiểm tra IO budget"))

    # Data path
    if cells:
        med_cell = statistics.median(s["delay"] for s in cells)
        trs = [s["trans"] for s in cells if s["trans"] is not None]
        med_tr = statistics.median(trs) if trs else None
        in_trs = [s["in_trans"] for s in cells if s["in_trans"] is not None]
        med_in = statistics.median(in_trs) if in_trs else None
        for i, s in enumerate(cells):
            tag = f"{s['inst']} ({s['cell']}) {s['from_pin']}->{s['to_pin']}"
            if s["delay"] > TH["cell_vs_median"] * med_cell:
                f.append(("DATA", f"Cell delay {fmt(s['delay'])} tại {tag} > {TH['cell_vs_median']}x median {fmt(med_cell)}"))
            if med_tr and s["trans"] and (s["trans"] > TH["trans_vs_median"] * med_tr or
                                         (TH["trans_abs"] and s["trans"] > TH["trans_abs"])):
                f.append(("DATA", f"Trans ngõ ra {fmt(s['trans'])} tại {tag} > {TH['trans_vs_median']}x median {fmt(med_tr)}"))
            if med_in and s["in_trans"] and s["in_trans"] > TH["trans_vs_median"] * med_in and s["delay"] > 1.3 * med_cell:
                f.append(("DATA", f"Delay {fmt(s['delay'])} của {tag} bị thổi phồng bởi slew đầu vào {fmt(s['in_trans'])} "
                                  "(nguyên nhân nằm ở stage trước)"))
            if DELAY_CELL_RE.search(s["cell"] or "") and P["check"] == "setup":
                f.append(("DATA", f"Delay cell trên path setup: {tag} = {fmt(s['delay'])} — kiểm tra hold fix chèn sai chỗ"))
            if s["rf_in"] and s["rf_out"]:
                if NONINV_RE.match(s["cell"] or "") and s["rf_in"] != s["rf_out"]:
                    f.append(("DATA", f"r/f lật qua cell không đảo {tag} ({s['rf_in']}->{s['rf_out']}): kiểm tra arc/cell"))
                if INV_RE.match(s["cell"] or "") and s["rf_in"] == s["rf_out"]:
                    f.append(("DATA", f"r/f không lật qua cell đảo {tag} ({s['rf_in']}->{s['rf_out']}): kiểm tra arc/cell"))
    st = P["stages"]
    for idx, s in enumerate(st):
        if s["kind"] != "net":
            continue
        drv = st[idx - 1] if idx > 0 and st[idx - 1]["kind"] == "cell" else None
        fo = s["fanout"]
        if fo is not None and fo >= TH["fanout_high"]:
            f.append(("DATA", f"Fanout {int(fo)} trên net {s['net']} (cap {fmt(s['cap'])})"))
        if drv is not None and fo is not None and fo > TH["fanout_weak"] and WEAK_RE.search(drv["cell"] or ""):
            f.append(("DATA", f"Cell drive yếu {drv['inst']} ({drv['cell']}) lái fanout {int(fo)}"))
        if drv is not None and s["delay"] >= max(TH["net_abs"], TH["net_vs_cell"] * drv["delay"]):
            f.append(("DATA", f"Net delay {fmt(s['delay'])} trên {s['net']} lớn so với cell driver {fmt(drv['delay'])}: dây dài/điện trở cao/crosstalk"))
        if drv is not None and drv["trans"] and s["load_trans"]:
            if s["load_trans"] - drv["trans"] > TH["slew_degrade"] * drv["trans"] and s["load_trans"] - drv["trans"] > 0.005:
                f.append(("DATA", f"Slew suy giảm trên dây {s['net']}: {fmt(drv['trans'])} -> {fmt(s['load_trans'])}"))
            if s["load_trans"] < drv["trans"] - 0.002:
                f.append(("SANITY", f"Trans tại tải {fmt(s['load_trans'])} nhỏ hơn tại driver {fmt(drv['trans'])} trên {s['net']}: bất thường"))
        if s["delta"]:
            stage = (drv["delay"] if drv else 0.0) + s["delay"]
            if check == "setup" and s["delta"] < 0:
                f.append(("SI", f"Delta âm {fmt(s['delta'])} trên path setup ({s['net']}): kiểm tra"))
            if abs(s["delta"]) >= max(TH["delta_abs"], TH["delta_vs_stage"] * stage):
                share = abs(s["delta"]) / s["delay"] * 100 if s["delay"] else 0
                f.append(("SI", f"Delta {fmt(s['delta'])} trên {s['net']} = {share:.0f}% net delay, "
                                f"{abs(s['delta'])/stage*100:.0f}% stage"))
    syms = [s["sym"] for s in st if s["kind"] == "net"]
    if syms and any(syms) and not all(syms):
        miss = [s["net"] for s in st if s["kind"] == "net" and not s["sym"]]
        f.append(("ANNOT", f"Một số net arc không có ký hiệu annotate (vd {', '.join(miss[:3])}): kiểm tra report_annotated_parasitics"))
    if P["n_cells"] > TH["depth_max"]:
        f.append(("DATA", f"Logic depth {P['n_cells']} cell > {TH['depth_max']}"))
    tops = []
    for s in cells:
        top = s["inst"].split("/")[0] if "/" in s["inst"] else "<top>"
        if top not in tops:
            tops.append(top)
    if len(tops) >= TH["hier_max"]:
        f.append(("INFO", f"Path đi qua {len(tops)} hierarchy ({' -> '.join(tops)}): khả năng khoảng cách vật lý lớn"))
    der = sorted({s["derate"] for s in cells if s["derate"] is not None})
    if len(der) > 1:
        f.append(("INFO", f"Nhiều giá trị derate trên cell của data path: {der}"))


def report(P, show_stages=False):
    h = P["hdr"]
    out = []
    out.append("=" * 100)
    out.append(f"{P['check'].upper()}  {h.get('Startpoint','?')}  ->  {h.get('Endpoint','?')}")
    out.append(f"  Group={h.get('Path Group','-')}  Scenario={h.get('Scenario','-')}  LastCommon={h.get('Last common pin','-')}")
    le, ce = P["launch_edge"], P["capture_edge"]
    out.append(f"  Launch edge: {le[0] if le else '-'} @ {fmt(le[1]) if le else '-'} | "
               f"Capture edge: {ce[0] if ce else '-'} @ {fmt(ce[1]) if ce else '-'} | relation={fmt(P['relation'])}")
    out.append(f"  Arrival={fmt(P['arrival'])} Required={fmt(P['required'])} Slack={fmt(P['slack'])}")
    lk, lv = P["lib_check"]
    out.append(f"  Launch lat={fmt(P['launch_lat'])} Capture lat={fmt(P['capture_lat'])} Raw skew={fmt(P['raw_skew'])} "
               f"CRPR={fmt(P['crpr'])} Unc={fmt(P['uncertainty'])} {lk or 'lib'}={fmt(lv)}")
    if P["ext_in"] is not None or P["ext_out"] is not None:
        out.append(f"  Input ext delay={fmt(P['ext_in'])} Output ext delay={fmt(P['ext_out'])}")
    dd = P["data_delay"]
    out.append(f"  Data path={fmt(dd)} = cell {fmt(P['sum_cell'])} + net {fmt(P['sum_net'])}  (delta tổng {fmt(P['sum_delta'])}, "
               f"{P['n_cells']} cell arc gồm clk->Q nếu có)")
    if show_stages:
        out.append("  Stages:")
        for s in P["stages"]:
            if s["kind"] == "cell":
                share = s["delay"] / dd * 100 if dd else 0
                out.append(f"    CELL {s['inst']:<28} {s['cell']:<10} {s['from_pin']:>3}->{s['to_pin']:<3} "
                           f"delay={fmt(s['delay'])} ({share:4.1f}%) trans={fmt(s['trans'])} {s['rf_in']}->{s['rf_out']}")
            else:
                out.append(f"    NET  {s['net']:<28} fo={'-' if s['fanout'] is None else int(s['fanout']):<3} "
                           f"cap={fmt(s['cap'])} delay={fmt(s['delay'])} delta={fmt(s['delta'])} load_trans={fmt(s['load_trans'])}")
    if P["flags"]:
        out.append("  Bất thường / cần xem:")
        for cat, msg in P["flags"]:
            out.append(f"    [{cat:<10}] {msg}")
    else:
        out.append("  Không có cảnh báo theo bộ luật hiện tại.")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report")
    ap.add_argument("--csv")
    ap.add_argument("--stages", action="store_true")
    a = ap.parse_args()
    text = open(a.report, encoding="utf-8", errors="ignore").read()
    paths = [analyze_path(b) for b in split_paths(text)]
    for P in paths:
        print(report(P, a.stages))
    if a.csv:
        keys = ["check", "startpoint", "endpoint", "slack", "relation", "launch_lat", "capture_lat", "raw_skew",
                "crpr", "uncertainty", "lib_check", "data_delay", "sum_cell", "sum_net", "sum_delta", "n_cells", "n_flags"]
        with open(a.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(keys)
            r4 = lambda v: round(v, 4) if isinstance(v, float) else v
            for P in paths:
                w.writerow([r4(x) for x in [P["check"], P["hdr"].get("Startpoint"), P["hdr"].get("Endpoint"), P["slack"], P["relation"],
                            P["launch_lat"], P["capture_lat"], P["raw_skew"], P["crpr"], P["uncertainty"],
                            P["lib_check"][1], P["data_delay"], P["sum_cell"], P["sum_net"], P["sum_delta"],
                            P["n_cells"], len(P["flags"])]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
