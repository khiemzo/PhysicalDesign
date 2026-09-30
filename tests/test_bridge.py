#!/usr/bin/env python3
"""Kiểm thử PD_Bridge với Claude giả lập (0 token).  Chạy: python3 -m unittest tests/test_bridge.py -v"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "PD_Bridge" / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(REPO / "tests"))
import make_fixture  # noqa: E402

DEB = 0.3


class Env:
    def __init__(self, **cfg_over):
        self.tmp = Path(tempfile.mkdtemp(prefix="pdb_"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "OneDrive - Alchip Technologies" / "PD_Bridge"
        for d in ("cau_hoi", "tong_hop", "kien_thuc"):
            (self.root / d).mkdir(parents=True)
        shutil.copy(REPO / "PD_Bridge" / "kien_thuc" / "kinh_nghiem.md", self.root / "kien_thuc")
        self.data = make_fixture.make(self.tmp)
        fake = self.tmp / "claude"
        fake.write_text(f"#!/bin/sh\nexec {sys.executable} {REPO / 'tests' / 'fake_claude.py'} \"$@\"\n")
        fake.chmod(0o755)
        cfg = {"bridge_dir": str(self.root), "data_dir": str(self.data), "claude_cmd": str(fake),
               "debounce_seconds": DEB, "poll_seconds": 0.1}
        cfg.update(cfg_over)
        (self.home / "config.json").write_text(json.dumps(cfg))
        os.environ["PD_BRIDGE_HOME"] = str(self.home)
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-khong-duoc-dung"
        import bridge
        import pd_index
        self.bridge_mod = bridge
        bridge.log.handlers.clear()
        bridge.setup_logging(self.home, verbose=False)
        # chỉ mục
        pd_index.build(self.data, self.data / ".pd_index" / "index.db")
        self.q = self.root / "cau_hoi"

    def new_bridge(self):
        b = self.bridge_mod
        return b.Bridge(b.load_config(), b.State(self.home / "state.json"))

    def script(self, *behs):
        (self.home / "fake_script.json").write_text(json.dumps(list(behs)))

    def auth(self, d):
        (self.home / "fake_auth.json").write_text(json.dumps(d))

    def calls(self):
        p = self.home / "fake_calls.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def ask(self, name, text, age=10):
        p = self.q / name
        p.write_text(text, encoding="utf-8")
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def run(self, b, n=3):
        for _ in range(n):
            b.tick(block=True)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class T(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.b = self.e.new_bridge()
        self.b._weekly_tried = True        # tắt tổng hợp tuần trong các test khác

    def tearDown(self):
        self.e.cleanup()

    def ans(self, qid="Q001"):
        return (self.e.q / f"{qid}_traloi.md").read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    def test_01_new_question_full_flow(self):
        self.e.ask("hold sau cts.md", "mode: nhanh\nVì sao hold âm nhiều sau CTS ở block median_filter? "
                                      "So sánh với flow trong one.tcl.")
        self.e.run(self.b)
        self.assertTrue((self.e.q / "Q001_hold sau cts.md").exists())
        self.assertFalse((self.e.q / "hold sau cts.md").exists())
        a = self.ans()
        self.assertIn("status=done", a)
        self.assertIn("## Tóm tắt", a)
        self.assertIn("danh_gia: chua", a)
        self.assertIn("mode: **nhanh**", a)
        self.assertNotIn("XONG", a)
        c = self.e.calls()
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["model"], "sonnet")                       # nhanh -> sonnet
        self.assertIsNone(c[0]["api_key"], "ANTHROPIC_API_KEY phải bị gỡ")
        args = c[0]["args"]
        self.assertIn(str(self.e.data), args)                          # --add-dir dữ liệu
        dis = args[args.index("--disallowedTools") + 1]
        self.assertIn("Write(//", dis)
        self.assertIn("physical design/**", dis)
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("one.tcl", c[0]["prompt"])                       # gợi ý chỉ mục
        self.assertIn("median_filter_postCTS_hold", c[0]["prompt"])
        self.assertEqual(Path(c[0]["cwd"]).resolve(), self.e.root.resolve())
        j = (self.e.root / "tong_hop" / "nhat_ky.jsonl").read_text()
        self.assertIn('"q": "Q001"', j)

    def test_02_debounce_and_empty(self):
        p = self.e.q / "moi.md"
        p.write_text("")                                               # rỗng
        self.b.tick(block=True)
        self.assertTrue(p.exists())
        p.write_text("câu hỏi đang gõ")                                # vừa sửa -> chờ
        self.b.tick(block=True)
        self.assertTrue(p.exists(), "chưa đủ thời gian ổn định thì chưa nhận")
        time.sleep(DEB + 0.2)
        self.e.run(self.b)
        self.assertFalse(p.exists())
        self.assertIn("status=done", self.ans())

    def test_03_hidden_extension_and_default_mode(self):
        self.e.ask("abc.md.txt", "set_clock_uncertainty sau CTS?")
        self.e.run(self.b)
        self.assertTrue((self.e.q / "Q001_abc.md").exists())
        self.assertEqual(self.e.calls()[0]["model"], "opus")          # chuan -> opus
        self.assertIn("mode: **chuan**", self.ans())

    def test_04_edit_question_reanswer(self):
        self.e.ask("q.md", "mode: chuan\nHold sau CTS?")
        self.e.run(self.b)
        a = self.ans()
        (self.e.q / "Q001_traloi.md").write_text(a.replace("danh_gia: chua", "danh_gia: sai")
                                                 .replace("ghi_chu:", "ghi_chu: thiếu phần OCV"), encoding="utf-8")
        qf = self.e.q / "Q001_q.md"
        qf.write_text("mode: sau\nHold sau CTS? Thêm: ảnh hưởng của OCV/AOCV?", encoding="utf-8")
        self.b.tick(block=True)
        self.assertEqual(len(self.e.calls()), 1, "chưa ổn định -> chưa trả lời lại")
        time.sleep(DEB + 0.2)
        self.e.run(self.b)
        self.assertTrue((self.e.q / "Q001_traloi_cu.md").exists())
        self.assertIn("danh_gia: sai", (self.e.q / "Q001_traloi_cu.md").read_text())
        a2 = self.ans()
        self.assertIn("v=2", a2)
        self.assertIn("mode: **sau**", a2)
        p2 = self.e.calls()[-1]["prompt"]
        self.assertIn("ĐÂY LÀ CÂU HỎI ĐÃ SỬA", p2)
        self.assertIn("thiếu phần OCV", p2)
        self.assertIn("+Hold sau CTS? Thêm", p2)                          # diff
        self.e.run(self.b)
        self.assertEqual(len(self.e.calls()), 2, "không trả lời lặp")
        j = (self.e.root / "tong_hop" / "nhat_ky.jsonl").read_text()
        self.assertIn('"danh_gia": "sai"', j)

    def test_05_rating_refresh(self):
        self.e.ask("q.md", "Hỏi gì đó về CTS")
        self.e.run(self.b)
        a = self.ans().replace("danh_gia: chua", "danh_gia: dung")
        (self.e.q / "Q001_traloi.md").write_text(a, encoding="utf-8")
        self.b.refresh_ratings()
        self.assertEqual(len(self.e.calls()), 1, "sửa file trả lời không được gọi Claude")
        self.e.run(self.b)
        self.assertEqual(len(self.e.calls()), 1)
        j = (self.e.root / "tong_hop" / "nhat_ky.jsonl").read_text()
        self.assertIn('"danh_gia": "dung"', j)

    def test_06_opus_limit_fallback_sonnet(self):
        self.e.script("limit_opus", "ok")
        self.e.ask("q.md", "mode: sau\nCâu hỏi khó về CTS skew")
        self.e.run(self.b)
        c = self.e.calls()
        self.assertEqual([x["model"] for x in c], ["opus", "sonnet"])
        self.assertIn("model: sonnet", self.ans())
        self.assertIn("status=done", self.ans())

    def test_07_plan_limit_pause_and_resume(self):
        self.e.script("limit_all", "limit_all")
        self.e.ask("q.md", "Câu hỏi CTS")
        self.e.run(self.b, 4)
        self.assertEqual(len(self.e.calls()), 2)                          # opus + sonnet, rồi dừng
        self.assertIn("status=waiting", self.ans())
        self.assertIn("Hết hạn mức", self.ans())
        pu = self.b.state.get("paused_until")
        self.assertGreater(pu, time.time() + 7000)
        self.e.ask("q2.md", "Câu hỏi thứ hai")
        self.e.run(self.b)
        self.assertTrue((self.e.q / "Q002_q2.md").exists(), "vẫn nhận câu hỏi khi tạm dừng (0 token)")
        self.assertEqual(len(self.e.calls()), 2, "không gọi Claude khi đang tạm dừng")
        self.b.state.set("paused_until", 0)                              # giả lập đã tới giờ reset
        self.e.run(self.b, 4)
        self.assertIn("status=done", self.ans("Q001"))
        self.assertIn("status=done", self.ans("Q002"))

    def test_08_not_logged_in(self):
        self.e.auth({"loggedIn": False, "authMethod": "none"})
        self.e.ask("q.md", "Câu hỏi")
        self.e.run(self.b)
        self.assertEqual(len(self.e.calls()), 0, "kiểm tra đăng nhập 0 token, không gọi -p")
        self.assertEqual(self.b.state.get("pause_reason"), "chua dang nhap")
        log = (self.e.home / "watcher.log").read_text()
        self.assertIn("chua dang nhap", log)
        self.e.auth({"loggedIn": True, "authMethod": "claude.ai"})
        self.e.script("login")
        self.b.state.set("paused_until", 0)
        self.e.run(self.b)
        self.assertIn("chưa đăng nhập", self.ans())
        self.b.state.set("paused_until", 0)
        self.e.run(self.b)
        self.assertIn("status=done", self.ans())

    def test_09_api_key_auth_blocked(self):
        self.e.auth({"loggedIn": True, "authMethod": "api_key"})
        self.e.ask("q.md", "Câu hỏi")
        self.e.run(self.b)
        self.assertEqual(len(self.e.calls()), 0, "đang dùng API key -> không chạy (tránh tốn tiền)")
        self.assertIn("API", self.b.state.get("pause_reason"))

    def test_10_max_turns_resume(self):
        self.e.script("max_turns", "max_turns")
        self.e.ask("q.md", "Câu hỏi rất dài")
        self.e.run(self.b)
        c = self.e.calls()
        self.assertEqual(len(c), 2)
        self.assertTrue(c[1]["resume"])
        self.assertIn("Viết sau khi hết lượt", self.ans())

    def test_11_errors_then_give_up(self):
        self.e.script("error", "error", "error", "error")
        self.e.ask("q.md", "Câu hỏi")
        for _ in range(3):
            self.e.run(self.b, 2)
            self.b.state.sub("retry").get("Q001", {})["next"] = 0      # bỏ thời gian chờ thử lại
        self.e.run(self.b, 2)
        self.assertEqual(len(self.e.calls()), 3)
        a = self.ans()
        self.assertIn("status=error", a)
        self.assertIn("Không tạo được câu trả lời", a)
        self.e.run(self.b)
        self.assertEqual(len(self.e.calls()), 3, "lỗi hẳn -> không lặp vô hạn")

    def test_12_ok_text_fallback(self):
        self.e.script("ok_text")
        self.e.ask("q.md", "Câu hỏi")
        self.e.run(self.b)
        self.assertIn("Trả lời qua result", self.ans())

    def test_13_retention_and_purge(self):
        for i in range(7):
            self.e.ask(f"c{i}.md", f"Câu hỏi số {i}", age=100 - i)
        self.e.run(self.b, 10)
        names = sorted(p.name for p in self.e.q.glob("Q*_traloi.md"))
        self.assertEqual(names, [f"Q00{i}_traloi.md" for i in range(3, 8)])
        trash = self.e.q / "_cho_xoa"
        self.assertEqual(len(list(trash.rglob("Q*"))), 4)                # Q001,Q002: câu hỏi + trả lời
        self.b.cfg["trash_after_minutes"] = -1
        self.b.purge_trash()
        self.assertFalse(trash.exists())
        self.e.ask("moi.md", "câu hỏi mới")
        self.e.run(self.b)
        self.assertTrue((self.e.q / "Q008_moi.md").exists(), "số thứ tự không bị dùng lại")

    def test_14_editor_resaves_old_name(self):
        self.e.ask("hold.md", "Hold sau CTS?")
        self.e.run(self.b)
        self.e.ask("hold.md", "Hold sau CTS? Thêm câu hỏi phụ.", age=0)   # Notepad lưu lại tên cũ
        self.e.run(self.b)
        self.assertTrue((self.e.q / "hold.md").exists(), "chờ ổn định")
        time.sleep(DEB + 0.2)
        self.e.run(self.b)
        self.assertFalse((self.e.q / "hold.md").exists())
        self.assertFalse((self.e.q / "Q002_hold.md").exists())
        self.assertIn("câu hỏi phụ", (self.e.q / "Q001_hold.md").read_text())
        time.sleep(DEB + 0.2)
        self.e.run(self.b)
        self.assertTrue((self.e.q / "Q001_traloi_cu.md").exists())
        self.assertIn("v=2", self.ans())

    def test_15_chat_claim_finish(self):
        self.e.ask("q.md", "mode: nhanh\nCâu hỏi trong chat")
        r = subprocess.run([sys.executable, str(TOOLS / "bridge.py"), "claim", "--now", "--json"],
                           capture_output=True, text=True, env=os.environ)
        items = json.loads(r.stdout)
        self.assertEqual(items[0]["qid"], "Q001")
        self.assertIn("GỢI Ý TỪ CHỈ MỤC", items[0]["prompt"])
        self.e.run(self.b)
        self.assertEqual(len(self.e.calls()), 0, "watcher bỏ qua câu đang làm trong chat")
        Path(items[0]["answer_tmp"]).write_text("## Tóm tắt\n\n" + "Trả lời trong chat. " * 20, encoding="utf-8")
        r = subprocess.run([sys.executable, str(TOOLS / "bridge.py"), "finish", "Q001"],
                           capture_output=True, text=True, env=os.environ)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("status=done", self.ans())
        self.e.run(self.b)
        self.assertEqual(len(self.e.calls()), 0)

    def test_16_weekly(self):
        self.b._weekly_tried = False
        self.assertTrue(self.b.weekly_due())
        self.b.tick(block=True)                                          # tuần trống -> 0 token
        self.assertEqual(len(self.e.calls()), 0)
        self.assertFalse(self.b.weekly_due())
        self.e.ask("q.md", "Câu hỏi CTS")
        self.e.run(self.b)
        a = self.ans().replace("danh_gia: chua", "danh_gia: dung")
        (self.e.q / "Q001_traloi.md").write_text(a, encoding="utf-8")
        self.e.script("weekly")
        res = self.b.weekly(force=True)
        self.assertEqual(res, "done")
        c = self.e.calls()[-1]
        self.assertEqual(c["model"], "sonnet")
        self.assertIn("danh_gia: dung", c["prompt"])
        self.assertIn("✅ dung 1", c["prompt"])
        wk = list((self.e.root / "tong_hop").glob("*-W*.md"))
        self.assertEqual(len(wk), 1)
        self.assertIn("✅ Sau CTS", (self.e.root / "kien_thuc" / "kinh_nghiem.md").read_text())

    def test_17_weekly_catchup_cutoff(self):
        import datetime as dt
        fri = dt.datetime(2026, 10, 2, 16, 55)                          # thứ Sáu
        c, wid = self.b.weekly_cutoff(dt.datetime(2026, 10, 5, 9, 0).timestamp())   # bật máy thứ Hai
        self.assertEqual(c, fri.timestamp())
        self.assertEqual(wid, "2026-W40")
        c2, _ = self.b.weekly_cutoff(dt.datetime(2026, 10, 2, 16, 50).timestamp())  # trước 16:55
        self.assertEqual(c2, (fri - dt.timedelta(days=7)).timestamp())

    def test_18_parsers(self):
        b = self.e.bridge_mod
        self.assertEqual(b.parse_mode("mode: sâu\nabc", "chuan")[0], "sau")
        self.assertEqual(b.parse_mode("Mode = Nhanh\nabc", "chuan")[0], "nhanh")
        self.assertEqual(b.parse_mode("abc\nmode: chuẩn", "sau")[0], "chuan")
        m, body, raw = b.parse_mode("mode: xyz\nabc", "chuan")
        self.assertEqual((m, body, raw), ("chuan", "abc", "xyz"))
        self.assertEqual(b.display_stem("abc.md.txt"), "abc")
        self.assertEqual(b.display_stem("traloi.md"), "traloi_q")
        self.assertEqual(b.parse_rating("x\ndanh_gia: Đúng\nghi_chu: tốt")[0], "dung")
        self.assertEqual(b.parse_rating("danh_gia: mot_phan")[0], "mot_phan")
        ref = time.mktime((2026, 9, 30, 10, 0, 0, 0, 0, -1))
        r = b.parse_reset("You've hit your limit · resets 3pm", ref)
        self.assertEqual(time.localtime(r - 60)[3:5], (15, 0))
        r = b.parse_reset("5-hour limit reached ∙ resets 9am", ref)
        self.assertEqual(time.localtime(r - 60)[2:5], (1, 9, 0))        # ngày hôm sau
        self.assertAlmostEqual(b.parse_reset("Claude AI usage limit reached|1790000000", ref), 1790000060)
        self.assertEqual(b.classify_failure("Claude AI usage limit reached|1790000000"), "limit")
        self.assertEqual(b.classify_failure("Invalid API key · Please run /login"), "login")
        self.assertEqual(b.classify_failure("API Error: 529 overloaded"), "transient")
        self.assertEqual(b.rule_path(Path("/home/x/physical design")), "//home/x/physical design")

    def test_19_status_and_doctor(self):
        self.e.ask("q.md", "Câu hỏi")
        self.e.run(self.b)
        r = subprocess.run([sys.executable, str(TOOLS / "bridge.py"), "status"], capture_output=True,
                           text=True, env=os.environ)
        self.assertIn("Q001", r.stdout)
        r = subprocess.run([sys.executable, str(TOOLS / "bridge.py"), "doctor"], capture_output=True,
                           text=True, env=os.environ)
        self.assertIn("KẾT QUẢ: OK", r.stdout, r.stdout)

    def test_20_filename_prefix_mode_model(self):
        self.e.ask("[sau-sonnet] hold sau cts.md", "Hold sau CTS?")
        self.e.ask("[nhanh] cu phap.md", "cú pháp set_clock_uncertainty?", age=9)
        self.e.ask("[abc] khong phai tien to.md", "câu hỏi thường", age=8)
        self.e.run(self.b, 6)
        models = [c["model"] for c in self.e.calls()]
        self.assertEqual(models, ["sonnet", "sonnet", "opus"])
        self.assertIn("mode: **sau**", self.ans("Q001"))
        self.assertIn("mode: **nhanh**", self.ans("Q002"))
        self.assertIn("mode: **chuan**", self.ans("Q003"))
        self.assertIn("SÂU", self.e.calls()[0]["prompt"])
        args = self.e.calls()[0]["args"]
        tools = args[args.index("--tools") + 1]
        self.assertIn("WebSearch", tools)
        self.assertNotIn("Agent", tools)
        self.assertIn("--exclude-dynamic-system-prompt-sections", args)

    def test_21_folder_with_attachments(self):
        d = self.e.q / "[nhanh] hold median"
        d.mkdir()
        (d / "cau hoi.md").write_text("Phân tích report hold đính kèm, vì sao âm?", encoding="utf-8")
        src = self.e.data / "median_filter" / "rpt" / "postcts_hold" / "median_filter_postCTS_hold.tarpt"
        shutil.copy(src, d / "hold.tarpt")
        (d / "ghi_chu.txt").write_text("block median_filter, run 0930", encoding="utf-8")
        t = time.time() - 10
        for f in [d, *d.iterdir()]:
            os.utime(f, (t, t))
        self.e.run(self.b)
        self.assertTrue((self.e.q / "Q001_[nhanh] hold median.md").exists())
        gui = self.e.root / "du_lieu_gui" / "Q001_[nhanh] hold median"
        self.assertTrue((gui / "hold.tarpt").exists())
        self.assertFalse(d.exists())
        p = self.e.calls()[0]["prompt"]
        self.assertIn("DỮ LIỆU NGƯỜI HỎI GỬI KÈM", p)
        self.assertIn("WNS=-0.0870", p)                         # tóm tắt tự động report
        self.assertIn("run 0930", p)                            # file nhỏ đưa thẳng vào
        self.assertIn(str(gui), self.e.calls()[0]["args"])       # --add-dir
        self.assertEqual(self.e.calls()[0]["model"], "sonnet")

    def test_22_same_stem_attachment(self):
        self.e.ask("hold.md", "Xem log đính kèm")
        (self.e.q / "hold_innovus.log").write_text("**ERROR: (IMPCCOPT-1209): skew\n" * 3)
        self.e.run(self.b)
        self.assertTrue((self.e.root / "du_lieu_gui" / "Q001_hold" / "hold_innovus.log").exists())
        self.assertIn("IMPCCOPT-1209", self.e.calls()[0]["prompt"])

    def test_23_memory_history_and_sent_data(self):
        d = self.e.q / "hold median"
        d.mkdir()
        (d / "cau hoi.md").write_text("Vì sao hold median_filter âm sau CTS? xem report", encoding="utf-8")
        shutil.copy(self.e.data / "median_filter" / "rpt" / "postcts_hold" / "median_filter_postCTS_hold.tarpt",
                    d / "hold_0930.tarpt")
        t = time.time() - 10
        for f in [d, *d.iterdir()]:
            os.utime(f, (t, t))
        self.e.run(self.b)
        a = self.ans("Q001").replace("danh_gia: chua", "danh_gia: dung")
        (self.e.q / "Q001_traloi.md").write_text(a, encoding="utf-8")
        self.b.refresh_ratings()
        cat = (self.e.root / "du_lieu_gui" / "MUC_LUC.md").read_text(encoding="utf-8")
        self.assertIn("hold_0930.tarpt", cat)
        self.assertIn("WNS", cat)
        self.e.ask("q2.md", "Hold median_filter sau CTS vẫn âm, so sánh với report tuần trước (VIOLATED reg2reg)")
        self.e.run(self.b)
        p = self.e.calls()[-1]["prompt"]
        self.assertIn("CÂU HỎI CŨ LIÊN QUAN", p)
        self.assertIn("Q001", p)
        self.assertIn("✅ đã xác nhận đúng", p)
        self.assertIn("DỮ LIỆU NGƯỜI HỎI ĐÃ GỬI TRƯỚC ĐÂY", p)
        self.assertIn("hold_0930.tarpt", p)
        self.assertIn(str(self.e.root / "du_lieu_gui"), self.e.calls()[-1]["args"])
        # dọn lượt cũ không xoá dữ liệu đã gửi
        self.b.cfg["keep_rounds"] = 0
        _, rounds = self.b.scan()
        self.b.retention(rounds)
        self.assertTrue((self.e.root / "du_lieu_gui" / "Q001_hold median" / "hold_0930.tarpt").exists())

    def test_24_wrong_answers_not_reused(self):
        self.e.ask("q.md", "clock gating check setup hold ICG")
        self.e.run(self.b)
        a = self.ans().replace("danh_gia: chua", "danh_gia: sai")
        (self.e.q / "Q001_traloi.md").write_text(a, encoding="utf-8")
        self.b.refresh_ratings()
        self.e.ask("q2.md", "clock gating check ICG setup hold là gì")
        self.e.run(self.b)
        self.assertNotIn("CÂU HỎI CŨ LIÊN QUAN", self.e.calls()[-1]["prompt"])


class TWatch(unittest.TestCase):
    """Chạy watcher thật (process riêng) — kiểm tra phát hiện file mới + dừng."""

    def test_watch_process(self):
        e = Env(debounce_seconds=1)
        try:
            e.ask("hold sau cts.md", "mode: nhanh\nHold sau CTS?", age=0)
            p = subprocess.Popen([sys.executable, str(TOOLS / "bridge.py"), "watch"], env=os.environ,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            t0 = time.time()
            while time.time() - t0 < 30:
                a = e.q / "Q001_traloi.md"
                if a.exists() and "status=done" in a.read_text():
                    break
                time.sleep(0.3)
            dt_ = time.time() - t0
            self.assertIn("status=done", (e.q / "Q001_traloi.md").read_text())
            self.assertGreaterEqual(dt_, 0.9, "phải chờ file ổn định")
            print(f"\n  [đo] thả file -> câu trả lời (claude giả): {dt_:.1f}s")
            p2 = subprocess.run([sys.executable, str(TOOLS / "bridge.py"), "watch"], env=os.environ,
                                capture_output=True, text=True, timeout=20)
            self.assertEqual(p2.returncode, 0)                           # instance thứ 2 thoát ngay
            subprocess.run([sys.executable, str(TOOLS / "bridge.py"), "stop"], env=os.environ)
            p.wait(timeout=20)
            self.assertEqual(p.returncode, 0)
            log = (e.home / "watcher.log").read_text()
            self.assertIn("Câu hỏi mới", log)
            self.assertIn("Watcher khác đang chạy", log)
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TLatency(unittest.TestCase):
    """Đo độ trễ thật với cấu hình mặc định (quét 1s, ổn định 2s)."""

    def test_latency_under_10s(self):
        e = Env(debounce_seconds=2, poll_seconds=1)
        try:
            p = subprocess.Popen([sys.executable, str(TOOLS / "bridge.py"), "watch"], env=os.environ,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
            res = []
            for i in range(3):
                t0 = time.time()
                (e.q / f"cau {i}.md").write_text(f"câu hỏi số {i}", encoding="utf-8")
                qid = f"Q{i + 1:03d}"
                t_ack = t_start = None
                while time.time() - t0 < 20:
                    a = e.q / f"{qid}_traloi.md"
                    if t_ack is None and a.exists():
                        t_ack = time.time() - t0
                    if t_start is None and len(e.calls()) > i:
                        t_start = time.time() - t0
                    if a.exists() and "status=done" in a.read_text():
                        break
                    time.sleep(0.1)
                res.append((t_ack, t_start))
            subprocess.run([sys.executable, str(TOOLS / "bridge.py"), "stop"], env=os.environ)
            p.wait(timeout=20)
            for i, (a, s) in enumerate(res):
                print(f"\n  [đo] câu {i}: xác nhận 'Đã nhận' sau {a:.1f}s, Claude bắt đầu sau {s:.1f}s")
                self.assertLess(a, 5)
                self.assertLess(s, 10)
        finally:
            e.cleanup()
