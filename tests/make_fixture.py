#!/usr/bin/env python3
"""Tạo thư mục 'physical design' giả lập để kiểm thử PD_Bridge (không cần dữ liệu thật)."""
import sys
from pathlib import Path

ONE_TCL = """\
# one.tcl - flow median_filter
set DESIGN median_filter
source ../scripts/mmmc.tcl
init_design
floorPlan -site core -r 1.0 0.70 10 10 10 10
place_opt_design
# --- CTS
set_ccopt_property target_skew 0.08
set_ccopt_property target_max_trans 0.12
set_clock_uncertainty -setup 0.15 [all_clocks]
set_clock_uncertainty -hold 0.10 [all_clocks]
ccopt_design
# sau CTS: KHONG giam uncertainty -> hold van bi phat 0.10
timeDesign -postCTS -hold -outDir rpt/postcts_hold
optDesign -postCTS -hold
routeDesign
timeDesign -postRoute -outDir rpt/postroute
"""

SUMMARY = """\
+--------------------+---------+---------+---------+
|     Hold mode      |   all   | reg2reg | default |
+--------------------+---------+---------+---------+
|           WNS (ns):| -0.087  | -0.087  |  0.015  |
|           TNS (ns):| -3.412  | -3.412  |  0.000  |
|    Violating Paths:|   112   |   112   |    0    |
|          All Paths:|  2048   |  2010   |   38    |
+--------------------+---------+---------+---------+
Density: 71.2%
Routing Overflow: 0.00% H and 0.01% V
"""


def hold_path(i, slack, start, end):
    return f"""Path {i}: VIOLATED Hold Check with Pin {end}/CK
Endpoint:   {end}/D (^) checked with  leading edge of 'clk'
Beginpoint: {start}/Q (v) triggered by  leading edge of 'clk'
Path Groups: {{reg2reg}}
Analysis View: func_ff_hold
Other End Arrival Time          0.412
+ Hold                          0.021
+ Phase Shift                   0.000
+ Uncertainty                   0.100
= Required Time                 0.533
  Arrival Time                  {0.533 + slack:.3f}
  Slack Time                    {slack:.3f}
     Clock Rise Edge                 0.000
     = Beginpoint Arrival Time       0.380
-------------------------------------------------------------
"""


LOG = """\
<CMD> init_design
**WARN: (IMPLF-200): Pin 'A' in macro 'BUFX2' has no ANTENNAGATEAREA defined.
**WARN: (IMPLF-200): Pin 'B' in macro 'NAND2X1' has no ANTENNAGATEAREA defined.
<CMD> place_opt_design
**WARN: (IMPOPT-3564): The following cells are set dont_use temporarily by the tool.
<CMD> set_ccopt_property target_skew 0.08
<CMD> set_clock_uncertainty -hold 0.10 [all_clocks]
<CMD> ccopt_design
**ERROR: (IMPCCOPT-1209): Clock tree skew group clk/func could not meet target skew 0.08 (achieved 0.121).
**WARN: (IMPCCOPT-2215): The route type of the clock net is not set.
<CMD> timeDesign -postCTS -hold -outDir rpt/postcts_hold
--- Ending "timeDesign" (totcpu=0:00:12.3, real=0:00:15.0, mem=2310.4M) ---
<CMD> optDesign -postCTS -hold
--- Ending "optDesign" (totcpu=0:03:10.1, real=0:02:01.0, mem=2410.9M) ---
"""

HTML = """<html><head><title>set_clock_uncertainty</title></head><body>
<h1>set_clock_uncertainty</h1>
<p>set_clock_uncertainty [-setup | -hold] [-from ...] [-to ...] value object_list</p>
<p>Specifies clock uncertainty (jitter + skew margin). Before CTS, uncertainty usually models
skew + jitter. After ccopt_design the propagated clock skew is real, so the skew part of the
uncertainty should be removed and only jitter/margin kept, otherwise hold is over-pessimistic.</p>
</body></html>"""


def make(root: Path) -> Path:
    pd = root / "physical design"
    (pd / "median_filter" / "scripts").mkdir(parents=True, exist_ok=True)
    (pd / "median_filter" / "rpt" / "postcts_hold").mkdir(parents=True, exist_ok=True)
    (pd / "docs" / "innovus_tcr").mkdir(parents=True, exist_ok=True)
    (pd / "median_filter" / "db" / "median_filter.enc.dat").mkdir(parents=True, exist_ok=True)
    (pd / "median_filter" / "scripts" / "one.tcl").write_text(ONE_TCL)
    (pd / "median_filter" / "rpt" / "postcts_hold" / "median_filter_postCTS_hold.summary").write_text(SUMMARY)
    paths = []
    import random
    random.seed(1)
    for i in range(1, 41):
        blk = random.choice(["u_core/u_win", "u_core/u_sort", "u_io"])
        sl = round(random.uniform(-0.09, 0.03), 3)
        paths.append(hold_path(i, sl, f"{blk}/d_reg_{i}", f"{blk}/q_reg_{i}"))
    (pd / "median_filter" / "rpt" / "postcts_hold" / "median_filter_postCTS_hold.tarpt").write_text(
        SUMMARY + "\n" + "\n".join(paths))
    (pd / "median_filter" / "innovus.log").write_text(LOG * 3)
    (pd / "docs" / "innovus_tcr" / "set_clock_uncertainty.html").write_text(HTML)
    (pd / "median_filter" / "db" / "median_filter.enc.dat" / "big.v.gz").write_bytes(b"\x1f\x8b" + b"\x00" * 100)
    texts = ["Innovus Text Command Reference - Introduction",
             "ccopt_design: runs clock concurrent optimization. Properties: target_skew, target_max_trans.",
             "Clock uncertainty after CTS: reduce set_clock_uncertainty to jitter only "
             "because propagated clocks model real skew."]
    (pd / "docs" / "innovus_tcr.pdf").write_bytes(simple_pdf(texts))
    return pd


def simple_pdf(pages):
    """PDF tối giản, mỗi phần tử = 1 trang text (Helvetica)."""
    objs = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append("<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>")
    font_id = 3 + 2 * n
    for i, t in enumerate(pages):
        t = t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 10 Tf 40 750 Td ({t}) Tj ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {4 + 2 * i} 0 R >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = b"%PDF-1.4\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


if __name__ == "__main__":
    print(make(Path(sys.argv[1])))
