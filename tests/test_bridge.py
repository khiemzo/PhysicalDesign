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
        self.kho = self.tmp / "kho"
        cfg = {"bridge_dir": str(self.root), "data_dir": str(self.data), "claude_cmd": str(fake),
               "store_dir": str(self.kho),
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
        self.assertIn("mode **nhanh**", a)
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
        self.assertIn("mode **chuan**", self.ans())

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
        self.assertIn("mode **sau**", a2)
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
        self.assertIn("model sonnet", self.ans())
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
        self.assertIn("mode **sau**", self.ans("Q001"))
        self.assertIn("mode **nhanh**", self.ans("Q002"))
        self.assertIn("mode **chuan**", self.ans("Q003"))
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
        gui = self.e.kho / "du_an" / "chung" / "du_lieu" / "Q001_[nhanh] hold median"
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
        self.assertTrue((self.e.kho / "du_an" / "chung" / "du_lieu" / "Q001_hold" / "hold_innovus.log").exists())
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
        cat = (self.e.kho / "du_an" / "median_filter" / "DONG_THOI_GIAN.md").read_text(encoding="utf-8")
        self.assertIn("hold_0930.tarpt", cat)
        self.assertIn("WNS", cat)
        self.e.ask("q2.md", "Hold median_filter sau CTS vẫn âm, so sánh với report tuần trước (VIOLATED reg2reg)")
        self.e.run(self.b)
        p = self.e.calls()[-1]["prompt"]
        self.assertIn("CÂU HỎI CŨ LIÊN QUAN", p)
        self.assertIn("Q001", p)
        self.assertIn("✅ đã xác nhận đúng", p)
        self.assertIn("TRONG KHO ĐÃ CÓ", p)
        self.assertIn("hold_0930.tarpt", p)
        self.assertIn(str(self.e.kho / "du_an"), self.e.calls()[-1]["args"])
        self.assertIn("PROJECT: median_filter", p)
        # dọn lượt cũ không xoá dữ liệu đã gửi
        self.b.cfg["keep_rounds"] = 0
        _, rounds = self.b.scan()
        self.b.retention(rounds)
        self.assertTrue((self.e.kho / "du_an" / "median_filter" / "du_lieu" / "Q001_hold median" / "hold_0930.tarpt").exists())
        self.assertTrue((self.e.kho / "du_an" / "median_filter" / "hoi_dap" / "Q001_traloi.md").exists(),
                        "kho giữ vĩnh viễn dù OneDrive đã dọn")

    def test_24_wrong_answers_not_reused(self):
        self.e.ask("q.md", "clock gating check setup hold ICG")
        self.e.run(self.b)
        a = self.ans().replace("danh_gia: chua", "danh_gia: sai")
        (self.e.q / "Q001_traloi.md").write_text(a, encoding="utf-8")
        self.b.refresh_ratings()
        self.e.ask("q2.md", "clock gating check ICG setup hold là gì")
        self.e.run(self.b)
        self.assertNotIn("CÂU HỎI CŨ LIÊN QUAN", self.e.calls()[-1]["prompt"])

    def test_25_stall_killed_and_retried(self):
        self.b.cfg["stall_minutes"] = 0.05          # 3 giây cho test
        self.e.script("hang", "ok")
        self.e.ask("q.md", "Câu hỏi")
        t0 = time.time()
        self.e.run(self.b, 2)
        self.assertLess(time.time() - t0, 15, "phải dừng lượt treo nhanh")
        self.assertIn("treo", self.b.last_error)
        self.b.state.sub("retry")["Q001"]["next"] = 0
        self.e.run(self.b)
        self.assertIn("status=done", self.ans())
        self.assertIn("stream-json", self.e.calls()[0]["args"])

    def test_26_progress_and_status_file(self):
        self.e.script("slow_ok")
        seen = []
        orig = self.b.write_placeholder

        def spy(*a, **k):
            seen.append(a[5])
            return orig(*a, **k)
        self.b.write_placeholder = spy
        self.b.runner_progress_every = 0
        self.e.ask("q.md", "Câu hỏi")
        self.e.run(self.b)
        self.assertIn("status=done", self.ans())
        self.b.write_status(force=True)
        st = (self.e.q / "_TRANG_THAI.md").read_text(encoding="utf-8")
        self.assertIn("đã đăng nhập", st)
        self.assertIn("Nhật ký gần nhất", st)
        self.e.ask("q2.md", "Câu hỏi 2")
        self.e.run(self.b)
        self.assertFalse((self.e.q / "Q003__TRANG_THAI.md").exists(), "file trạng thái không bị coi là câu hỏi")

    def test_27_preflight_error_written_to_answer(self):
        self.e.auth({"loggedIn": False})
        self.e.ask("q.md", "Câu hỏi")
        self.e.run(self.b)
        a = self.ans()
        self.assertIn("CHƯA ĐĂNG NHẬP", a)
        self.assertIn("claude auth login", a)
        self.assertIn("CHƯA ĐĂNG NHẬP", (self.e.q / "_TRANG_THAI.md").read_text(encoding="utf-8"))

    def test_28_old_cli_compat(self):
        self.e.script("old_cli", "old_cli")
        self.e.ask("q.md", "Câu hỏi")
        self.e.run(self.b)
        self.assertIn("status=done", self.ans())
        self.assertNotIn("--tools", self.e.calls()[-1]["args"])

    def test_29_answer_written_wrong_place(self):
        self.e.script("wrong_place")
        self.e.ask("q.md", "Câu hỏi")
        self.e.run(self.b)
        self.assertIn("Ghi nhầm chỗ", self.ans())

    def test_30_forgot_to_write_file_resumed(self):
        self.e.script("no_file", "no_file")
        self.e.ask("q.md", "Câu hỏi")
        self.e.run(self.b)
        self.assertIn("Ghi sau khi được nhắc", self.ans())
        self.assertTrue(self.e.calls()[-1]["resume"])

    def test_31_internal_exception_not_stuck(self):
        def boom(*a, **k):
            raise RuntimeError("lỗi giả lập")
        self.b.build_prompt = boom
        self.e.ask("q.md", "Câu hỏi")
        for _ in range(4):
            self.e.run(self.b, 1)
            rt = self.b.state.sub("retry").get("Q001")
            if rt:
                rt["next"] = 0
        a = self.ans()
        self.assertIn("status=error", a)
        self.assertIn("lỗi giả lập", a)

    def test_32_transient_errors_retry_more(self):
        self.e.script(*(["hang"] * 4 + ["ok"]))
        self.b.cfg["stall_minutes"] = 0.03
        self.e.ask("q.md", "Câu hỏi")
        for _ in range(6):
            self.e.run(self.b, 1)
            rt = self.b.state.sub("retry").get("Q001")
            if rt:
                rt["next"] = 0
        self.assertIn("status=done", self.ans())

    def test_33_project_archive_and_index(self):
        self.e.ask("hold.md", "Hold âm ở block median_filter sau CTS?")
        self.e.ask("@pcie_top clock.md", "clock gating của pcie?", age=9)
        self.e.ask("chung.md", "STA là gì?", age=8)
        self.e.run(self.b, 6)
        pr = self.b.state.sub("proj")
        self.assertEqual((pr["Q001"], pr["Q002"], pr["Q003"]), ("median_filter", "pcie_top", "chung"))
        hd = self.e.kho / "du_an" / "median_filter" / "hoi_dap"
        self.assertTrue((hd / "Q001_hold.md").exists())
        self.assertIn("status=done", (hd / "Q001_traloi.md").read_text(encoding="utf-8"))
        ml = (self.e.kho / "du_an" / "median_filter" / "MUC_LUC.md").read_text(encoding="utf-8")
        self.assertIn("| Q001 |", ml)
        self.assertTrue((self.e.kho / "du_an" / "median_filter" / "BOI_CANH.md").exists())
        self.assertIn(str(self.e.data / "median_filter"), (self.e.kho / "du_an" / "median_filter" / "BOI_CANH.md").read_text())
        # sửa câu hỏi -> bản cũ được giữ trong kho
        (self.e.q / "Q001_hold.md").write_text("Hold âm ở block median_filter sau CTS? thêm OCV", encoding="utf-8")
        self.b.tick(block=True)
        time.sleep(DEB + 0.2)
        self.e.run(self.b)
        self.assertTrue((hd / "Q001_traloi_v1.md").exists())
        self.assertIn("v=2", (hd / "Q001_traloi.md").read_text(encoding="utf-8"))

    def test_34_followup_in_answer_file(self):
        self.e.ask("q.md", "Hold sau CTS?")
        self.e.run(self.b)
        a = self.e.q / "Q001_traloi.md"
        t = a.read_text(encoding="utf-8").replace("danh_gia: chua", "danh_gia: dung")
        a.write_text(t + "\n>> Vậy với OCV thì sao?\n", encoding="utf-8")
        self.e.run(self.b)
        self.assertEqual(len(self.e.calls()), 1, "chờ file ổn định")
        time.sleep(DEB + 0.2)
        self.e.run(self.b)
        c = self.e.calls()
        self.assertEqual(len(c), 2)
        self.assertIn("HỎI TIẾP", c[1]["prompt"])
        self.assertIn("Vậy với OCV thì sao?", c[1]["prompt"])
        self.assertTrue(c[1]["resume"], "tiếp tục phiên cũ (rẻ hơn)")
        t2 = a.read_text(encoding="utf-8")
        self.assertIn("## ❓ Hỏi tiếp", t2)
        self.assertIn("> Vậy với OCV thì sao?", t2)
        self.assertNotIn("\n>> ", t2)
        self.assertIn("danh_gia: dung", t2, "giữ đánh giá người hỏi")
        self.assertLess(t2.index("## ❓ Hỏi tiếp"), t2.index("📜 Các lượt trước (1)"), "lượt mới ở đầu")
        self.assertLess(t2.index("<details>"), t2.index("### Lượt 1 · Câu hỏi gốc"), "lượt cũ được thu gọn")
        self.assertLess(t2.index("📜 Các lượt trước"), t2.index("**Đánh giá**"))
        self.assertIn("CHUỖI HỎI ĐÁP TRƯỚC", c[1]["prompt"])
        self.assertIn("[Lượt 1] Hỏi: Hold sau CTS?", c[1]["prompt"])
        self.e.run(self.b)
        self.assertEqual(len(self.e.calls()), 2, "không trả lời lặp")
        kho = (self.e.kho / "du_an" / "chung" / "hoi_dap" / "Q001_traloi.md").read_text(encoding="utf-8")
        self.assertIn("## ❓ Hỏi tiếp", kho)
        # hỏi tiếp lần 2 bằng ký hiệu » (bàn phím tiếng Việt hay tự đổi >> thành »)
        a.write_text(a.read_text(encoding="utf-8") + "\n» Còn POCV?\n", encoding="utf-8")
        self.b.tick(block=True)
        time.sleep(DEB + 0.2)
        self.e.run(self.b)
        t3 = a.read_text(encoding="utf-8")
        self.assertIn("> Còn POCV?", t3)
        a.write_text(t3 + "\n>> [nhanh] Tóm tắt lại 3 ý?\n", encoding="utf-8")
        self.b.tick(block=True)
        time.sleep(DEB + 0.2)
        self.e.run(self.b)
        self.assertEqual(self.e.calls()[-1]["model"], "sonnet", ">> [nhanh] dùng mode nhanh")
        self.assertIn("> Tóm tắt lại 3 ý?", a.read_text(encoding="utf-8"))
        self.assertIn("📜 Các lượt trước (2)", t3)
        self.assertLess(t3.index("> Còn POCV?"), t3.index("> Vậy với OCV thì sao?"))

    def test_35_lesson_notebook_and_verification(self):
        self.e.script("lesson")
        self.e.ask("q.md", "CTS và CPPR?")
        self.e.run(self.b)
        a = self.ans()
        self.assertIn("Kiểm chứng tự động", a)
        self.assertIn("`ccopt_design` | 📘", a)
        self.assertIn("`fake_cmd_xyz` | ⚠️", a)
        sl = (self.e.root / "so_tay" / "so_lenh.md").read_text(encoding="utf-8")
        self.assertIn("ccopt_design", sl)
        self.assertIn("◻️ 📘 `ccopt_design`", sl)
        self.assertIn("⚠️ `fake_cmd_xyz", sl)
        tn = (self.e.root / "so_tay" / "thuat_ngu.md").read_text(encoding="utf-8")
        self.assertIn("**CPPR", tn)
        (self.e.q / "Q001_traloi.md").write_text(a.replace("danh_gia: chua", "danh_gia: dung"), encoding="utf-8")
        self.b.refresh_ratings()
        self.assertIn("✅ 📘 `ccopt_design`", (self.e.root / "so_tay" / "so_lenh.md").read_text(encoding="utf-8"))
        self.assertIn("✅ **CPPR", (self.e.root / "so_tay" / "thuat_ngu.md").read_text(encoding="utf-8"))

    def test_36_auto_learn_after_14_days(self):
        self.e.ask("q.md", "Hold block median_filter sau CTS?")
        self.e.run(self.b)
        kt = self.e.kho / "du_an" / "median_filter" / "KIEN_THUC.md"
        self.b.store.auto_learn(self.b.read_journal(), time.time())
        self.assertNotIn("◻️ (Q001", kt.read_text(encoding="utf-8"), "chưa đủ 14 ngày")
        self.b.store.auto_learn(self.b.read_journal(), time.time() + 15 * 86400)
        self.assertIn("◻️ (Q001", kt.read_text(encoding="utf-8"))
        a = self.ans().replace("danh_gia: chua", "danh_gia: sai")
        (self.e.q / "Q001_traloi.md").write_text(a, encoding="utf-8")
        self.b.refresh_ratings()
        self.b.store.auto_learn(self.b.read_journal(), time.time() + 15 * 86400)
        self.assertNotIn("◻️ (Q001", kt.read_text(encoding="utf-8"), "bị đánh giá sai -> bỏ")
        self.e.ask("q2.md", "median_filter hold CTS uncertainty?")
        self.b.state.sub("ratings").clear()

    def test_37_synonyms_and_block_search(self):
        import pd_index
        t = pd_index.extract_terms("độ lệch xung nhịp, nhiễu xuyên âm và OCV")
        for w in ("skew", "clock", "crosstalk", "aocv", "derate"):
            self.assertIn(w, t)
        self.e.ask("q.md", "Vì sao CTS của median_filter bị lệch nhiều?")
        self.e.run(self.b)
        p = self.e.calls()[0]["prompt"]
        self.assertIn("ccopt", p.split("GỢI Ý TỪ CHỈ MỤC")[1][:3000].lower())

    def test_38_followup_pending_survives_restart_and_typing(self):
        self.e.ask("q.md", "Hold sau CTS?")
        self.e.run(self.b)
        a = self.e.q / "Q001_traloi.md"
        # giả lập: đã nhận hỏi tiếp rồi máy tắt (câu hỏi nằm trong state, dòng >> đã bị gỡ khỏi file)
        self.b.state.sub("fu_pending")["Q001"] = "Câu hỏi tiếp trước khi tắt máy"
        self.b.state.save()
        b2 = self.e.new_bridge()
        b2._weekly_tried = True
        self.e.script("slow_ok", "ok")
        b2.run_once(False)
        t = a.read_text(encoding="utf-8")
        self.assertIn("> Câu hỏi tiếp trước khi tắt máy", t)
        self.assertNotIn("Q001", b2.state.sub("fu_pending"))
        # dòng >> người hỏi gõ trong lúc đang trả lời không bị ghi đè
        cur = {"qid": "Q001", "stem": "q", "v": 1, "mode": "chuan", "model": "opus",
               "question": "đang hỏi", "pending_kind": "followup"}
        a.write_text(a.read_text(encoding="utf-8") + "\n>> câu gõ thêm lúc đang chạy\n", encoding="utf-8")
        b2.show_progress(cur, {"t0": time.time(), "steps": 2, "recent": ["Grep x"]}, None)
        t = a.read_text(encoding="utf-8")
        self.assertIn(">> câu gõ thêm lúc đang chạy", t)
        self.assertIn("⏳ **Đang trả lời**", t)
        self.assertLess(t.index("Đang trả lời"), t.index("📜 Các lượt trước"))

    def test_39_draft_shown_while_writing(self):
        tmp = self.e.tmp / "draft.md"
        tmp.write_text("## ✅ Kết luận\n\nĐang viết dở phần kết luận…", encoding="utf-8")
        self.e.ask("q.md", "câu hỏi")
        self.b.tick(block=False) if False else None
        cur = {"qid": "Q009", "stem": "x", "v": 1, "mode": "chuan", "model": "opus", "question": "x"}
        self.b.show_progress(cur, {"t0": time.time() - 65, "steps": 3, "recent": ["Read one.tcl"]}, tmp)
        t = (self.e.q / "Q009_traloi.md").read_text(encoding="utf-8")
        self.assertIn("1 phút 05 giây", t)
        self.assertIn("Bản nháp", t)
        self.assertIn("Đang viết dở phần kết luận", t)
        self.assertIn("status=running", t)

    def test_40_two_questions_in_parallel(self):
        self.e.script("slow_ok", "slow_ok")
        self.e.ask("[sau] a.md", "câu sâu", age=10)
        self.e.ask("[nhanh] b.md", "câu nhanh", age=9)
        t0 = time.time()
        for _ in range(60):
            self.b.tick()
            if not self.b.busy() and len(self.e.calls()) >= 2:
                break
            time.sleep(0.1)
        for _ in range(100):
            if not self.b.busy():
                break
            time.sleep(0.1)
        self.assertIn("status=done", self.ans("Q001"))
        self.assertIn("status=done", self.ans("Q002"))
        self.assertLess(time.time() - t0, 3.2, "2 câu chạy song song (mỗi câu ~1.5s)")


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
