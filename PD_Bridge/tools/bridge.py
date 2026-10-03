#!/usr/bin/env python3
"""bridge.py - bộ điều phối PD_Bridge (hỏi đáp Physical Design tự động).

Chạy trên máy ngoài (watcher.ps1 gọi `bridge.py watch`). Khi không có câu hỏi,
mọi việc đều là script cục bộ: 0 token. Mỗi câu hỏi = một lần `claude -p`
(tính vào hạn mức gói Claude, KHÔNG dùng API key).

Lệnh:
  watch                 vòng lặp chính (phát hiện câu hỏi, gọi Claude, dọn dẹp, tổng hợp tuần)
  once [--now]          chạy một vòng rồi thoát (--now: bỏ qua thời gian chờ 1 phút)
  status                trạng thái hàng đợi / hạn mức / chỉ mục
  doctor                kiểm tra cài đặt (0 token)
  claim [--now]         (chế độ chat "làm") nhận câu hỏi đang chờ, in prompt
  finish Qnnn [--model] (chế độ chat) hoàn tất câu trả lời đã ghi
  weekly [--force]      tổng hợp tuần ngay
  stop                  yêu cầu watcher dừng
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
import pd_index  # noqa: E402
import pd_summarize  # noqa: E402
import pd_store  # noqa: E402

IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0

MODES = ("nhanh", "chuan", "sau")
MODE_ALIASES = {
    "nhanh": "nhanh", "nhan": "nhanh", "fast": "nhanh", "quick": "nhanh", "n": "nhanh",
    "chuan": "chuan", "standard": "chuan", "normal": "chuan", "c": "chuan", "tb": "chuan",
    "sau": "sau", "deep": "sau", "ky": "sau", "s": "sau",
}
DEFAULTS = {
    "bridge_dir": None,          # mặc định: thư mục cha của tools/
    "data_dir": None,            # thư mục "physical design"
    "index_db": None,            # mặc định <data_dir>/.pd_index/index.db
    "claude_cmd": None,          # tự dò
    "python": None,
    "runner": "local",           # local | routine
    "routine": {"url": "", "token": "", "headers": {}},
    "models": {"nhanh": "sonnet", "chuan": "opus", "sau": "opus", "tong_hop": "sonnet",
               "fallback": "sonnet"},
    "effort": {"nhanh": "high", "chuan": "xhigh", "sau": "max", "tong_hop": "low"},
    "max_turns": {"nhanh": 50, "chuan": 120, "sau": 300, "tong_hop": 40},
    "timeout_min": {"nhanh": 25, "chuan": 60, "sau": 150, "tong_hop": 30},
    "default_mode": "chuan",
    "debounce_seconds": 2,       # file ngừng thay đổi 2 giây là nhận
    "poll_seconds": 1,           # quét thư mục câu hỏi mỗi giây (0 token)
    "keep_rounds": 5,
    "trash_after_minutes": 60,
    "index_refresh_hours": 24,
    "weekly": {"weekday": 4, "time": "16:55"},   # 0=Thứ Hai ... 4=Thứ Sáu
    "allow_api_key": False,      # False: xoá ANTHROPIC_API_KEY khỏi môi trường -> không tốn tiền API
    "max_attempts": 3,               # lỗi thật (không rõ nguyên nhân)
    "max_attempts_transient": 8,     # lỗi mạng/treo/quá tải: thử nhiều hơn, chờ tối đa 10 phút/lần
    "progress_seconds": 10,          # cập nhật tiến độ vào file trả lời
    "auto_update_cli": True,         # mỗi ngày chạy `claude update` khi rảnh (0 token)
    "chat_lock_hours": 3,
    "prevent_sleep": True,
    "pre_search_hits": 12,
    "attach_inline_chars": 14000,    # tổng ký tự file đính kèm đưa thẳng vào đề bài
    "stall_minutes": 5,
    "store_dir": None,               # kho project trên máy ngoài (mặc định %USERPROFILE%\\PD_Bridge_Kho)
    "auto_learn_days": 14,           # câu trả lời chưa bị đánh giá sai sau N ngày -> ◻️ chưa xác nhận
    "followup_max_turns": 60,
    "max_workers": 2,
    "use_history": False,            # False: KHÔNG đưa hỏi đáp cũ của câu khác vào đề bài (mỗi câu trả lời mới hoàn toàn)
    "review_modes": ["chuan", "sau"],  # lượt rà soát + hoàn thiện sau khi viết xong
    "review_max_turns": 40,
    "doc_excerpts": 3,               # đưa sẵn nội dung N đoạn tài liệu tool khớp nhất vào đề bài
    "draft_chars": 8000,             # hiện bản nháp câu trả lời trong lúc Claude đang viết
    "idle_poll_seconds": 3,          # rảnh > 10 phút: quét thưa hơn (đỡ tốn tài nguyên)
    "idle_stop_minutes": 0,          # >0: rảnh liên tục N phút thì tự TẮT (bật lại bằng pdbat); 0 = không tự tắt
}
MODEL_NAMES = {"opus", "sonnet", "haiku", "fable"}

Q_RX = re.compile(r"^(Q\d{3,})_(.+)\.md$", re.I)
ANS_RX = re.compile(r"^(Q\d{3,})_traloi\.md$", re.I)
OLD_RX = re.compile(r"^(Q\d{3,})_traloi_cu\.md$", re.I)
MARK_RX = re.compile(r"<!--\s*pd_bridge\s+(.*?)-->", re.S)
RATING_RX = re.compile(r"^\s*danh_gia\s*:\s*([^\s<]*)", re.I | re.M)
NOTE_RX = re.compile(r"^\s*ghi_chu\s*:\s*(.*)$", re.I | re.M)
RATING_ALIASES = {"dung": "dung", "đúng": "dung", "ok": "dung", "true": "dung",
                  "mot_phan": "mot_phan", "một_phần": "mot_phan", "motphan": "mot_phan",
                  "partial": "mot_phan", "sai": "sai", "wrong": "sai", "chua": "chua"}

log = logging.getLogger("pd_bridge")


# ============================================================================ utils
def home_dir() -> Path:
    return pd_index._bridge_home()


def now() -> float:
    return time.time()


def ts_str(t: float | None = None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return dt.datetime.fromtimestamp(t if t is not None else now()).strftime(fmt)


def strip_accents(s: str) -> str:
    return pd_index.strip_accents(s)


_READ_CACHE: dict[str, tuple] = {}


def read_text(p: Path) -> str:
    """Đọc file (có cache theo size/mtime để quét mỗi giây không tốn I/O)."""
    st = p.stat()
    key = str(p)
    c = _READ_CACHE.get(key)
    if c and c[0] == st.st_size and c[1] == st.st_mtime_ns:
        return c[2]
    t = pd_index.decode_bytes(p.read_bytes())
    if len(_READ_CACHE) > 400:
        _READ_CACHE.clear()
    _READ_CACHE[key] = (st.st_size, st.st_mtime_ns, t)
    return t


def norm_text(t: str) -> str:
    t = t.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    t = unicodedata.normalize("NFC", t)
    return "\n".join(l.rstrip() for l in t.split("\n")).strip()


def text_hash(t: str) -> str:
    return hashlib.sha1(norm_text(t).encode("utf-8")).hexdigest()[:12]


def atomic_write(p: Path, text: str) -> None:
    tmp = p.with_name("." + p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    for i in range(10):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.5 * (i + 1))
    os.replace(tmp, p)


def parse_marker(text: str) -> dict:
    ms = MARK_RX.findall(text)
    if not ms:
        return {}
    d = {}
    for m in re.finditer(r"(\w+)=(\"[^\"]*\"|\S+)", ms[-1]):
        d[m.group(1)] = m.group(2).strip('"')
    return d


def parse_mode(text: str, default: str) -> tuple[str, str, str | None]:
    """-> (mode, question_body_without_mode_line, raw_mode_if_unknown)."""
    lines = norm_text(text).split("\n")
    for i, line in enumerate(lines[:6]):
        m = re.match(r"^\s*(?:[-*#>]\s*)?(?:mode|che do|chế độ)\s*[:=]\s*`?([^\s`]+)", line, re.I)
        if m:
            raw = strip_accents(m.group(1)).lower().strip(".,;")
            mode = MODE_ALIASES.get(raw)
            body = "\n".join(lines[:i] + lines[i + 1:]).strip()
            return (mode or default), body, (None if mode else m.group(1))
    return default, "\n".join(lines).strip(), None


PREFIX_RX = re.compile(r"^\s*[\[(]\s*([^\])]+?)\s*[\])]\s*(.*)$")


def parse_prefix(stem: str) -> tuple[str | None, str | None, str]:
    """'[sau-sonnet] hold sau cts' -> ('sau', 'sonnet', 'hold sau cts'). Không có tiền tố -> (None, None, stem)."""
    m = PREFIX_RX.match(stem)
    if not m:
        return None, None, stem
    mode = model = None
    for tok in re.split(r"[\s,;/+_\-]+", strip_accents(m.group(1)).lower()):
        if not tok:
            continue
        if tok in MODE_ALIASES and mode is None:
            mode = MODE_ALIASES[tok]
        elif tok in MODEL_NAMES and model is None:
            model = tok
        else:
            return None, None, stem          # không phải tiền tố quy tắc -> giữ nguyên tên
    return mode, model, (m.group(2).strip() or stem)


def parse_rating(text: str) -> tuple[str, str]:
    r = RATING_RX.findall(text)
    n = NOTE_RX.findall(text)
    raw = (r[-1] if r else "chua").strip().strip("`*").lower()
    rating = RATING_ALIASES.get(raw, RATING_ALIASES.get(strip_accents(raw), raw or "chua"))
    note = (n[-1] if n else "").strip()
    if note.startswith("<!--"):
        note = ""
    return rating, note


def display_stem(fname: str) -> str:
    s = fname
    for _ in range(3):
        low = s.lower()
        for ext in (".md", ".txt", ".markdown"):
            if low.endswith(ext):
                s = s[: -len(ext)]
                break
        else:
            break
    s = s.strip() or "cau_hoi"
    if s.lower() in ("traloi", "traloi_cu"):
        s += "_q"
    return re.sub(r'[<>:"/\\|?*]', "_", s)


FU_LINE = re.compile(r"^\s*(?:>>|»|＞＞)\s?(.*\S.*)$")


def _outside_fences(text: str):
    fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        yield line, fence


def parse_followups(text: str) -> str:
    """Các dòng bắt đầu bằng '>>' (ngoài code block) = câu hỏi tiếp chưa trả lời."""
    out = []
    for line, fence in _outside_fences(text):
        m = FU_LINE.match(line)
        if m and not fence:
            out.append(m.group(1).strip())
    return "\n".join(out).strip()


def strip_followups(text: str, mark: str | None = None) -> str:
    lines = []
    for line, fence in _outside_fences(text):
        m = FU_LINE.match(line)
        if m and not fence:
            if mark:
                lines.append(mark + m.group(1).strip())
            continue
        lines.append(line)
    return "\n".join(lines)


def escape_followups(body: str) -> str:
    """Dòng '>>' do Claude viết (ngoài code block) -> '\\>>' để không bị hiểu nhầm là câu hỏi tiếp."""
    lines = []
    for line, fence in _outside_fences(body):
        lines.append(re.sub(r"^(\s*)(>>|»)", r"\1\\\2", line) if not fence else line)
    return "\n".join(lines)


def splice_followup(text: str, question: str, body: str, when: str) -> str:
    """Chèn '## 🔁 Hỏi tiếp' trước phần đánh giá ở cuối file trả lời."""
    i = text.rfind("\n---\n\n**Đánh giá**")
    if i < 0:
        i = len(text)
    q = "\n".join("> " + l for l in question.splitlines())
    sec = f"\n\n## 🔁 Hỏi tiếp ({when})\n\n{q}\n\n{body.strip()}\n"
    return text[:i].rstrip() + sec + text[i:]


def summary_section(answer: str, limit: int = 1200) -> str:
    m = re.search(r"^##\s*(?:✅\s*)?(?:Tóm tắt|Kết luận)[^\n]*$(.*?)(?=^##\s|\Z)", answer, re.M | re.S | re.I)
    s = (m.group(1) if m else answer).strip()
    s = MARK_RX.sub("", s)
    return s[:limit]


def model_short(name: str) -> str:
    n = (name or "").lower()
    for k in ("opus", "sonnet", "haiku", "fable"):
        if k in n:
            return k
    return name or "?"


def qnum(qid: str) -> int:
    return int(qid[1:])


# ============================================================================ config/state
def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    p = home_dir() / "config.json"
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8-sig"))
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:  # noqa: BLE001
            print(f"config.json lỗi: {e}", file=sys.stderr)
    if not cfg.get("bridge_dir"):
        cfg["bridge_dir"] = str(TOOLS_DIR.parent)
    if not cfg.get("python"):
        cfg["python"] = sys.executable
    return cfg


class State:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.d: dict = {}
        if path.exists():
            try:
                self.d = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self.d = {}

    def get(self, k, default=None):
        with self.lock:
            return self.d.get(k, default)

    def set(self, k, v):
        with self.lock:
            self.d[k] = v
            self.save()

    def sub(self, k) -> dict:
        with self.lock:
            return self.d.setdefault(k, {})

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.d, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)


def setup_logging(home: Path, verbose: bool = True) -> None:
    home.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    fh = logging.handlers.RotatingFileHandler(home / "watcher.log", maxBytes=1_000_000,
                                              backupCount=3, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def prevent_sleep() -> None:
    if IS_WIN:
        try:
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        except Exception:
            pass


class SingleInstance:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def release(self) -> None:
        if not self.fh:
            return
        try:
            if IS_WIN:
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None

    def held_by_other(self) -> bool:
        """True nếu tiến trình khác đang giữ khoá (dò không làm hỏng khoá)."""
        if not self.path.exists():
            return False
        try:
            fh = open(self.path, "a+")
        except OSError:
            return True
        try:
            if IS_WIN:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return False
        except OSError:
            return True
        finally:
            fh.close()

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if IS_WIN:
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return True


# ============================================================================ bật / tắt hệ thống
def off_flag(home: Path) -> Path:
    return home / "tat"          # có file này = hệ thống ĐÃ TẮT (watcher không chạy, không tự khởi động lại)


def watcher_running(home: Path) -> bool:
    return SingleInstance(home / "watcher.lock").held_by_other()


def read_pid(f: Path) -> int | None:
    try:
        return int(f.read_text(encoding="utf-8", errors="replace").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def child_pids(pid: int) -> list[int]:
    """Các tiến trình con/cháu (Linux, đọc /proc) — để tắt cả claude đang chạy dở."""
    kids: dict[int, list[int]] = {}
    for d in Path("/proc").glob("[0-9]*"):
        try:
            ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
            kids.setdefault(ppid, []).append(int(d.name))
        except (OSError, ValueError, IndexError):
            continue
    out, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def kill_pid_tree(pid: int) -> None:
    if not pid or pid == os.getpid():
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
            return
        for c in reversed(child_pids(pid)):
            try:
                os.kill(c, signal.SIGKILL)
            except OSError:
                pass
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def write_off_status(cfg: dict, reason: str) -> None:
    """cau_hoi/_TRANG_THAI.md: báo ĐÃ TẮT để người hỏi biết từ xa."""
    try:
        qdir = Path(cfg["bridge_dir"]) / "cau_hoi"
        if qdir.is_dir():
            atomic_write(qdir / "_TRANG_THAI.md", "\n".join([
                "# Trạng thái PD_Bridge (máy ngoài)", "",
                f"- ⏹ **ĐÃ TẮT** lúc {ts_str(fmt='%Y-%m-%d %H:%M:%S')} — {reason}",
                "- Câu hỏi gửi lúc tắt sẽ được trả lời khi bật lại.",
                "- Bật lại trên máy ngoài: gõ `pdbat` (hoặc double-click shortcut **PD_Bridge - BAT** trên Desktop).", ""]))
    except OSError:
        pass


def start_system(cfg: dict, home: Path, wait: float = 20) -> int:
    off_flag(home).unlink(missing_ok=True)
    (home / "stop").unlink(missing_ok=True)
    if watcher_running(home):
        print("PD_Bridge ĐANG BẬT sẵn rồi.")
        return 0
    home.mkdir(parents=True, exist_ok=True)
    ps1 = TOOLS_DIR / "watcher.ps1"
    if IS_WIN and ps1.exists():
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(ps1)]
    else:
        cmd = [cfg.get("python") or sys.executable, str(TOOLS_DIR / "bridge.py"), "watch"]
    # chạy tách khỏi cửa sổ lệnh: đóng cửa sổ pdbat không làm tắt hệ thống
    NEW_GROUP, BREAKAWAY = 0x00000200, 0x01000000
    variants = [{"creationflags": CREATE_NO_WINDOW | NEW_GROUP | BREAKAWAY},
                {"creationflags": CREATE_NO_WINDOW | NEW_GROUP}] if IS_WIN else [{"start_new_session": True}]
    err = None
    for kw in variants:
        try:
            with open(home / "bridge_err.log", "ab") as ef:
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=ef,
                                 close_fds=True, **kw)
            err = None
            break
        except OSError as e:          # vd. không được tách khỏi job của cửa sổ lệnh -> thử cách khác
            err = e
    if err:
        print(f"Không bật được: {err}")
        return 1
    t0 = now()
    while now() - t0 < wait:
        if watcher_running(home):
            print("✅ PD_Bridge ĐÃ BẬT (chạy nền, ẩn). Gửi câu hỏi vào OneDrive\\PD_Bridge\\cau_hoi. Tắt: pdtat")
            return 0
        time.sleep(0.5)
    print(f"⚠️ Chưa thấy watcher chạy sau {wait:g} giây — xem {home / 'watcher.log'} và bridge_err.log")
    return 1


def stop_system(cfg: dict, home: Path, wait: float = 15) -> int:
    """Tắt hẳn: kể cả khi đang trả lời dở (câu dở dang được làm lại khi bật)."""
    off_flag(home).write_text(ts_str(), encoding="utf-8")
    running = watcher_running(home)
    t0 = now()
    while running and now() - t0 < wait:
        time.sleep(0.5)
        running = watcher_running(home)
    if running:                                   # không tự thoát được -> buộc dừng
        kill_pid_tree(read_pid(home / "watcher.pid") or 0)
        time.sleep(1)
        running = watcher_running(home)
    kill_pid_tree(read_pid(home / "watcher_ps.pid") or 0)   # vòng lặp watcher.ps1 (Windows)
    if running:
        print("⚠️ Không tắt được watcher — mở Task Manager, tắt python.exe chạy bridge.py")
        return 1
    write_off_status(cfg, "tắt bằng lệnh pdtat")
    print("⏹ PD_Bridge ĐÃ TẮT (không chạy nền, không tự bật lại khi mở máy). Bật lại: pdbat")
    return 0


# ============================================================================ limit/error parsing
LOGIN_RX = re.compile(r"(invalid api key|please run /login|run /login|not logged in|log ?in required|"
                      r"authentication_error|oauth token (?:has )?expired|token (?:has )?expired|"
                      r"unauthorized|\b401\b|credit balance is too low)", re.I)
LIMIT_RX = re.compile(r"(usage limit|limit reached|hit your (?:\w+ )?limit|reached your (?:\w+ )?limit|"
                      r"out of (?:extra )?usage|quota|resets? (?:at |in |on )?\d|rate_limit_error|"
                      r"\b429\b|exceeded .*limit)", re.I)
TRANSIENT_RX = re.compile(r"(overloaded|\b529\b|\b50[0234]\b|timed? ?out|ECONNRESET|ETIMEDOUT|"
                          r"network|socket hang up|EAI_AGAIN|fetch failed|api_error|stream)", re.I)


def parse_reset(text: str, ref: float | None = None) -> float | None:
    ref = ref or now()
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return float(m.group(1)) + 60
    m = re.search(r"in (\d+)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", text, re.I)
    if m and re.search(r"reset|try again|limit", text, re.I):
        n = int(m.group(1))
        return ref + n * (3600 if m.group(2).lower().startswith("h") else 60) + 60
    base = dt.datetime.fromtimestamp(ref)
    m = re.search(r"resets?\s+(?:at\s+|on\s+)?(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{1,2}),?\s+(?:at\s+)?)?"
                  r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.I)
    if m:
        hh = int(m.group(3))
        mm = int(m.group(4) or 0)
        ap = (m.group(5) or "").lower()
        if ap == "pm" and hh < 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
        if hh > 23 or mm > 59:
            return None
        tzname = re.search(r"\(([A-Za-z_]+/[A-Za-z_]+)\)", text)
        target = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if tzname:
            try:
                from zoneinfo import ZoneInfo
                z = ZoneInfo(tzname.group(1))
                zbase = dt.datetime.fromtimestamp(ref, z)
                zt = zbase.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if m.group(1):
                    mon = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(m.group(1)[:3].lower()) + 1
                    zt = zt.replace(month=mon, day=int(m.group(2)))
                    if zt.timestamp() < ref - 86400:
                        zt = zt.replace(year=zt.year + 1)
                elif zt.timestamp() <= ref:
                    zt += dt.timedelta(days=1)
                return zt.timestamp() + 60
            except Exception:
                pass
        if m.group(1):
            mon = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(m.group(1)[:3].lower()) + 1
            target = target.replace(month=mon, day=int(m.group(2)))
            if target.timestamp() < ref - 86400:
                target = target.replace(year=target.year + 1)
        elif target.timestamp() <= ref:
            target += dt.timedelta(days=1)
        return target.timestamp() + 60
    return None


def classify_failure(text: str) -> str:
    if LOGIN_RX.search(text):
        return "login"
    if LIMIT_RX.search(text):
        return "limit"
    if TRANSIENT_RX.search(text):
        return "transient"
    return "error"


# ============================================================================ runner
class RunResult:
    def __init__(self):
        self.ok = False
        self.kind = "error"            # ok|login|limit|transient|error|timeout|max_turns
        self.text = ""
        self.model = ""
        self.session_id = ""
        self.num_turns = 0
        self.cost = 0.0
        self.reset_at: float | None = None
        self.raw = ""

    def __repr__(self):
        return f"<RunResult {self.kind} model={self.model} turns={self.num_turns}>"


def find_claude(cfg: dict) -> str | None:
    c = cfg.get("claude_cmd")
    if c and (Path(c).exists() or shutil.which(c)):
        return shutil.which(c) or c
    w = shutil.which("claude")
    if w:
        return w
    cands = []
    if IS_WIN:
        up = Path(os.environ.get("USERPROFILE", str(Path.home())))
        cands = [up / ".local" / "bin" / "claude.exe",
                 Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
                 Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe"]
    else:
        cands = [Path.home() / ".local" / "bin" / "claude", Path("/usr/local/bin/claude")]
    for p in cands:
        if p.exists():
            return str(p)
    return None


def rule_path(p: Path) -> str:
    """Đường dẫn tuyệt đối theo cú pháp luật quyền của Claude Code (//d/K/... hoặc //home/...)."""
    s = str(p.resolve() if p.exists() else p)
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if m:
        return "//" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/").rstrip("/")
    return "/" + s.replace("\\", "/").rstrip("/")


def child_env(cfg: dict) -> dict:
    env = dict(os.environ)
    if not cfg.get("allow_api_key"):
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                  "CLAUDE_CODE_USE_VERTEX"):
            env.pop(k, None)
    py_dir = str(Path(cfg.get("python") or sys.executable).parent)
    env["PATH"] = py_dir + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PD_BRIDGE_HOME"] = str(home_dir())
    if cfg.get("data_dir"):
        env["PD_DATA_DIR"] = str(cfg["data_dir"])
    return env


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class ClaudeRunner:
    def __init__(self, cfg: dict, state: State):
        self.cfg = cfg
        self.state = state
        self.exe = find_claude(cfg)
        self._flags: set[str] | None = None
        self.compat = False          # True: bản claude cũ, chỉ dùng các cờ cơ bản
        self.procs: set = set()      # tiến trình claude đang chạy
        self.aborting = False        # đang tắt hệ thống: không xử lý kết quả nữa

    def abort_all(self) -> None:
        self.aborting = True
        for p in list(self.procs):
            kill_tree(p)

    def flags(self) -> set[str]:
        if self._flags is None:
            cached = self.state.get("cli_flags")
            ver = self.version()
            if cached and cached.get("ver") == ver:
                self._flags = set(cached["flags"])
            else:
                try:
                    out = subprocess.run([self.exe, "--help"], capture_output=True, text=True, timeout=60,
                                         encoding="utf-8", errors="replace", env=child_env(self.cfg),
                                         creationflags=CREATE_NO_WINDOW).stdout
                except Exception:
                    out = ""
                self._flags = set(re.findall(r"(--[A-Za-z][A-Za-z\-]+)", out))
                self._flags.add("--max-turns")
                self.state.set("cli_flags", {"ver": ver, "flags": sorted(self._flags)})
        return self._flags

    def version(self) -> str:
        try:
            return subprocess.run([self.exe, "--version"], capture_output=True, text=True, timeout=60,
                                  encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
                                  env=child_env(self.cfg)).stdout.strip()
        except Exception:
            return ""

    def auth_status(self) -> dict:
        """0 token. -> {'loggedIn': bool, 'authMethod': str} hoặc {} nếu CLI không hỗ trợ."""
        try:
            r = subprocess.run([self.exe, "auth", "status", "--json"], capture_output=True, text=True,
                               timeout=60, encoding="utf-8", errors="replace", env=child_env(self.cfg),
                               creationflags=CREATE_NO_WINDOW)
            m = re.search(r"\{.*\}", r.stdout, re.S)
            if m:
                return json.loads(m.group(0))
            if re.search(r"not logged in|login", (r.stdout + r.stderr), re.I) and r.returncode != 0:
                return {"loggedIn": False, "authMethod": ""}
        except Exception:
            pass
        return {}

    def build_cmd(self, model: str, job: str, add_dirs: list[Path], deny_dirs: list[Path],
                  allowed: list[str], resume: str | None = None, max_turns: int | None = None) -> list[str]:
        f = self.flags()
        if self.compat:
            f = {"--allowedTools", "--add-dir", "--max-turns", "--output-format"}
        cmd = [self.exe, "-p", "--output-format", "json", "--model", model]
        if "--permission-mode" in f:
            cmd += ["--permission-mode", "dontAsk"]
        cmd += ["--allowedTools", ",".join(allowed)]
        deny = ["Agent", "Task", "NotebookEdit"]
        for d in deny_dirs:
            rp = rule_path(d)
            deny += [f"Write({rp}/**)", f"Edit({rp}/**)"]
        cmd += ["--disallowedTools", ",".join(deny)]
        for d in add_dirs:
            cmd += ["--add-dir", str(d)]
        eff = (self.cfg.get("effort") or {}).get(job)
        if eff and "--effort" in f:
            cmd += ["--effort", eff]
        fb = (self.cfg.get("models") or {}).get("fallback")
        if fb and "--fallback-model" in f and model_short(fb) != model_short(model):
            cmd += ["--fallback-model", fb]
        if "--tools" in f:                     # chỉ nạp mô tả các tool cần dùng -> bớt token cố định
            names = sorted({re.sub(r"\(.*", "", t) for t in allowed if t != "TodoWrite"})
            if not IS_WIN:
                names = [n for n in names if n != "PowerShell"]
            cmd += ["--tools", ",".join(dict.fromkeys(names))]
        if "--exclude-dynamic-system-prompt-sections" in f:
            cmd += ["--exclude-dynamic-system-prompt-sections"]   # tăng cache hit giữa các lượt
        if "--strict-mcp-config" in f:
            cmd += ["--strict-mcp-config"]      # không nạp MCP server -> bớt token mô tả tool
        mt = max_turns or (self.cfg.get("max_turns") or {}).get(job)
        if mt:
            cmd += ["--max-turns", str(mt)]
        if resume:
            cmd += ["--resume", resume]
        return cmd

    def run(self, prompt: str, model: str, job: str, cwd: Path, add_dirs: list[Path],
            deny_dirs: list[Path], allowed: list[str], run_dir: Path, tag: str = "",
            resume: str | None = None, max_turns: int | None = None, on_progress=None) -> RunResult:
        res = RunResult()
        if not self.exe:
            res.kind, res.text = "login", "Không tìm thấy lệnh claude (chưa cài Claude Code)"
            return res
        cmd = self.build_cmd(model, job, add_dirs, deny_dirs, allowed, resume, max_turns)
        stream = "--verbose" in self.flags() and not self.compat
        if stream:      # stream-json: theo dõi tiến độ + phát hiện treo (không tốn thêm token)
            i = cmd.index("json")
            cmd[i:i + 1] = ["stream-json", "--verbose"]
        timeout = int((self.cfg.get("timeout_min") or {}).get(job, 45)) * 60
        stall = float(self.cfg.get("stall_minutes", 5)) * 60
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / f"cmd{tag}.txt").write_text(json.dumps(cmd, ensure_ascii=False, indent=1), encoding="utf-8")
        (run_dir / f"prompt{tag}.txt").write_text(prompt, encoding="utf-8")
        kw = {}
        if IS_WIN:
            kw["creationflags"] = CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        t0 = now()
        try:
            proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=child_env(self.cfg), **kw)
        except OSError as e:
            res.kind, res.text = "error", f"Không chạy được claude: {e}"
            return res
        self.procs.add(proc)
        if self.aborting:
            kill_tree(proc)
        out_lines: list[bytes] = []
        err_buf: list[bytes] = []
        prog = {"steps": 0, "last": "", "t_last": now(), "t0": t0, "recent": [], "sid": ""}

        def rd_out():
            for line in iter(proc.stdout.readline, b""):
                out_lines.append(line)
                prog["t_last"] = now()
                if not prog["sid"] and b'"session_id"' in line[:400]:
                    m_ = re.search(rb'"session_id"\s*:\s*"([^"]+)"', line)
                    if m_:
                        prog["sid"] = m_.group(1).decode()
                if stream and line.startswith(b"{") and b'"tool_use"' in line:
                    try:
                        ev = json.loads(line)
                        for c in (ev.get("message") or {}).get("content") or []:
                            if isinstance(c, dict) and c.get("type") == "tool_use":
                                prog["steps"] += 1
                                inp = c.get("input") or {}
                                arg = inp.get("query") or inp.get("pattern") or inp.get("file_path") or \
                                    inp.get("command") or inp.get("url") or ""
                                prog["last"] = f"{c.get('name')} {str(arg)[:70]}".strip()
                                prog["recent"] = (prog["recent"] + [prog["last"]])[-5:]
                    except Exception:
                        pass

        def rd_err():
            for line in iter(proc.stderr.readline, b""):
                err_buf.append(line)

        th = [threading.Thread(target=rd_out, daemon=True), threading.Thread(target=rd_err, daemon=True)]
        for t_ in th:
            t_.start()
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
        killed = None
        last_cb = 0.0
        while True:
            try:
                proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                pass
            t = now()
            if t - t0 > timeout:
                killed = f"Quá thời gian {timeout // 60} phút"
            elif stream and t - prog["t_last"] > stall:
                killed = f"Claude không phản hồi {stall / 60:g} phút (treo)"
            if killed:
                kill_tree(proc)
                try:
                    proc.wait(timeout=30)
                except Exception:
                    pass
                break
            if on_progress and t - last_cb >= float(self.cfg.get("progress_seconds", 10)):
                last_cb = t
                try:
                    on_progress(dict(prog))
                except Exception:
                    pass
        self.procs.discard(proc)
        while self.aborting:          # hệ thống đang tắt: bỏ dở, không ghi lỗi/thử lại (bật lại sẽ làm từ đầu)
            time.sleep(3600)
        for t_ in th:
            t_.join(timeout=5)
        out_s = b"".join(out_lines).decode("utf-8", errors="replace")
        err_s = b"".join(err_buf).decode("utf-8", errors="replace")
        res.session_id = prog["sid"]
        if killed:
            (run_dir / f"stdout{tag}.txt").write_text(out_s, encoding="utf-8")
            res.kind, res.text = "timeout", killed + (f" — bước cuối: {prog['last']}" if prog["last"] else "")
            log.warning("   claude %s: %s", job, res.text)
            return res
        (run_dir / f"stdout{tag}.json").write_text(out_s, encoding="utf-8")
        if err_s.strip():
            (run_dir / f"stderr{tag}.txt").write_text(err_s, encoding="utf-8")
        res.raw = out_s
        data = None
        lines_ = out_s.strip().splitlines()
        cands = [out_s.strip()] + [l for l in reversed(lines_) if '"type":"result"' in l.replace(" ", "")][:1] \
            + lines_[-1:]
        for cand in cands:
            try:
                data = json.loads(cand)
                break
            except Exception:
                continue
        if isinstance(data, list):
            data = next((x for x in reversed(data) if isinstance(x, dict) and x.get("type") == "result"), None)
        if isinstance(data, dict):
            res.text = str(data.get("result") or "")
            res.session_id = data.get("session_id") or ""
            res.num_turns = int(data.get("num_turns") or 0)
            res.cost = float(data.get("total_cost_usd") or 0)
            mu = data.get("modelUsage") or {}
            if mu:
                res.model = max(mu.items(), key=lambda kv: kv[1].get("outputTokens", 0))[0]
            else:
                res.model = model
            sub = data.get("subtype") or ""
            if not data.get("is_error") and sub == "success":
                res.ok, res.kind = True, "ok"
            elif sub == "error_max_turns":
                res.kind = "max_turns"
            else:
                blob = res.text + "\n" + err_s + "\n" + str(data.get("api_error_status") or "")
                res.kind = classify_failure(blob)
        else:
            blob = out_s + "\n" + err_s
            res.text = blob.strip()[-2000:]
            res.kind = classify_failure(blob) if proc.returncode else "error"
            if proc.returncode == 0 and out_s.strip():
                res.ok, res.kind, res.text = True, "ok", out_s
        if res.kind == "limit":
            res.reset_at = parse_reset(res.text + "\n" + err_s)
        if not res.ok and not self.compat and re.search(
                r"unknown option|unknown argument|error: option|invalid (choice|value|argument)|"
                r"unrecognized|not a valid", err_s + res.text, re.I) and now() - t0 < 60:
            log.warning("   CLI không nhận một số tuỳ chọn — chạy lại ở chế độ tương thích (%s)", err_s.strip()[:200])
            self.compat = True
            return self.run(prompt, model, job, cwd, add_dirs, deny_dirs, allowed, run_dir, tag + "_compat",
                            resume, max_turns, on_progress)
        if not res.ok:
            res.text = res.text or err_s.strip()[-1500:]
        log.info("   claude %s model=%s -> %s (%d lượt, %.0fs)%s", job, model, res.kind, res.num_turns, now() - t0,
                 "" if res.ok else " | " + res.text[:300].replace("\n", " "))
        return res


# ============================================================================ bridge
class Bridge:
    def __init__(self, cfg: dict, state: State, verbose: bool = True):
        self.cfg = cfg
        self.state = state
        self.home = home_dir()
        self.root = Path(cfg["bridge_dir"])
        self.qdir = self.root / "cau_hoi"
        self.trash = self.qdir / "_cho_xoa"
        self.tong_hop = self.root / "tong_hop"
        self.kien_thuc = self.root / "kien_thuc"
        self.tools = self.root / "tools"
        self.so_tay = self.root / "so_tay"              # sổ tay lệnh/thuật ngữ (đọc trên OneDrive)
        if not (self.tools / "pd_index.py").exists():
            self.tools = TOOLS_DIR
        self.runs = self.home / "runs"
        self.data_dir = Path(cfg["data_dir"]) if cfg.get("data_dir") else None
        for d in (self.qdir, self.tong_hop, self.kien_thuc, self.runs):
            d.mkdir(parents=True, exist_ok=True)
        self.runner = ClaudeRunner(cfg, state)
        self.seen: dict[str, tuple] = {}          # tên file -> (size, mtime, last_change)
        self.workers: dict[str, threading.Thread] = {}
        self.active: set[str] = set()
        self.io_lock = threading.RLock()
        self.index_lock = threading.Lock()
        self.index_proc: subprocess.Popen | None = None
        self.last_house = 0.0
        self.code_mtime = self._code_mtime()
        self.progress: dict[str, dict] = {}
        self.last_status = 0.0
        self.preflight_ok_until = 0.0
        self.last_error = ""
        self.live: dict[str, tuple] = {}     # qid -> (cur, tiến độ, answer_tmp) của câu đang trả lời
        self.auth_cache: tuple[float, dict] = (0.0, {})
        sd = cfg.get("store_dir") or str(Path(os.environ.get("USERPROFILE") or Path.home()) / "PD_Bridge_Kho")
        self.store = pd_store.Store(Path(sd), self.data_dir)
        self.notebook = pd_store.Notebook(self.store.root / "so_tay.json", self.so_tay)
        try:
            n = pd_store.migrate_old_attachments(self.root / "du_lieu_gui", self.store)
            if n:
                log.info("Đã chuyển %d mục du_lieu_gui (OneDrive) vào kho máy ngoài %s", n, self.store.root)
        except Exception as e:  # noqa: BLE001
            log.warning("Chuyển du_lieu_gui lỗi: %s", e)

    def proj_of(self, qid: str) -> str:
        return self.state.sub("proj").get(qid, pd_store.DEFAULT_PROJECT)

    def set_proj(self, qid: str, text: str, filename: str) -> str:
        proj = self.store.detect(text, filename)
        self.store.pdir(proj)
        self.state.sub("proj")[qid] = proj
        self.state.save()
        return proj

    # ------------------------------------------------------------------ paths
    def db_path(self) -> Path | None:
        _, db = pd_index.default_paths(str(self.data_dir) if self.data_dir else None,
                                       self.cfg.get("index_db"))
        if db and not db.exists():
            alt = self.home / ".pd_index" / "index.db"
            if alt.exists():
                return alt
        return db

    def _code_mtime(self) -> float:
        try:
            return (TOOLS_DIR / "bridge.py").stat().st_mtime
        except OSError:
            return 0

    # ------------------------------------------------------------------ scanning
    def is_ignored(self, name: str) -> bool:
        low = name.lower()
        return (name.startswith((".", "_", "~$", "~")) or low.endswith((".tmp", ".partial", ".crdownload"))
                or low in ("desktop.ini", "thumbs.db", "readme.md", "huong_dan.md"))

    def ready(self, p: Path, debounce: float | None = None) -> bool:
        """File đã ngừng thay đổi >= debounce giây và có nội dung."""
        debounce = self.cfg["debounce_seconds"] if debounce is None else debounce
        try:
            st = p.stat()
            if p.is_dir():
                files = [f for f in p.rglob("*") if f.is_file()]
                size = sum(f.stat().st_size for f in files)
                mt = max([f.stat().st_mtime for f in files] + [st.st_mtime])
                st = os.stat_result((0, 0, 0, 0, 0, 0, size, 0, mt, 0))
        except OSError:
            return False
        key = p.name
        sig = (st.st_size, st.st_mtime)
        t = now()
        prev = self.seen.get(key)
        if prev is None:
            last = st.st_mtime if st.st_mtime <= t else t
            self.seen[key] = (sig[0], sig[1], last)
        elif (prev[0], prev[1]) != sig:
            self.seen[key] = (sig[0], sig[1], t)
        if st.st_size == 0:
            return False
        return t - self.seen[key][2] >= debounce

    def scan(self) -> tuple[list[Path], dict[str, dict]]:
        new: list[Path] = []
        rounds: dict[str, dict] = {}
        try:
            entries = list(self.qdir.iterdir())
        except OSError:
            return new, rounds
        for p in entries:
            if self.is_ignored(p.name):
                continue
            if p.is_dir():
                if not re.match(r"^Q\d{3,}_", p.name):
                    new.append(p)                  # thư mục = câu hỏi kèm file dữ liệu
                continue
            if not p.is_file():
                continue
            n = p.name
            m = ANS_RX.match(n)
            if m:
                rounds.setdefault(m.group(1).upper(), {})["a"] = p
                continue
            m = OLD_RX.match(n)
            if m:
                rounds.setdefault(m.group(1).upper(), {})["old"] = p
                continue
            m = Q_RX.match(n)
            if m:
                r = rounds.setdefault(m.group(1).upper(), {})
                if "q" not in r or p.stat().st_mtime > r["q"].stat().st_mtime:
                    r["q"] = p
                continue
            if n.lower().endswith((".md", ".txt", ".markdown")):
                new.append(p)
        names = {p.name for p in entries}
        for k in [k for k in self.seen if k not in names]:
            self.seen.pop(k, None)
        new.sort(key=lambda p: p.stat().st_mtime)
        return new, rounds

    def next_id(self) -> str:
        mx = int(self.state.get("last_id", 0))
        for d in (self.qdir, self.trash):
            if d.exists():
                for p in d.rglob("Q*"):
                    m = re.match(r"^Q(\d{3,})_", p.name)
                    if m:
                        mx = max(mx, int(m.group(1)))
        return f"Q{mx + 1:03d}"

    # ------------------------------------------------------------------ claim
    def claim_new(self, p: Path, status_msg: str = "⏳ Đã nhận — đang chờ xử lý.") -> str | None:
        """Đổi tên câu hỏi mới -> Qnnn_<tên>.md và tạo file trả lời tạm. 0 token."""
        if p.is_dir():
            return self.claim_dir(p, status_msg)
        try:
            text = read_text(p)
        except OSError:
            return None
        if not norm_text(text):
            return None
        alias = self.state.sub("aliases").get(p.name)
        if alias and now() - alias.get("ts", 0) < 48 * 3600:
            # Trình soạn thảo lưu lại tên cũ sau khi đã đổi tên -> coi là sửa câu hỏi cũ
            _, rounds = self.scan()
            r = rounds.get(alias["q"], {})
            if r.get("q"):
                if text_hash(text) != text_hash(read_text(r["q"])):
                    atomic_write(r["q"], text)
                    log.info("%s: file '%s' được lưu lại theo tên cũ -> cập nhật câu hỏi", alias["q"], p.name)
                try:
                    p.unlink()
                except OSError:
                    return None
                return alias["q"]
        with self.state.lock:
            qid = self.next_id()
            stem = display_stem(p.name)
            target = self.qdir / f"{qid}_{stem}.md"
            try:
                os.replace(p, target)
            except OSError as e:
                log.warning("Chưa đổi tên được %s (%s) — thử lại sau", p.name, e)
                return None
            self.state.d["last_id"] = qnum(qid)
            self.state.sub("aliases")[p.name] = {"q": qid, "ts": now()}
            self.state.save()
        self.state.sub("att")[qid] = {"orig": display_stem(p.name)}
        self.state.save()
        proj = self.set_proj(qid, text, p.name)
        n_att = self.sweep_attachments(qid)
        mode, model = self.resolve_mode(stem, text)
        self.write_placeholder(qid, stem, mode, 1, "queued", status_msg)
        log.info("Câu hỏi mới: '%s' -> %s (mode %s, model %s, project %s%s)", p.name, target.name, mode, model,
                 proj, f", {n_att} file đính kèm" if n_att else "")
        return qid

    def claim_dir(self, d: Path, status_msg: str) -> str | None:
        """Thư mục câu hỏi: 1 file .md/.txt là câu hỏi, các file còn lại là dữ liệu đính kèm."""
        files = [f for f in d.iterdir() if f.is_file() and not self.is_ignored(f.name)]
        qs = [f for f in files if f.suffix.lower() in (".md", ".txt", ".markdown")]
        qs.sort(key=lambda f: (not re.search(r"cau.?hoi|question|hoi", strip_accents(f.name).lower()),
                               f.stat().st_size))
        text = ""
        if qs:
            text = read_text(qs[0])
        if not norm_text(text):
            if not files:
                return None
            text = f"{display_stem(d.name)}\n\n(Phân tích các file dữ liệu đính kèm.)"
        with self.state.lock:
            qid = self.next_id()
            stem = display_stem(d.name)
            proj = self.store.detect(text, d.name)
            dest = self.store.du_lieu_dir(proj, qid, stem)
            dest.rmdir()
            try:
                shutil.move(str(d), str(dest))
            except OSError as e:
                log.warning("Chưa chuyển được thư mục %s (%s) — thử lại sau", d.name, e)
                return None
            if qs:
                try:
                    (dest / qs[0].name).unlink()
                except OSError:
                    pass
            atomic_write(self.qdir / f"{qid}_{stem}.md", text)
            self.state.d["last_id"] = qnum(qid)
            self.state.sub("att")[qid] = {"orig": stem, "dir": str(dest)}
            self.state.sub("proj")[qid] = proj
            self.state.save()
        mode, model = self.resolve_mode(stem, text)
        self.write_placeholder(qid, stem, mode, 1, "queued", status_msg)
        log.info("Câu hỏi mới (thư mục): '%s' -> %s_%s.md (mode %s, %d file đính kèm)", d.name, qid, stem,
                 mode, len(files) - (1 if qs else 0))
        return qid

    def resolve_mode(self, stem: str, text: str) -> tuple[str, str]:
        """mode: dòng 'mode:' trong nội dung > tiền tố tên file > mặc định. model: tiền tố > config."""
        pmode, pmodel, _ = parse_prefix(stem)
        cmode, _, _ = parse_mode(text, "")
        mode = cmode or pmode or self.cfg["default_mode"]
        model = pmodel or self.cfg["models"].get(mode, "opus")
        return mode, model

    def sweep_attachments(self, qid: str) -> int:
        """Chuyển file dữ liệu cùng tên gốc (vd. hold.md + hold_timing.rpt) vào du_lieu_gui/Qnnn_*/."""
        meta = self.state.sub("att").get(qid) or {}
        orig = (meta.get("orig") or "").lower()
        if not orig:
            return 0
        moved = 0
        try:
            entries = list(self.qdir.iterdir())
        except OSError:
            return 0
        for f in entries:
            if not f.is_file() or self.is_ignored(f.name) or f.suffix.lower() in (".md", ".txt", ".markdown"):
                continue
            if re.match(r"^Q\d{3,}_", f.name) or not f.name.lower().startswith(orig):
                continue
            dest = Path(meta.get("dir") or self.store.du_lieu_dir(self.proj_of(qid), qid, orig))
            dest.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(f), str(dest / f.name))
                moved += 1
            except OSError:
                continue
            meta["dir"] = str(dest)
        if moved:
            self.state.sub("att")[qid] = meta
            self.state.save()
        return moved

    def attachments_dir(self, qid: str) -> Path | None:
        d = (self.state.sub("att").get(qid) or {}).get("dir")
        if d and Path(d).is_dir():
            return Path(d)
        return None

    def attachment_context(self, qid: str) -> str:
        """Tóm tắt file đính kèm bằng script (0 token) để Claude không phải tự mở từng file."""
        d = self.attachments_dir(qid)
        if not d:
            return ""
        budget = int(self.cfg.get("attach_inline_chars", 14000))
        out = [f"Thư mục: {d}"]
        files = sorted((f for f in d.rglob("*") if f.is_file()), key=lambda f: f.name.lower())[:40]
        for f in files:
            rel = f.relative_to(d).as_posix()
            size = f.stat().st_size
            ext = f.suffix.lower()
            head = f"- {rel} ({pd_summarize.human(size)})"
            try:
                if ext in (".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg", ".gif", ".bmp"):
                    out.append(head + " — mở bằng Read nếu cần (PDF/ảnh đọc trực tiếp được)")
                    continue
                raw = f.read_bytes()[:200000]
                if pd_index.looks_binary(raw):
                    out.append(head + " — file nhị phân")
                    continue
                if size <= 6000 and budget > size:
                    txt = pd_index.decode_bytes(raw).strip()
                    out.append(head + ":\n```\n" + txt + "\n```")
                    budget -= len(txt)
                elif budget > 500:
                    sm = pd_summarize.summarize_file(f, max_lines=35, top=8)
                    sm = sm[: min(len(sm), budget)]
                    out.append(head + " — tóm tắt tự động:\n```\n" + sm + "\n```")
                    budget -= len(sm)
                else:
                    out.append(head)
            except Exception as e:  # noqa: BLE001
                out.append(head + f" — không đọc được ({e})")
        return "\n".join(out)

    def write_placeholder(self, qid: str, stem: str, mode: str, v: int, status: str, msg: str,
                          extra: str = "") -> None:
        body = (f"# {qid} — {stem}\n\n{msg}\n\n"
                f"_mode: {mode} · cập nhật {ts_str()}_\n{extra}\n"
                f"<!-- pd_bridge q={qid} v={v} status={status} mode={mode} -->\n")
        atomic_write(self.qdir / f"{qid}_traloi.md", body)

    # ------------------------------------------------------------------ jobs
    def pending_jobs(self, rounds: dict[str, dict], ignore_debounce: bool = False) -> list[tuple[str, str]]:
        jobs = []
        retry = self.state.sub("retry")
        for qid in sorted(rounds, key=qnum):
            r = rounds[qid]
            qf = r.get("q")
            if not qf or qid in self.active:
                continue
            if self.chat_locked(qid):
                continue
            rt = retry.get(qid)
            if rt and now() < rt.get("next", 0):
                continue
            try:
                cur = text_hash(read_text(qf))
            except OSError:
                continue
            a = r.get("a")
            mk = parse_marker(read_text(a)) if a else {}
            st = mk.get("status")
            if st in ("done", "error") and mk.get("hash"):
                if mk["hash"] != cur and (ignore_debounce or self.ready(qf)):
                    jobs.append((qid, "edit"))
                elif st == "done" and (qid in self.state.sub("fu_pending") or (
                        parse_followups(read_text(a)) and (ignore_debounce or self.ready(a)))):
                    jobs.append((qid, "followup"))
            else:
                if ignore_debounce or self.ready(qf, 0 if st else None):
                    jobs.append((qid, "answer"))
        return jobs

    def chat_locked(self, qid: str) -> bool:
        lk = self.runs / f"{qid}.chat.lock"
        try:
            return now() - lk.stat().st_mtime < self.cfg["chat_lock_hours"] * 3600
        except OSError:
            return False

    # ------------------------------------------------------------------ prompt
    def kien_thuc_snippets(self, question: str, limit_chars: int = 2200, proj: str | None = None) -> str:
        terms = [t.lower() for t in pd_index.extract_terms(question, 20)]
        if not terms:
            return ""
        blocks = []
        files = sorted(self.kien_thuc.glob("*.md")) + self.store.kien_thuc_files(proj)
        for f in files:
            if f.name.lower().startswith("boi_canh"):
                continue
            try:
                txt = read_text(f)
            except OSError:
                continue
            parts = re.split(r"(?m)^(?=#{2,4}\s|- (?:✅|⚠️|❌|◻️))", txt)
            for part in parts:
                low = part.lower()
                score = sum(1 for t in terms if t in low)
                if score and not self.cfg.get("use_history") and "◻️" in part:
                    continue                                 # bỏ mục tự học (rút từ câu trả lời cũ)
                if score:
                    name = f.name if f.parent == self.kien_thuc else f"{f.parent.name}/{f.name}"
                    bonus = 1 if proj and f.parent.name == pd_store.slug(proj) else 0
                    blocks.append((score + bonus, name, part.strip().replace(pd_store.AUTO_START, "")
                                   .replace(pd_store.AUTO_END, "")))
        blocks.sort(key=lambda b: -b[0])
        out, size = [], 0
        for score, fn, part in blocks:
            s = part[:700]
            if size + len(s) > limit_chars:
                break
            out.append(f"[{fn}] {s}")
            size += len(s)
        return "\n".join(out)

    def boi_canh(self) -> str:
        f = self.kien_thuc / "boi_canh_du_an.md"
        try:
            t = read_text(f).strip()
        except OSError:
            return ""
        if len(t) <= 2500:
            return t
        return f"(file dài — đọc khi cần: {f})\n" + t[:1200]

    def doc_excerpts(self, question: str) -> str:
        """Nội dung các trang/đoạn tài liệu tool khớp nhất (script lấy, 0 token) -> Claude có căn cứ ngay."""
        n = int(self.cfg.get("doc_excerpts", 3))
        db = self.db_path()
        if n <= 0 or not db or not db.exists():
            return ""
        try:
            hits = pd_index.search(db, question, kind="doc", n=n)
        except BaseException:  # noqa: BLE001
            return ""
        out = []
        import sqlite3
        try:
            con = sqlite3.connect(str(db), timeout=30)
        except Exception:  # noqa: BLE001
            return ""
        try:
            for h in hits:
                loc = h["locs"][0] if h["locs"] else ""
                row = con.execute("SELECT body FROM chunks WHERE path=? AND loc=? LIMIT 1", (h["path"], loc)).fetchone()
                txt = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", (row[0] if row else "") or "")).strip()[:1800]
                if txt:
                    out.append(f"--- {h['path']}{' [' + loc + ']' if loc else ''}\n{txt}")
        finally:
            con.close()
        return "\n".join(out)

    def pre_search(self, question: str) -> str:
        db = self.db_path()
        if not db or not db.exists():
            return "(chưa có chỉ mục — dùng Glob/Grep trong thư mục dữ liệu)"
        try:
            hits = pd_index.search(db, question, n=int(self.cfg.get("pre_search_hits", 12)))
        except BaseException as e:  # noqa: BLE001
            return f"(tìm chỉ mục lỗi: {e})"
        if not hits:
            return "(không có kết quả tự động — hãy tự tìm với từ khoá tiếng Anh)"
        lines = []
        for i, h in enumerate(hits, 1):
            loc = (" [" + ", ".join(h["locs"][:3]) + "]") if h["locs"] else ""
            snip = (" — " + h["snippet"][:150]) if h["snippet"] else ""
            lines.append(f"{i}. [{h['kind']}] {h['path']}{loc}{snip}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ bộ nhớ: kho project (0 token)
    def remember_data(self, qid: str | None = None) -> None:
        """Ghi dòng thời gian cho dữ liệu vừa gửi (chỉ mục kho làm nền sau khi trả lời)."""
        d = self.attachments_dir(qid) if qid else None
        if d:
            try:
                self.store.add_timeline(self.proj_of(qid), qid, sorted(f for f in d.rglob("*") if f.is_file()))
            except Exception as e:  # noqa: BLE001
                log.warning("Dòng thời gian lỗi: %s", e)

    def search_gui(self, question: str, qid: str, n: int = 6) -> str:
        proj = self.proj_of(qid)
        d = self.attachments_dir(qid)
        ex = [f"/hoi_dap/{qid}_"] + ([f"/du_lieu/{d.name}/"] if d else [])
        if not self.cfg.get("use_history"):
            ex.append("/hoi_dap/")                       # chỉ dữ liệu đã gửi, không lấy hỏi đáp cũ
        return "\n".join(self.store.search(question, proj, ex, n))

    def related_history(self, qid: str, question: str, n: int = 3) -> str:
        """Câu hỏi cũ liên quan (từ nhat_ky.jsonl): câu hỏi, tóm tắt trả lời, đánh giá, file."""
        terms = {t.lower() for t in pd_index.extract_terms(question, 20)}
        if not terms:
            return ""
        latest: dict[str, dict] = {}
        ratings: dict[str, str] = {}
        for r in self.read_journal():
            if r.get("type") in ("answer", "followup") and r.get("q") != qid and r.get("status", "done") == "done":
                if r.get("type") == "followup" and r["q"] in latest:
                    latest[r["q"]] = dict(latest[r["q"]], summary=latest[r["q"]].get("summary", "")
                                          + " | Hỏi tiếp: " + r.get("question", "")[:150])
                    continue
                latest[r["q"]] = r
            elif r.get("type") == "rating":
                ratings[f"{r['q']}:{r['v']}"] = r.get("danh_gia", "chua")
        scored = []
        for q, r in latest.items():
            blob = (r.get("name", "") + " " + r.get("question", "") + " " + r.get("summary", "")).lower()
            sc = sum(1 for t in terms if t in blob)
            rating = ratings.get(f"{q}:{r.get('v', 1)}", "chua")
            same = r.get("proj", pd_store.DEFAULT_PROJECT) == self.proj_of(qid)
            if sc >= 2 and rating != "sai":
                scored.append((sc + (2 if rating == "dung" else 0) + (2 if same else 0), q, r, rating))
        scored.sort(key=lambda x: (-x[0], -qnum(x[1])))
        out = []
        for _, q, r, rating in scored[:n]:
            a = self.store.answer_path(r.get("proj", pd_store.DEFAULT_PROJECT), q) or self.qdir / f"{q}_traloi.md"
            mark = {"dung": "✅ đã xác nhận đúng", "mot_phan": "◑ đúng một phần"}.get(rating, "chưa đánh giá")
            d = self.attachments_dir(q)
            out.append(f"- {q} [{r.get('proj', pd_store.DEFAULT_PROJECT)}] ({r.get('ts', '')[:10]}, {mark}) — {r.get('name', '')}\n"
                       f"  Hỏi: {r.get('question', '')[:220]}\n"
                       f"  Kết luận: {r.get('summary', '')[:500]}\n"
                       f"  File: {a if a.exists() else '(đã lưu trữ)'}{' · dữ liệu: ' + str(d) if d else ''}")
        return "\n".join(out)

    def data_layout(self) -> str:
        if not self.data_dir or not self.data_dir.is_dir():
            return "(không thấy thư mục dữ liệu)"
        try:
            names = sorted(p.name + ("/" if p.is_dir() else "") for p in self.data_dir.iterdir()
                           if not p.name.startswith("."))
        except OSError:
            return ""
        s = ", ".join(names[:45])
        return s + (f", … (+{len(names) - 45})" if len(names) > 45 else "")

    MODE_TEXT = {
        "nhanh": ("NHANH — ưu tiên kiến thức + chỉ mục trên máy (Innovus Text Command Reference…), web kiểm chứng "
                  "1–2 điểm. Nhanh nhưng VẪN đủ: kết luận rõ, lệnh chạy được, cách kiểm tra kết quả."),
        "chuan": ("CHUẨN — tìm kỹ trên máy (script/report/log liên quan + tài liệu tool), đọc đúng phần cần; "
                  "research web 2–4 nguồn uy tín; đối chiếu lý thuyết với dữ liệu thật; đưa phương án cụ thể có "
                  "số liệu, các bước áp dụng, cách kiểm tra, rủi ro và phương án thay thế."),
        "sau": ("SÂU — nghiên cứu nhiều vòng: (1) thu thập đủ dữ liệu trên máy (report/log/script các run liên "
                "quan, so sánh giữa các run), (2) tài liệu Cadence/Synopsys trên máy, (3) web ≥5 nguồn: app note, "
                "paper, tài liệu hãng, diễn đàn, (4) lập giả thuyết → kiểm chứng bằng số liệu → so sánh các "
                "phương án (bảng ưu/nhược/rủi ro), (5) tự phản biện trước khi kết luận; đề xuất thí nghiệm "
                "kiểm chứng (run nào, đo gì, kỳ vọng gì)."),
    }

    def build_prompt(self, qid: str, stem: str, mode: str, question: str, answer_tmp: Path,
                     edit: dict | None, raw_mode: str | None) -> str:
        att = self.attachment_context(qid)
        search_text = question + " " + parse_prefix(stem)[2]
        hist = self.related_history(qid, search_text) if self.cfg.get("use_history") else ""
        self.wait_reindex()
        gui_hits = self.search_gui(search_text, qid)
        proj = self.proj_of(qid)
        pbc = self.store.boi_canh(proj)
        tools = self.tools
        py = Path(self.cfg.get("python") or sys.executable).stem.lower()   # python (Windows) / python3
        if py not in ("python", "python3", "py"):
            py = "python"
        kt = self.kien_thuc_snippets(question, proj=proj)
        bc = self.boi_canh()
        parts = [
            f"Câu hỏi {qid} ({stem}) từ PD_Bridge. Làm theo quy trình trong CLAUDE.md.",
            f"PROJECT: {proj} — kho lưu trữ trên máy: {self.store.pdir(proj)} (hỏi đáp cũ, dữ liệu đã gửi; đọc khi cần)",
            f"MODE: {self.MODE_TEXT[mode]}",
        ]
        if raw_mode:
            parts.append(f"(Người hỏi ghi mode '{raw_mode}' không hợp lệ → dùng {mode}.)")
        parts += [
            "",
            f"THƯ MỤC DỮ LIỆU (chỉ đọc, KHÔNG sửa/ghi): {self.data_dir}",
            f"Cấp 1 gồm: {self.data_layout()}",
            "CÔNG CỤ TRÊN MÁY (rẻ token, dùng trước khi Read file lớn):",
            f'  {py} "{tools / "pd_index.py"}" search "<từ khoá tiếng Anh>" [--kind doc|script|report|log] [-n 20]',
            f'  {py} "{tools / "pd_index.py"}" show "<đường dẫn tương đối>" [--page N | --chunk N | --grep "regex" -C 2]',
            f'  {py} "{tools / "pd_summarize.py"}" "<report/log hoặc thư mục run>"',
            "",
            "GỢI Ý TỪ CHỈ MỤC (tự động theo từ khoá câu hỏi, có thể chưa đủ):",
            self.pre_search(search_text),
        ]
        ex = self.doc_excerpts(search_text)
        if ex:
            parts += ["", "TRÍCH SẴN TÀI LIỆU TOOL TRÊN MÁY (đoạn khớp nhất; đọc thêm trang lân cận khi cần):", ex]
        if hist:
            parts += ["", "CÂU HỎI CŨ LIÊN QUAN ĐÃ TRẢ LỜI (bộ nhớ — tận dụng, không research lại phần đã chắc chắn):", hist]
        if gui_hits:
            parts += ["", f"DỮ LIỆU BẠN ĐÃ GỬI TRƯỚC ĐÂY có liên quan (gốc kho/du_an = {self.store.du_an}):",
                      gui_hits]
        if att:
            parts += ["", "DỮ LIỆU NGƯỜI HỎI GỬI KÈM (ưu tiên phân tích; đã tóm tắt sẵn, mở file gốc khi cần chi tiết):", att]
        if bc:
            parts += ["", "BỐI CẢNH CHUNG (kien_thuc/boi_canh_du_an.md):", bc]
        if pbc:
            parts += ["", f"BỐI CẢNH PROJECT {proj} (BOI_CANH.md):", pbc]
        if kt:
            parts += ["", "KIẾN THỨC TÍCH LUỸ LIÊN QUAN (✅ = đã xác nhận đúng; ◻️ = chưa xác nhận, dùng có kiểm chứng; "
                          "❌ = đã bị đánh giá sai, tránh lặp):", kt]
        if edit:
            parts += ["", f"ĐÂY LÀ CÂU HỎI ĐÃ SỬA (lần {edit['v']}). Bản trả lời trước: {edit['old_path']}"
                          f" (đánh giá: {edit['rating']}{'; ghi chú: ' + edit['note'] if edit['note'] else ''}).",
                      "Thay đổi trong câu hỏi:", edit["diff"] or "(không lấy được diff — đọc lại toàn bộ câu hỏi)",
                      "Hãy tận dụng phần còn đúng của bản trước (đọc file đó thay vì research lại từ đầu), tập trung "
                      "vào phần mới/đã sửa; nếu bản trước bị đánh giá sai/một phần thì tìm và sửa chỗ sai. "
                      "Câu trả lời mới phải ĐẦY ĐỦ, đọc độc lập được."]
        parts += [
            "",
            "CÂU HỎI:",
            "<<<",
            question,
            ">>>",
            "",
            f"Ghi TOÀN BỘ câu trả lời (Markdown, tiếng Việt; lệnh/thuật ngữ giữ tiếng Anh) vào file: {answer_tmp}",
            "Viết SỚM: ngay khi có kết luận, Write phần '## ✅ Kết luận' trước (người hỏi thấy bản nháp ngay), "
            "rồi Edit nối tiếp từng mục theo CLAUDE.md. KHÔNG cắt ngắn nội dung.",
            "Trước khi kết thúc: đọc lại câu hỏi, đối chiếu từng ý đã trả lời đủ chưa (mục '## Đã trả lời đủ chưa?'). "
            "Không in lại câu trả lời ra màn hình — cuối cùng chỉ in một dòng: XONG",
        ]
        return "\n".join(parts)

    def allowed_tools(self, web: bool = True) -> list[str]:
        base = ["Read", "Grep", "Glob", "Write", "Edit",
                "Bash(python *)", "Bash(python3 *)", "Bash(py *)",
                "PowerShell(python *)", "PowerShell(py *)"]
        # lệnh chỉ-đọc (Git Bash / PowerShell) để xem nhanh file lớn
        for c in ("cat", "head", "tail", "wc", "grep", "ls", "zcat", "sort", "uniq", "cut", "diff"):
            base.append(f"Bash({c} *)")
        for c in ("Get-Content", "Select-String", "Get-ChildItem", "Measure-Object"):
            base.append(f"PowerShell({c} *)")
        if web:
            base += ["WebSearch", "WebFetch"]
        return base

    # ------------------------------------------------------------------ process
    def process(self, qid: str, kind: str, chat: bool = False) -> dict | None:
        """Chạy 1 câu hỏi. chat=True: chỉ chuẩn bị (claim) cho chế độ chat, không gọi claude."""
        if kind == "followup":
            return None if chat else self.process_followup(qid)
        _, rounds = self.scan()
        r = rounds.get(qid, {})
        qf: Path | None = r.get("q")
        if not qf:
            return None
        stem = Q_RX.match(qf.name).group(2)
        qtext_raw = read_text(qf)
        qhash = text_hash(qtext_raw)
        _, question, raw_mode = parse_mode(qtext_raw, self.cfg["default_mode"])
        mode, model = self.resolve_mode(stem, qtext_raw)
        if not chat:
            self.sweep_attachments(qid)
        self.remember_data(qid)
        if not question.strip():
            log.info("%s: câu hỏi rỗng — bỏ qua", qid)
            return None
        a = r.get("a")
        mk = parse_marker(read_text(a)) if a else {}
        v = int(mk.get("v", 1))
        edit = None
        if kind == "edit" or (mk.get("status") in ("done", "error") and mk.get("hash") and mk["hash"] != qhash):
            old_text = read_text(a)
            rating, note = parse_rating(old_text)
            self.record_rating(qid, v, rating, note, "traloi")
            old = self.qdir / f"{qid}_traloi_cu.md"
            if old.exists():
                orating, onote = parse_rating(read_text(old))
                omk = parse_marker(read_text(old))
                self.record_rating(qid, int(omk.get("v", max(v - 1, 1))), orating, onote, "traloi_cu")
            os.replace(a, old)
            prev_q = (self.state.sub("last_q").get(qid) or {}).get("text", "")
            diff = "\n".join(list(difflib.unified_diff(prev_q.splitlines(), question.splitlines(),
                                                       "cũ", "mới", lineterm="", n=1))[2:80]) if prev_q else ""
            v += 1
            edit = {"v": v, "old_path": str(old), "rating": rating, "note": note, "diff": diff}
            log.info("%s: câu hỏi đã sửa -> trả lời lại (v%d)", qid, v)
        run_dir = self.runs / f"{qid}-v{v}-{ts_str(fmt='%Y%m%d-%H%M%S')}"
        run_dir.mkdir(parents=True, exist_ok=True)
        answer_tmp = run_dir / "answer.md"
        prompt = self.build_prompt(qid, stem, mode, question, answer_tmp, edit, raw_mode)
        info = {"qid": qid, "stem": stem, "mode": mode, "v": v, "hash": qhash, "question": question,
                "run_dir": str(run_dir), "answer_tmp": str(answer_tmp), "started": now()}
        if chat:
            (self.runs / f"{qid}.chat.lock").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
            (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
            self.write_placeholder(qid, stem, mode, v, "running", "⏳ Đang được trả lời trong chat…")
            info["prompt"] = prompt
            return info
        cur = {"qid": qid, "stem": stem, "v": v, "mode": mode, "model": model, "hash": qhash,
               "question": question, "pending_kind": "edit" if edit else "main"}
        self.show_progress(cur, {"t0": now(), "steps": 0, "recent": []}, answer_tmp)
        log.info("%s: bắt đầu (mode %s, model %s, v%d)", qid, mode, model, v)
        if self.cfg.get("runner") == "routine":
            res = self.run_routine(prompt, answer_tmp, mode)
        else:
            res = self.run_with_fallback(prompt, mode, model, run_dir, answer_tmp, job=mode,
                                         extra_dirs=[self.attachments_dir(qid), self.store.du_an], cur=cur)
        return self.handle_result(info, res, model)

    def process_followup(self, qid: str) -> dict | None:
        """'>> câu hỏi tiếp' -> trả lời nối tiếp, giữ mạch: tiếp tục phiên cũ + kèm tóm tắt cả chuỗi hỏi đáp."""
        _, rounds = self.scan()
        r = rounds.get(qid, {})
        a, qf = r.get("a"), r.get("q")
        if not a or not qf:
            return None
        pend = self.state.sub("fu_pending")
        typed = parse_followups(read_text(a))
        fu = "\n".join(x for x in (pend.get(qid, ""), typed) if x).strip()
        if not fu:
            pend.pop(qid, None)
            self.state.save()
            return None
        pend[qid] = fu                                     # lưu lại: máy tắt giữa chừng vẫn không mất câu hỏi
        self.state.save()
        stem = Q_RX.match(qf.name).group(2)
        qraw = read_text(qf)
        mode, model = self.resolve_mode(stem, qraw)
        fmode, fmodel, rest = parse_prefix(fu)             # '>> [nhanh] …' : chọn mode/model riêng cho lượt hỏi tiếp
        if fmode or fmodel:
            fu = rest
            mode = fmode or mode
            model = fmodel or self.cfg["models"].get(mode, model)
        proj = self.proj_of(qid)
        mk = parse_marker(read_text(a))
        v = int(mk.get("v", 1))
        thread = self.load_thread(qid)
        if not thread:                                     # file cũ (trước khi có luồng): lấy nội dung hiện có
            old = strip_followups(read_text(a))
            old = MARK_RX.sub("", RATING_RX.sub("", old))
            thread = [{"kind": "main", "v": v, "q": parse_mode(qraw, "")[1], "body": old.strip(), "ts": "",
                       "mode": mode, "model": "", "minutes": 0}]
            self.save_thread(qid, thread)
        run_dir = self.runs / f"{qid}-fu-{ts_str(fmt='%Y%m%d-%H%M%S')}"
        run_dir.mkdir(parents=True, exist_ok=True)
        answer_tmp = run_dir / "answer.md"
        cur = {"qid": qid, "stem": stem, "v": v, "mode": mode, "model": model, "question": fu,
               "pending_kind": "followup"}
        self.show_progress(cur, {"t0": now(), "steps": 0, "recent": []}, None)
        convo = []
        for i, t in enumerate(thread, 1):
            kind = {"main": "Hỏi", "edit": "Hỏi (đã sửa)", "followup": "Hỏi tiếp"}.get(t.get("kind"), "Hỏi")
            convo.append(f"[Lượt {i}] {kind}: {norm_text(t.get('q', ''))[:700]}\n"
                         f"  Kết luận đã trả lời: {summary_section(t.get('body', ''), 900)}")
        sid = self.state.sub("sessions").get(qid)
        prompt = "\n".join([
            f"HỎI TIẾP cho {qid} ({stem}), project {proj}. Làm theo CLAUDE.md (mục Hỏi tiếp + Tiêu chuẩn chất lượng).",
            f"MODE: {self.MODE_TEXT[mode]}",
            "",
            "CHUỖI HỎI ĐÁP TRƯỚC (giữ mạch suy nghĩ; bản đầy đủ: "
            f"{self.store.pdir(proj) / 'hoi_dap' / (qid + '_traloi.md')}):",
            *convo,
            "",
            "CÂU HỎI TIẾP MỚI:", "<<<", fu, ">>>", "",
            "Yêu cầu: hiểu ngữ cảnh các lượt trước nhưng KHÔNG nhắc lại/tóm tắt lại chúng; chỉ nói ngắn gọn nếu kết "
            "luận cũ thay đổi. Trả lời câu hỏi tiếp đầy đủ, sâu, áp dụng được ngay như một câu hỏi mới — không qua "
            "loa; tìm thêm trên máy/web khi cần; tự kiểm chứng lệnh; có '## Bài học' nếu có khái niệm/lệnh mới.",
            f"Ghi câu trả lời (Markdown, bắt đầu bằng '## ✅ Kết luận') vào file: {answer_tmp}",
            "Cuối cùng chỉ in: XONG",
        ])
        log.info("%s: hỏi tiếp (%s) — %s", qid, "tiếp tục phiên cũ" if sid else "phiên mới", fu[:80])
        t0 = now()
        res = self.run_with_fallback(prompt, mode, model, run_dir, answer_tmp, job=mode,
                                     extra_dirs=[self.attachments_dir(qid), self.store.du_an],
                                     resume=sid, max_turns=int(self.cfg.get("followup_max_turns", 60)), cur=cur)
        fails = self.state.sub("fu_fail")
        if not self.answer_ok(answer_tmp):
            if res.kind == "limit":
                self.state.set("paused_until", res.reset_at or now() + 1800)
                self.state.set("pause_reason", "limit")
                return {"qid": qid, "status": "limit"}
            fails[qid] = fails.get(qid, 0) + 1
            self.state.save()
            log.error("%s: hỏi tiếp lỗi (%s, lần %d): %s", qid, res.kind, fails[qid], res.text[:200])
            self.last_error = f"{qid} hỏi tiếp: {res.kind}"
            limit_n = int(self.cfg.get("max_attempts_transient" if res.kind in ("timeout", "transient")
                                       else "max_attempts", 3))
            if fails[qid] >= limit_n:
                body = (f"## ✅ Kết luận\n\n❌ Chưa trả lời được câu hỏi tiếp sau {fails[qid]} lần thử ({res.kind}): "
                        f"`{res.text[:300]}`\n\nViết lại dòng `>>` để thử lại.")
                self._append_round(qid, stem, thread, "followup", fu, body, model, model, t0, mk, mode=mode)
                pend.pop(qid, None)
                fails.pop(qid, None)
                self.state.save()
            else:
                self.state.sub("retry")[qid] = {"n": fails[qid], "next": now() + 30 * fails[qid]}
                self.state.save()
            return {"qid": qid, "status": "error"}
        body, verified = self.clean_body(read_text(answer_tmp), True)
        text = self._append_round(qid, stem, thread, "followup", fu, body, res.model or model, model, t0, mk,
                                  mode=mode)
        if res.session_id:
            self.state.sub("sessions")[qid] = res.session_id
        pend.pop(qid, None)
        fails.pop(qid, None)
        self.state.sub("retry").pop(qid, None)
        self.state.save()
        self.after_answer(qid, proj, stem, text, v, body, verified)
        self.journal({"type": "followup", "q": qid, "v": v, "proj": proj,
                      "ts": ts_str(fmt="%Y-%m-%d %H:%M:%S"), "t": now(), "question": fu[:800],
                      "summary": summary_section(body, 600)})
        self.render_learning(proj)
        self.reindex_later()
        log.info("%s: XONG hỏi tiếp (%.0fs)", qid, now() - t0)
        return {"qid": qid, "status": "done"}

    def _append_round(self, qid, stem, thread, kind, q, body, used_model, model, t0, mk, mode=None) -> str:
        with self.io_lock:
            thread = self.load_thread(qid) or thread
            thread.append({"kind": kind, "v": int(mk.get("v", 1)), "q": q, "body": body, "ts": ts_str(),
                           "mode": mode or mk.get("mode", ""), "model": model_short(used_model or model),
                           "minutes": max(1, round((now() - t0) / 60))})
            self.save_thread(qid, thread)
            return self.render_answer(qid, stem, thread,
                                      {"v": int(mk.get("v", 1)), "hash": mk.get("hash", ""), "status": "done",
                                       "mode": mk.get("mode", ""), "model": mk.get("model", "")})

    def preflight(self) -> str | None:
        """Kiểm tra đăng nhập (0 token). -> None nếu ổn, ngược lại lý do."""
        if self.cfg.get("runner") == "routine":
            return None
        if not self.runner.exe:
            return "chua cai claude"
        if now() < self.preflight_ok_until:
            return None
        st = self.runner.auth_status()
        if not st:
            return None
        if st.get("loggedIn") is False:
            return "chua dang nhap"
        meth = str(st.get("authMethod", "")).lower()
        if not self.cfg.get("allow_api_key") and ("api" in meth and "key" in meth or meth == "console"):
            return "dang dung API key (se ton tien API) — dang nhap lai bang tai khoan Claude: claude auth login"
        self.preflight_ok_until = now() + 600
        return None

    def run_with_fallback(self, prompt: str, mode: str, model: str, run_dir: Path, answer_tmp: Path,
                          job: str, extra_dirs: list | None = None, resume: str | None = None,
                          max_turns: int | None = None, cur: dict | None = None) -> RunResult:
        res = self._run_main(prompt, mode, model, run_dir, answer_tmp, job, extra_dirs, resume, max_turns, cur)
        if self.answer_ok(answer_tmp) and res.session_id and mode in (self.cfg.get("review_modes") or []):
            res = self.review_pass(res, mode, model, run_dir, answer_tmp, job, extra_dirs, cur)
        return res

    def review_pass(self, res: RunResult, mode: str, model: str, run_dir: Path, answer_tmp: Path, job: str,
                    extra_dirs: list | None, cur: dict | None) -> RunResult:
        """Lượt 2: reviewer PD senior đọc lại, kiểm chứng, bổ sung chỗ thiếu — sửa thẳng vào file."""
        backup = run_dir / "answer_v1.md"
        shutil.copy(answer_tmp, backup)
        q = (cur or {}).get("question", "")
        prompt = "\n".join([
            "RÀ SOÁT & HOÀN THIỆN câu trả lời bạn vừa viết (đóng vai reviewer Physical Design senior, khắt khe).",
            f"File câu trả lời: {answer_tmp}",
            "Câu hỏi:", "<<<", q[:4000], ">>>", "",
            "Kiểm tra lần lượt và SỬA THẲNG vào file (Edit), không viết lại từ đầu nếu không cần:",
            "1. Đủ từng ý (1),(2)… của câu hỏi chưa? Ý nào trả lời chung chung/thiếu số liệu → bổ sung cụ thể.",
            "2. Mỗi lệnh/tuỳ chọn: đã tra tài liệu trên máy chưa (pd_index.py search/show)? Sai → sửa; chưa thấy → "
            "đánh dấu ⚠️ và cách kiểm (`help <lệnh>`).",
            "3. Mỗi con số: khớp nguồn (file:dòng) chưa? Phép tính đúng chưa? Tính lại.",
            "4. '🛠 Áp dụng ngay' có chạy được ngay không (thứ tự lệnh, biến, đường dẫn, cách kiểm tra + con số kỳ vọng, "
            "cách quay lui)? Thiếu → bổ sung.",
            "5. Còn thiếu góc nhìn quan trọng (rủi ro, ảnh hưởng setup/hold/power/area/DRC, khác biệt corner/phiên bản, "
            "phương án thay thế)? Có mâu thuẫn nội bộ? → bổ sung/sửa. Nếu cần, tìm thêm trên máy/web.",
            "6. Kết luận có trả lời thẳng, rõ ràng, nhất quán với phần chi tiết không?",
            "Cuối file (trước '## Bài học' nếu có) thêm mục '## 🔎 Đã rà soát' liệt kê ngắn những gì đã sửa/bổ sung.",
            f"Ghi kết quả vào file: {answer_tmp}",
            "Cuối cùng chỉ in: XONG",
        ])
        if cur is not None:
            cur["phase"] = "review"
        log.info("   rà soát & hoàn thiện câu trả lời…")
        cwd = self.root
        add_dirs = [d for d in (self.data_dir, run_dir, self.tools, *(extra_dirs or [])) if d and Path(d).exists()]
        deny = [self.data_dir] if self.data_dir else []

        def progress(pg):
            if cur:
                self.progress[cur["qid"]] = pg
                self.show_progress(cur, pg, answer_tmp)
        r2 = self.runner.run(prompt, res.model or model, job, cwd, add_dirs, deny, self.allowed_tools(web=True),
                             run_dir, tag="_review", resume=res.session_id,
                             max_turns=int(self.cfg.get("review_max_turns", 40)), on_progress=progress)
        if cur is not None:
            cur.pop("phase", None)
        old_len = len(read_text(backup))
        if self.answer_ok(answer_tmp) and len(read_text(answer_tmp)) >= 0.8 * old_len:
            log.info("   rà soát xong (%s, %d → %d ký tự)", r2.kind, old_len, len(read_text(answer_tmp)))
            res.session_id = r2.session_id or res.session_id
            return res
        log.warning("   rà soát không đạt (%s) — giữ bản đầu", r2.kind)
        shutil.copy(backup, answer_tmp)
        return res

    def _run_main(self, prompt: str, mode: str, model: str, run_dir: Path, answer_tmp: Path, job: str,
                  extra_dirs: list | None, resume: str | None, max_turns: int | None,
                  cur: dict | None) -> RunResult:
        cwd = self.root
        add_dirs = [d for d in (self.data_dir, run_dir, self.tools, *(extra_dirs or []))
                    if d and Path(d).exists()]
        deny = [self.data_dir] if self.data_dir else []
        allowed = self.allowed_tools(web=True)
        cur = cur or {}

        def progress(pg):
            if not cur:
                return
            self.progress[cur["qid"]] = pg
            self.show_progress(cur, pg, answer_tmp)

        res = self.runner.run(prompt, model, job, cwd, add_dirs, deny, allowed, run_dir, on_progress=progress,
                              resume=resume, max_turns=max_turns)
        if resume and not res.ok and res.kind not in ("limit", "login") and not self.answer_ok(answer_tmp):
            log.info("   không tiếp tục được phiên cũ -> chạy phiên mới")
            res = self.runner.run(prompt, model, job, cwd, add_dirs, deny, allowed, run_dir, tag="_new",
                                  on_progress=progress, max_turns=max_turns)
        fb = self.cfg["models"].get("fallback", "sonnet")
        if res.kind == "limit" and model_short(model) != model_short(fb):
            log.warning("   hết hạn mức %s -> chuyển %s", model_short(model), fb)
            res = self.runner.run(prompt, fb, job, cwd, add_dirs, deny, allowed, run_dir, tag="_fb",
                                  on_progress=progress)
            res.model = res.model or fb
        self.rescue_answer(answer_tmp, run_dir, cur)
        if res.kind in ("max_turns", "ok", "timeout") and not self.answer_ok(answer_tmp) and res.session_id \
                and not (res.ok and len(res.text.strip()) >= 400):
            log.info("   chưa có file trả lời (%s) -> yêu cầu viết câu trả lời với thông tin đã có", res.kind)
            fin = ("Bạn đã dừng mà chưa ghi file trả lời. Hãy viết NGAY câu trả lời hoàn chỉnh nhất có thể "
                   "với thông tin đã thu thập (bắt đầu bằng '## ✅ Kết luận', ghi rõ phần nào chưa kịp kiểm chứng) "
                   f"vào file: {answer_tmp}\nCuối cùng in: XONG")
            res2 = self.runner.run(fin, res.model or model, job, cwd, add_dirs, deny, ["Write", "Edit", "Read"],
                                   run_dir, tag="_fin", resume=res.session_id, max_turns=8)
            self.rescue_answer(answer_tmp, run_dir, cur)
            if self.answer_ok(answer_tmp):
                res2.ok, res2.kind = True, "ok" if res.kind != "max_turns" else "max_turns"
                res2.model = res2.model or res.model
                return res2
        return res

    def rescue_answer(self, answer_tmp: Path, run_dir: Path, cur: dict | None = None) -> None:
        """Claude ghi nhầm chỗ (file .md khác trong run_dir, hoặc thẳng vào Qnnn_traloi.md) -> lấy lại."""
        if self.answer_ok(answer_tmp):
            return
        cands = [f for f in run_dir.glob("*.md") if f.name != answer_tmp.name]
        cur = cur or {}
        if cur:
            a = self.qdir / f"{cur['qid']}_traloi.md"
            if a.exists() and "pd_bridge" not in read_text(a)[-400:]:
                cands.append(a)
        for f in sorted(cands, key=lambda f: -f.stat().st_size):
            if self.answer_ok(f):
                answer_tmp.write_text(read_text(f), encoding="utf-8")
                log.info("   lấy câu trả lời từ %s", f)
                return

    @staticmethod
    def answer_ok(p: Path) -> bool:
        try:
            return p.exists() and len(norm_text(read_text(p))) >= 200
        except OSError:
            return False

    def handle_result(self, info: dict, res: RunResult, model: str) -> dict:
        qid, stem, mode, v = info["qid"], info["stem"], info["mode"], info["v"]
        answer_tmp = Path(info["answer_tmp"])
        retry = self.state.sub("retry")
        if self.answer_ok(answer_tmp) or (res.ok and len(res.text.strip()) >= 400):
            body = read_text(answer_tmp) if self.answer_ok(answer_tmp) else res.text
            note = ""
            if res.kind == "max_turns":
                note = " · ⚠️ hết số lượt, có thể chưa đầy đủ"
            info["sid"] = res.session_id
            self.finalize(info, body, res.model or model, note)
            with self.state.lock:
                retry.pop(qid, None)
                self.state.save()
            return {"qid": qid, "status": "done"}
        # ---- thất bại
        if res.kind == "limit":
            until = res.reset_at or (now() + 30 * 60)
            self.state.set("paused_until", until)
            self.state.set("pause_reason", "limit")
            log.warning("Loi (limit): hết hạn mức gói Claude — tạm dừng đến %s", ts_str(until))
            self.write_placeholder(qid, stem, mode, v, "waiting",
                                   f"⏸ Hết hạn mức gói Claude — sẽ tự làm lại lúc {ts_str(until, '%H:%M %d/%m')}.")
            return {"qid": qid, "status": "limit"}
        if res.kind == "login":
            self.preflight_ok_until = 0
            self.state.set("paused_until", now() + 10 * 60)
            self.state.set("pause_reason", "login")
            log.error("Loi: chua dang nhap Claude Code (%s) — mở PowerShell, gõ `claude` rồi đăng nhập",
                      res.text[:200])
            self.write_placeholder(qid, stem, mode, v, "waiting",
                                   "⚠️ Claude Code trên máy ngoài chưa đăng nhập — sẽ tự thử lại sau khi đăng nhập.")
            return {"qid": qid, "status": "login"}
        with self.state.lock:
            rt = retry.setdefault(qid, {"n": 0})
            rt["n"] += 1
            n = rt["n"]
            rt["next"] = now() + min(600, (15 if res.kind == "timeout" else 60 if res.kind == "transient" else 300) * n)
            self.state.save()
        log.error("%s: thất bại (%s, lần %d): %s", qid, res.kind, n, res.text[:300])
        self.last_error = f"{qid}: {res.kind} — {res.text[:200]}"
        limit_n = int(self.cfg.get("max_attempts_transient" if res.kind in ("timeout", "transient")
                                   else "max_attempts", 3))
        if n >= limit_n:
            body = (f"## Tóm tắt\n\n❌ Không tạo được câu trả lời sau {n} lần thử ({res.kind}).\n\n"
                    f"Chi tiết lỗi:\n\n```\n{res.text[:1500]}\n```\n\n"
                    f"Xem log: `{self.home / 'watcher.log'}` và `{info['run_dir']}`.\n"
                    "Muốn thử lại: thêm/sửa một dòng trong file câu hỏi rồi lưu.")
            self.finalize(info, body, res.model or model, " · ❌ lỗi", status="error")
            with self.state.lock:
                retry.pop(qid, None)
                self.state.save()
            return {"qid": qid, "status": "error"}
        self.write_placeholder(qid, stem, mode, v, "waiting",
                               f"⚠️ Lần chạy {n} lỗi ({res.kind}) — sẽ tự thử lại lúc {ts_str(rt['next'], '%H:%M')}.")
        return {"qid": qid, "status": "retry"}

    def finalize(self, info: dict, body: str, model: str, note: str = "", status: str = "done") -> None:
        qid, stem, mode, v = info["qid"], info["stem"], info["mode"], info["v"]
        mins = max(1, round((now() - info.get("started", now())) / 60))
        body, verified = self.clean_body(body, status == "done")
        proj = self.proj_of(qid)
        kind = "main" if v == 1 else "edit"
        with self.io_lock:
            rounds = self.load_thread(qid)
            rounds.append({"kind": kind, "v": v, "q": info["question"][:8000], "body": body, "ts": ts_str(),
                           "mode": mode, "model": model_short(model), "minutes": mins, "note": note,
                           "status": status})
            self.save_thread(qid, rounds)
            text = self.render_answer(qid, stem, rounds, {"v": v, "hash": info["hash"], "status": status,
                                                         "mode": mode, "model": model_short(model)},
                                      rating=("chua", ""))
            self.state.sub("last_q")[qid] = {"text": info["question"][:6000], "hash": info["hash"], "v": v}
            if info.get("sid"):
                self.state.sub("sessions")[qid] = info["sid"]
            self.state.save()
            self.after_answer(qid, proj, stem, text, v, body, verified)
            rec = {"type": "answer", "q": qid, "v": v, "ts": ts_str(fmt="%Y-%m-%d %H:%M:%S"), "t": now(),
                   "name": stem, "mode": mode, "model": model_short(model), "status": status, "minutes": mins,
                   "proj": proj, "question": info["question"][:1500], "summary": summary_section(body)}
            self.journal(rec)
            with open(self.tong_hop / "nhat_ky.md", "a", encoding="utf-8") as f:
                f.write(f"- {ts_str()} **{qid}** v{v} [{mode}/{model_short(model)}, {mins}′] {stem}"
                        f"{' — ❌ lỗi' if status == 'error' else ''}\n")
        log.info("%s: %s -> %s_traloi.md (%d phút, project %s)", qid, "XONG" if status == "done" else "LỖI",
                 qid, mins, proj)
        self.render_learning(proj)
        self.reindex_later()

    # ------------------------------------------------------------------ luồng hỏi đáp trong 1 file
    def clean_body(self, body: str, verify: bool) -> tuple[str, dict]:
        body = MARK_RX.sub("", body).strip()
        body = re.sub(r"\A#\s[^\n]*\n+", "", body)          # bỏ tiêu đề H1 Claude tự thêm (file đã có)
        body = re.sub(r"\n?XONG\s*$", "", body).rstrip()
        body = escape_followups(RATING_RX.sub("", body))
        body = re.sub(r"\n## Kiểm chứng tự động \(script.*?(?=\n## |\Z)", "", body, flags=re.S).rstrip()
        verified: dict = {}
        if verify:
            try:
                ver, verified = pd_store.verify_commands(body, self.db_path())
                body += ver
            except Exception as e:  # noqa: BLE001
                log.warning("Kiểm chứng lệnh lỗi: %s", e)
        return body, verified

    def thread_file(self, qid: str) -> Path:
        return self.store.pdir(self.proj_of(qid)) / "hoi_dap" / f"{qid}.thread.json"

    def load_thread(self, qid: str) -> list[dict]:
        try:
            return json.loads(self.thread_file(qid).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []

    def save_thread(self, qid: str, rounds: list[dict]) -> None:
        f = self.thread_file(qid)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(rounds, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, f)

    @staticmethod
    def quote(t: str, limit: int = 3000) -> str:
        t = norm_text(t)[:limit]
        return "\n".join("> " + l if l.strip() else ">" for l in t.split("\n"))

    def render_answer(self, qid: str, stem: str, rounds: list[dict], mark: dict,
                      rating: tuple[str, str] | None = None, pending: dict | None = None) -> str:
        """File trả lời: lượt MỚI NHẤT ở đầu (đầy đủ), các lượt cũ thu gọn trong <details>, đánh giá ở cuối.
        Giữ đánh giá + dòng '>>' người hỏi đang gõ dở trong file hiện tại."""
        a = self.qdir / f"{qid}_traloi.md"
        cur_text = ""
        try:
            cur_text = read_text(a) if a.exists() else ""
        except OSError:
            pass
        if rating is None:
            rating = parse_rating(cur_text) if cur_text else ("chua", "")
        typed = parse_followups(cur_text) if cur_text else ""
        if pending and typed:
            typed = "\n".join(l for l in typed.split("\n") if l.strip() and l.strip() not in pending.get("q", ""))
        proj = self.proj_of(qid)
        main_v = mark.get("v", 1)
        out = [f"# {qid} — {stem}", ""]
        older = list(rounds)
        if pending:
            out += [f"> ⏳ **Đang trả lời** · {pending.get('label', '')} · project {proj}", "",
                    "## ❓ Câu hỏi", "", self.quote(pending.get("q", "")), "", pending.get("body", "")]
        elif older:
            last = older.pop()
            kind = {"main": "Câu hỏi", "edit": f"Câu hỏi (đã sửa, lần {last.get('v')})",
                    "followup": "Hỏi tiếp"}.get(last.get("kind"), "Câu hỏi")
            out += [f"> 🆕 {last.get('ts', '')} · mode **{last.get('mode', '')}** · model {last.get('model', '')} · "
                    f"{last.get('minutes', 1)} phút · project {proj}{last.get('note', '')}"
                    + (f" · {len(rounds)} lượt" if len(rounds) > 1 else ""), "",
                    f"## ❓ {kind}", "", self.quote(last.get("q", "")), "", last.get("body", "")]
        if older:
            out += ["", "---", "", "<details>", f"<summary>📜 Các lượt trước ({len(older)}) — bấm để mở</summary>", ""]
            for i, r in enumerate(reversed(older)):
                n = len(older) - i
                kind = {"main": "Câu hỏi gốc", "edit": f"Câu hỏi đã sửa (lần {r.get('v')})",
                        "followup": "Hỏi tiếp"}.get(r.get("kind"), "Câu hỏi")
                body = re.sub(r"(?m)^(#{1,3}) ", lambda m: "#" * min(6, len(m.group(1)) + 2) + " ", r.get("body", ""))
                out += [f"### Lượt {n} · {kind} · {r.get('ts', '')}", "", self.quote(r.get("q", ""), 1500), "",
                        body, ""]
            out += ["</details>"]
        out += ["", "---", "",
                "**Hỏi tiếp:** viết một dòng bắt đầu bằng `>>` (ví dụ `>> còn trường hợp OCV thì sao?`) rồi lưu file.",
                ""]
        if typed:
            out += ["\n".join(">> " + l for l in typed.split("\n") if l.strip()), ""]
        out += ["**Đánh giá** — sửa `chua` thành `dung`, `mot_phan` hoặc `sai` (tuỳ chọn thêm ghi chú sau `ghi_chu:`):",
                "", f"danh_gia: {rating[0]}", "", f"ghi_chu: {rating[1]}".rstrip(), "",
                f"<!-- pd_bridge q={qid} v={main_v} hash={mark.get('hash', '')} status={mark.get('status', 'done')} "
                f"mode={mark.get('mode', '')} model={mark.get('model', '')} -->", ""]
        text = "\n".join(out)
        atomic_write(a, text)
        return text

    def show_progress(self, cur: dict, pg: dict, answer_tmp: Path | None, stopped: bool = False) -> None:
        """Tiến độ + bản nháp đang viết, ghi vào file trả lời (lượt mới ở đầu)."""
        qid = cur["qid"]
        if not stopped:
            if self.runner.aborting:
                return
            self.live[qid] = (cur, dict(pg), answer_tmp)
        el = int(now() - pg.get("t0", now()))
        steps = "\n".join(f"- `{x}`" for x in pg.get("recent") or []) or "- _đang đọc câu hỏi, dữ liệu và suy nghĩ…_"
        draft = ""
        try:
            if answer_tmp and answer_tmp.exists():
                d = read_text(answer_tmp).strip()
                lim = int(self.cfg.get("draft_chars", 8000))
                if d:
                    draft = ("\n\n---\n\n**📝 Bản nháp (đang viết/rà soát tiếp, sẽ được thay bằng bản hoàn chỉnh):**\n\n"
                             + escape_followups(d[:lim]) + ("\n\n…" if len(d) > lim else ""))
        except OSError:
            pass
        phase = "🔎 **Đang rà soát & hoàn thiện** (bản đầu đã xong)" if cur.get("phase") == "review" else \
            "⏳ **Đang xử lý**"
        if stopped:
            phase = "⏸ **Hệ thống đã TẮT khi đang trả lời** — câu này sẽ được trả lời lại khi bật (`pdbat`). Đã làm"
        msg = (f"{phase} — {el // 60} phút {el % 60:02d} giây · {pg.get('steps', 0)} bước nghiên cứu · "
               f"mode {cur['mode']} · model {model_short(cur['model'])}\n\nCác bước gần nhất:\n{steps}\n\n"
               + ("_Đã dừng lúc " + ts_str() + "._" if stopped else
                  f"_Tự cập nhật mỗi {int(float(self.cfg.get('progress_seconds', 10)))} giây; Claude im lặng quá "
                  f"{self.cfg.get('stall_minutes', 5)} phút thì tự chạy lại._") + draft)
        with self.io_lock:
            rounds = self.load_thread(qid)
            if rounds:
                label = {"followup": "hỏi tiếp", "edit": "câu hỏi đã sửa"}.get(cur.get("pending_kind"), "")
                mk = parse_marker(read_text(self.qdir / f"{qid}_traloi.md")) if \
                    (self.qdir / f"{qid}_traloi.md").exists() else {}
                status = "done" if cur.get("pending_kind") == "followup" else "running"
                self.render_answer(qid, cur["stem"], rounds,
                                   {"v": cur["v"], "hash": mk.get("hash", "") if status == "done" else "",
                                    "status": status, "mode": cur["mode"], "model": model_short(cur["model"])},
                                   rating=None if status == "done" else ("chua", ""),
                                   pending={"q": cur.get("question", ""), "body": msg, "label": label})
            else:
                self.write_placeholder(qid, cur["stem"], cur["mode"], cur["v"], "running", msg)

    def reindex_later(self) -> None:
        """Chỉ mục kho chạy nền sau khi đã gửi câu trả lời (không làm chậm việc trả lời)."""
        def run():
            try:
                with self.index_lock:
                    self.store.reindex()
            except BaseException as e:  # noqa: BLE001
                log.warning("Chỉ mục kho lỗi: %s", e)
        self.reindex_thread = threading.Thread(target=run, daemon=True)
        self.reindex_thread.start()

    def wait_reindex(self) -> None:
        t = getattr(self, "reindex_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout=20)

    def after_answer(self, qid: str, proj: str, stem: str, full_text: str, v: int, body: str,
                     verified: dict) -> None:
        """Lưu kho (vĩnh viễn) + gom sổ tay. 0 token."""
        try:
            qf = next((f for f in self.qdir.glob(f"{qid}_*.md") if Q_RX.match(f.name)
                       and not ANS_RX.match(f.name) and not OLD_RX.match(f.name)), None)
            self.store.archive(proj, qid, stem, read_text(qf) if qf else "", full_text, v)
            terms, cmds = pd_store.lesson_items(body)
            if terms or cmds:
                self.notebook.add(qid, terms, cmds, verified)
        except Exception as e:  # noqa: BLE001
            log.warning("Lưu kho/sổ tay lỗi: %s", e)

    def latest_ratings(self) -> dict[str, str]:
        rv: dict[str, int] = {}
        out: dict[str, str] = {}
        recs = self.read_journal()
        for r in recs:
            if r.get("type") == "answer":
                rv[r["q"]] = int(r.get("v", 1))
        for r in recs:
            if r.get("type") == "rating" and int(r.get("v", 1)) == rv.get(r["q"]):
                out[r["q"]] = r.get("danh_gia", "chua")
        return out

    def render_learning(self, proj: str | None = None) -> None:
        with self.io_lock:
            self._render_learning(proj)

    def _render_learning(self, proj: str | None = None) -> None:
        try:
            recs = self.read_journal()
            for p in ([proj] if proj else self.store.projects()):
                self.store.render_muc_luc(p, recs)
            self.notebook.render(self.latest_ratings())
        except Exception as e:  # noqa: BLE001
            log.warning("Cập nhật mục lục/sổ tay lỗi: %s", e)

    def journal(self, rec: dict) -> None:
        with self.io_lock, open(self.tong_hop / "nhat_ky.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def record_rating(self, qid: str, v: int, rating: str, note: str, file: str) -> None:
        key = f"{qid}:{v}"
        ratings = self.state.sub("ratings")
        if ratings.get(key) == [rating, note]:
            return
        if rating == "chua" and key not in ratings:
            ratings[key] = [rating, note]
            self.state.save()
            return
        ratings[key] = [rating, note]
        self.state.save()
        self.journal({"type": "rating", "q": qid, "v": v, "danh_gia": rating, "ghi_chu": note,
                      "file": file, "ts": ts_str(fmt="%Y-%m-%d %H:%M:%S"), "t": now()})
        log.info("%s v%d: đánh giá = %s%s", qid, v, rating, f" ({note})" if note else "")
        a = self.qdir / f"{qid}_traloi.md"
        if file == "traloi" and a.exists():
            try:
                hd = self.store.pdir(self.proj_of(qid)) / "hoi_dap" / f"{qid}_traloi.md"
                atomic_write(hd, read_text(a))
            except Exception:  # noqa: BLE001
                pass
        self.render_learning(self.proj_of(qid))

    # ------------------------------------------------------------------ routine runner (dự phòng)
    def run_routine(self, prompt: str, answer_tmp: Path, job: str) -> RunResult:
        res = RunResult()
        rc = self.cfg.get("routine") or {}
        if not rc.get("url"):
            res.kind, res.text = "error", "runner=routine nhưng thiếu routine.url trong config.json"
            return res
        text = (prompt + "\n\n(Được gọi qua routine từ PD_Bridge watcher trên máy này. "
                f"Ghi câu trả lời vào đúng file {answer_tmp}.)")
        headers = {"Content-Type": "application/json", **(rc.get("headers") or {})}
        if rc.get("token"):
            headers["Authorization"] = f"Bearer {rc['token']}"
        req = urllib.request.Request(rc["url"], data=json.dumps({"text": text}).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                res.raw = r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            res.kind = "limit" if "429" in msg else ("login" if "401" in msg or "403" in msg else "transient")
            res.text = f"Gọi routine lỗi: {msg}"
            return res
        deadline = now() + int((self.cfg.get("timeout_min") or {}).get(job, 45)) * 60
        last_sig = None
        stable_since = None
        while now() < deadline:
            time.sleep(15)
            if answer_tmp.exists():
                st = answer_tmp.stat()
                sig = (st.st_size, st.st_mtime)
                if sig != last_sig:
                    last_sig, stable_since = sig, now()
                elif now() - stable_since >= 90 and self.answer_ok(answer_tmp):
                    res.ok, res.kind, res.model = True, "ok", "routine"
                    return res
        res.kind, res.text = "timeout", "Routine không ghi câu trả lời trong thời hạn"
        return res

    # ------------------------------------------------------------------ housekeeping (0 token)
    def refresh_ratings(self, rounds: dict[str, dict] | None = None) -> None:
        if rounds is None:
            _, rounds = self.scan()
        for qid, r in rounds.items():
            for key, fname in (("a", "traloi"), ("old", "traloi_cu")):
                p = r.get(key)
                if not p:
                    continue
                try:
                    t = read_text(p)
                except OSError:
                    continue
                mk = parse_marker(t)
                if mk.get("status") not in ("done", "error"):
                    continue
                rating, note = parse_rating(t)
                self.record_rating(qid, int(mk.get("v", 1)), rating, note, fname)

    def retention(self, rounds: dict[str, dict]) -> None:
        keep = int(self.cfg.get("keep_rounds", 5))
        order = sorted(rounds, key=qnum)
        if len(order) <= keep:
            return
        for qid in order[: len(order) - keep]:
            r = rounds[qid]
            if qid in self.active or self.chat_locked(qid):
                continue
            a = r.get("a")
            mk = parse_marker(read_text(a)) if a else {}
            if r.get("q") and mk.get("status") not in ("done", "error"):
                continue      # chưa xong -> giữ lại
            if r.get("q") and mk.get("hash") and mk["hash"] != text_hash(read_text(r["q"])):
                continue      # vừa bị sửa, chờ trả lời lại
            self.refresh_ratings({qid: r})
            dest = self.trash / ts_str(fmt="%Y%m%d-%H%M%S")
            dest.mkdir(parents=True, exist_ok=True)
            moved = 0
            for p in (r.get("q"), r.get("a"), r.get("old")):
                if p and p.exists():
                    try:
                        shutil.move(str(p), str(dest / p.name))
                        moved += 1
                    except OSError as e:
                        log.warning("Không chuyển được %s: %s", p.name, e)
            with self.state.lock:
                self.state.sub("last_q").pop(qid, None)
                self.state.sub("retry").pop(qid, None)
                self.state.save()
            log.info("%s: lượt cũ -> _cho_xoa (%d file)", qid, moved)

    def purge_trash(self) -> None:
        if not self.trash.exists():
            return
        limit = now() - int(self.cfg.get("trash_after_minutes", 60)) * 60
        for p in self.trash.iterdir():
            try:
                m = re.match(r"^(\d{8}-\d{6})$", p.name)
                t = dt.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp() if m else p.stat().st_mtime
                if t < limit:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                    log.info("Đã xoá %s", p.name)
            except Exception as e:  # noqa: BLE001
                log.warning("Chưa xoá được %s: %s", p.name, e)
        try:
            if not any(self.trash.iterdir()):
                self.trash.rmdir()
        except OSError:
            pass

    def prune_runs(self, keep: int = 60) -> None:
        dirs = sorted((d for d in self.runs.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
        for d in dirs[:-keep]:
            shutil.rmtree(d, ignore_errors=True)
        aliases = self.state.sub("aliases")
        for k in [k for k, v in aliases.items() if now() - v.get("ts", 0) > 48 * 3600]:
            aliases.pop(k, None)
        self.state.save()

    def update_cli(self) -> None:
        if not self.cfg.get("auto_update_cli", True) or not self.runner.exe or self.busy():
            return
        if now() - float(self.state.get("cli_update_last", 0)) < 86400:
            return
        self.state.set("cli_update_last", now())
        if self.busy():
            return
        try:
            r = subprocess.run([self.runner.exe, "update"], capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
                               encoding="utf-8", errors="replace", env=child_env(self.cfg),
                               creationflags=CREATE_NO_WINDOW)
            log.info("claude update: %s", (r.stdout + r.stderr).strip().splitlines()[-1:] or "-")
            self.runner._flags = None
            self.runner.compat = False
        except Exception as e:  # noqa: BLE001
            log.warning("claude update lỗi: %s", e)

    def housekeeping(self, force: bool = False) -> None:
        if not force and now() - self.last_house < 3600:
            return
        self.last_house = now()
        threading.Thread(target=self.update_cli, daemon=True).start()   # nền: không chặn việc nhận câu hỏi
        try:
            self.purge_trash()
            self.prune_runs()
            self.refresh_ratings()
            n = self.store.auto_learn(self.read_journal(), now(), int(self.cfg.get("auto_learn_days", 14))) \
                if self.cfg.get("use_history") else 0
            if n:
                log.info("Tự học: %d mục ◻️ chưa xác nhận trong KIEN_THUC.md của các project", n)
        except Exception as e:  # noqa: BLE001
            log.warning("housekeeping: %s", e)

    def index_check(self) -> None:
        if self.index_proc is not None:
            rc = self.index_proc.poll()
            if rc is None:
                return
            self.index_proc = None
            if getattr(self, "_index_log", None):
                self._index_log.close()
                self._index_log = None
            self.state.set("index_last", now())
            try:
                last = (self.home / "index.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1]
            except Exception:
                last = ""
            log.info("Chỉ mục xong (rc=%s) %s", rc, last[:300])
            return
        if not self.data_dir or not self.data_dir.is_dir():
            return
        db = self.db_path()
        due = now() - float(self.state.get("index_last", 0)) > float(self.cfg.get("index_refresh_hours", 24)) * 3600
        if db and db.exists() and not due:
            return
        py = self.cfg.get("python") or sys.executable
        cmd = [py, str(TOOLS_DIR / "pd_index.py"), "--data-dir", str(self.data_dir)]
        if self.cfg.get("index_db"):
            cmd += ["--db", str(self.cfg["index_db"])]
        cmd += ["build"]
        logf = open(self.home / "index.log", "a", encoding="utf-8")
        logf.write(f"\n--- {ts_str()} build\n")
        logf.flush()
        kw = {"creationflags": CREATE_NO_WINDOW | 0x00004000} if IS_WIN else {}  # BELOW_NORMAL
        try:
            self.index_proc = subprocess.Popen(cmd, stdout=logf, stderr=logf, env=child_env(self.cfg), **kw)
            self._index_log = logf
            log.info("Làm mới chỉ mục tài liệu (%s)…", self.data_dir)
        except OSError as e:
            log.warning("Không chạy được pd_index: %s", e)
            self.state.set("index_last", now())

    # ------------------------------------------------------------------ weekly
    def weekly_cutoff(self, ref: float | None = None) -> tuple[float, str]:
        w = self.cfg.get("weekly") or {}
        wd = int(w.get("weekday", 4))
        hh, mm = (int(x) for x in str(w.get("time", "16:55")).split(":"))
        base = dt.datetime.fromtimestamp(ref or now())
        c = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        c -= dt.timedelta(days=(c.weekday() - wd) % 7)
        if c > base:
            c -= dt.timedelta(days=7)
        iso = c.isocalendar()
        return c.timestamp(), f"{iso[0]}-W{iso[1]:02d}"

    def weekly_due(self) -> bool:
        _, wid = self.weekly_cutoff()
        return self.state.get("weekly_done") != wid

    def read_journal(self) -> list[dict]:
        p = self.tong_hop / "nhat_ky.jsonl"
        out = []
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out

    def weekly(self, force: bool = False) -> str:
        cutoff, wid = self.weekly_cutoff()
        if force:
            cutoff = now()
            iso = dt.datetime.fromtimestamp(cutoff).isocalendar()
            wid = f"{iso[0]}-W{iso[1]:02d}"
        prev = float(self.state.get("weekly_cutoff") or (cutoff - 7 * 86400))
        if force:
            prev = min(prev, cutoff - 7 * 86400)
        self.refresh_ratings()
        recs = self.read_journal()
        answers = [r for r in recs if r.get("type") == "answer" and prev < r.get("t", 0) <= cutoff]
        ratings: dict[str, tuple] = {}
        for r in recs:
            if r.get("type") == "rating":
                ratings[f"{r['q']}:{r['v']}"] = (r.get("danh_gia", "chua"), r.get("ghi_chu", ""))
        out = self.tong_hop / f"{wid}.md"
        if not answers:
            log.info("Tổng hợp tuần %s: không có câu hỏi — bỏ qua (0 token)", wid)
            self._weekly_done(wid, cutoff)
            return "empty"
        latest: dict[str, dict] = {}
        for r in answers:
            latest[r["q"]] = r
        cnt = {"dung": 0, "mot_phan": 0, "sai": 0, "chua": 0}
        digest = []
        for qid in sorted(latest, key=qnum):
            r = latest[qid]
            vr = [(v, ratings.get(f"{qid}:{v}", ("chua", ""))) for v in range(1, r["v"] + 1)]
            rating, note = vr[-1][1]
            cnt[rating if rating in cnt else "chua"] += 1
            older = "; ".join(f"v{v}={rt[0]}" + (f" ({rt[1]})" if rt[1] else "") for v, rt in vr[:-1])
            loc = self.store.answer_path(r.get("proj", pd_store.DEFAULT_PROJECT), qid) or self.qdir / f"{qid}_traloi.md"
            digest.append(
                f"### {qid} — {r['name']} (v{r['v']}, {r['mode']}/{r['model']}, danh_gia: {rating}"
                f"{', ghi_chu: ' + note if note else ''}{', bản trước: ' + older if older else ''})\n"
                f"Câu hỏi: {r['question'][:600]}\n"
                f"Tóm tắt trả lời: {r['summary'][:900]}\n"
                f"File: {loc if loc.exists() else '(đã lưu trữ)'}")
        stats = (f"Tổng: {len(latest)} câu hỏi · ✅ dung {cnt['dung']} · ◑ mot_phan {cnt['mot_phan']} · "
                 f"❌ sai {cnt['sai']} · chưa đánh giá {cnt['chua']}")
        prompt = "\n".join([
            f"TỔNG HỢP TUẦN {wid} cho PD_Bridge (xem CLAUDE.md, mục Tổng hợp tuần).",
            f"Thống kê (đã tính sẵn): {stats}",
            "",
            f"1) Viết file {out} gồm: tiêu đề, thống kê trên, bảng các câu hỏi (Q, chủ đề, mode, đánh giá), "
            "'Kết luận chính' theo từng chủ đề, 'Bài học/kinh nghiệm', 'Cần xác nhận thêm' (các câu mot_phan/chua).",
            f"2) Cập nhật {self.kien_thuc / 'kinh_nghiem.md'}: CHỈ thêm mục '- ✅ ...' cho điều từ câu hỏi có "
            "danh_gia=dung (nếu cần chi tiết chính xác, đọc file trả lời nếu còn); với danh_gia=sai thêm '- ❌ ...' "
            "vào phần 'Đã bị đánh giá sai — tránh lặp'; mot_phan chỉ thêm phần mà ghi_chu xác nhận đúng. "
            "Gộp trùng, xếp theo chủ đề, mỗi mục 1–3 dòng, ghi (Qnnn, tuần). Không thêm điều chưa xác nhận.",
            "3) Nếu thấy thông tin bối cảnh dự án mới (tên block, flow, tool version) đã được xác nhận, cập nhật "
            f"{self.kien_thuc / 'boi_canh_du_an.md'}.",
            "Không dùng web. Cuối cùng in: XONG",
            "",
            "DỮ LIỆU TUẦN:",
            *digest,
        ])
        run_dir = self.runs / f"weekly-{wid}-{ts_str(fmt='%Y%m%d-%H%M%S')}"
        model = self.cfg["models"].get("tong_hop", "sonnet")
        before = out.stat().st_mtime if out.exists() else 0
        if self.cfg.get("runner") == "routine":
            res = RunResult()
            res.kind = "skip"
        else:
            res = self.runner.run(prompt, model, "tong_hop", self.root, [self.kien_thuc, self.tong_hop, run_dir],
                                  [self.data_dir] if self.data_dir else [],
                                  ["Read", "Write", "Edit", "Glob", "Grep"], run_dir)
        if res.kind == "limit":
            self.state.set("paused_until", res.reset_at or now() + 1800)
            log.warning("Tổng hợp tuần %s: hết hạn mức — làm lại sau", wid)
            return "limit"
        if res.kind == "login":
            return "login"
        if not out.exists() or out.stat().st_mtime == before:
            # dự phòng 0 token: viết bản tổng hợp thô
            atomic_write(out, f"# Tổng hợp tuần {wid}\n\n{stats}\n\n"
                              "_(Bản tự động do không gọi được Claude — chưa cập nhật kien_thuc.)_\n\n"
                              + "\n\n".join(digest) + "\n")
            log.warning("Tổng hợp tuần %s: Claude không ghi file (%s) — đã ghi bản thô", wid, res.kind)
        else:
            log.info("Tổng hợp tuần %s: xong -> %s", wid, out.name)
        self._weekly_done(wid, cutoff)
        return "done"

    def _weekly_done(self, wid: str, cutoff: float) -> None:
        with self.state.lock:
            self.state.d["weekly_done"] = wid
            self.state.d["weekly_cutoff"] = cutoff
            self.state.save()

    # ------------------------------------------------------------------ main loop
    def paused(self) -> bool:
        return now() < float(self.state.get("paused_until", 0))

    def _work(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            log.exception("Lỗi khi xử lý %s", args)
            self.last_error = f"lỗi nội bộ: {e}"
            qid = args[0] if args and isinstance(args[0], str) and re.match(r"^Q\d{3,}$", args[0]) else None
            if qid:
                self.internal_failure(qid, e)
        finally:
            name = args[0] if args and isinstance(args[0], str) else getattr(fn, "__name__", "")
            self.active.discard(name)
            self.progress.pop(name, None)
            self.live.pop(name, None)

    def internal_failure(self, qid: str, e: Exception) -> None:
        """Lỗi trong code (không phải Claude): thử lại vài lần, sau đó ghi lỗi rõ ràng thay vì treo."""
        retry = self.state.sub("retry")
        with self.state.lock:
            rt = retry.setdefault(qid, {"n": 0})
            rt["n"] += 1
            rt["next"] = now() + 60 * rt["n"]
            n = rt["n"]
            self.state.save()
        try:
            _, rounds = self.scan()
            r = rounds.get(qid, {})
            qf = r.get("q")
            if not qf:
                return
            stem = Q_RX.match(qf.name).group(2)
            txt = read_text(qf)
            mode, _ = self.resolve_mode(stem, txt)
            mk = parse_marker(read_text(r["a"])) if r.get("a") else {}
            v = int(mk.get("v", 1))
            if n >= int(self.cfg.get("max_attempts", 3)):
                info = {"qid": qid, "stem": stem, "mode": mode, "v": v, "hash": text_hash(txt),
                        "question": parse_mode(txt, "")[1], "run_dir": str(self.runs), "started": now()}
                self.finalize(info, f"## Tóm tắt\n\n❌ Lỗi nội bộ PD_Bridge sau {n} lần thử: `{e}`\n\n"
                                    f"Xem `{self.home / 'watcher.log'}`. Sửa/thêm một dòng trong câu hỏi để thử lại.",
                              "?", " · ❌ lỗi", status="error")
                retry.pop(qid, None)
                self.state.save()
            else:
                self.write_placeholder(qid, stem, mode, v, "waiting",
                                       f"⚠️ Lỗi nội bộ lần {n} (`{str(e)[:150]}`) — tự thử lại sau {n} phút.")
        except Exception:  # noqa: BLE001
            log.exception("internal_failure")

    def start_worker(self, name: str, fn, *args) -> None:
        self.active.add(name)
        t = threading.Thread(target=self._work, args=(fn, *args), daemon=True)
        self.workers[name] = t
        t.start()

    def busy(self) -> bool:
        self.workers = {k: t for k, t in self.workers.items() if t.is_alive()}
        return bool(self.workers)

    def free_slots(self) -> int:
        self.busy()
        return max(0, int(self.cfg.get("max_workers", 2)) - len(self.workers))

    def tick(self, ignore_debounce: bool = False, block: bool = False) -> None:
        new, rounds = self.scan()
        for p in new:                                   # 1) nhận câu hỏi mới (0 token)
            if ignore_debounce or self.ready(p):
                self.claim_new(p)
        if new:
            new, rounds = self.scan()
        self.retention(rounds)
        self.housekeeping()
        self.index_check()
        if self.paused():
            return
        if not block and self.free_slots() == 0:
            return
        if "weekly" in self.active:
            return
        jobs = self.pending_jobs(rounds, ignore_debounce)
        if jobs:
            why = self.preflight()
            if why:
                self.state.set("paused_until", now() + 10 * 60)
                self.state.set("pause_reason", why)
                log.error("Loi: %s — thử lại sau 10 phút", why)
                self.last_error = why
                fix = {"chua cai claude": "Máy ngoài chưa có Claude Code (hoặc watcher không tìm thấy lệnh `claude`). "
                                          "Chạy lại `cai_dat.ps1` trên máy ngoài.",
                       "chua dang nhap": "Claude Code trên máy ngoài CHƯA ĐĂNG NHẬP. Trên máy ngoài mở PowerShell, "
                                         "gõ `claude auth login` và đăng nhập tài khoản Claude."}.get(why, why)
                for qid, _ in jobs:
                    r = rounds[qid]
                    stem = Q_RX.match(r["q"].name).group(2)
                    mode, _ = self.resolve_mode(stem, read_text(r["q"]))
                    mk = parse_marker(read_text(r["a"])) if r.get("a") else {}
                    self.write_placeholder(qid, stem, mode, int(mk.get("v", 1)), "waiting",
                                           f"⚠️ Chưa chạy được: {fix}\n\nWatcher tự thử lại mỗi 10 phút.")
                self.write_status(force=True)
                return
            if block:
                qid, kind = jobs[0]
                self.active.add(qid)
                self._work(self.process, qid, kind)
                return
            for qid, kind in jobs[: self.free_slots()]:
                self.start_worker(qid, self.process, qid, kind)
            return
        if self.busy():
            return
        if self.weekly_due():
            if block:
                if getattr(self, "_weekly_tried", False):
                    return
                self._weekly_tried = True
                self.active.add("weekly")
                self._work(self.weekly)
            else:
                self.start_worker("weekly", self.weekly)

    def write_status(self, force: bool = False) -> None:
        """cau_hoi/_TRANG_THAI.md: tình trạng máy ngoài, xem được từ OneDrive (0 token)."""
        if not force and now() - self.last_status < 60:
            return
        self.last_status = now()
        t, auth = self.auth_cache
        if self.cfg.get("runner") != "routine" and self.runner.exe and (force or now() - t > 600):
            auth = self.runner.auth_status()
            self.auth_cache = (now(), auth)
        pu = float(self.state.get("paused_until", 0))
        pgs = dict(self.progress)
        try:
            tail = (self.home / "watcher.log").read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        except OSError:
            tail = []
        if auth.get("loggedIn"):
            auth_s = f"đã đăng nhập ({auth.get('authMethod', '')})"
        elif auth:
            auth_s = "**CHƯA ĐĂNG NHẬP** — trên máy ngoài chạy: `claude auth login`"
        else:
            auth_s = "không kiểm tra được"
        doing = ", ".join(sorted(self.active)) or "-"
        for q, pg in pgs.items():
            doing += f"; {q}: {int((now() - pg['t0']) // 60)} phút, {pg['steps']} bước, gần nhất: `{pg.get('last', '')}`"
        lines = [
            "# Trạng thái PD_Bridge (máy ngoài)", "",
            f"- Cập nhật lúc: **{ts_str(fmt='%Y-%m-%d %H:%M:%S')}** (ghi mỗi phút; giờ cũ = watcher đã dừng)",
            f"- Claude Code: `{self.runner.exe or 'KHÔNG THẤY'}` — {auth_s}",
            f"- Thư mục dữ liệu: `{self.data_dir}` " +
            ("✅" if self.data_dir and self.data_dir.is_dir() else "❌ không thấy"),
            f"- Đang làm: {doing}",
            (f"- Tạm dừng đến {ts_str(pu)} ({self.state.get('pause_reason')})" if pu > now() else "- Tạm dừng: không"),
            f"- Lỗi gần nhất: {self.last_error or '-'}",
            "", "## Nhật ký gần nhất", "```", *tail, "```", "",
        ]
        try:
            atomic_write(self.qdir / "_TRANG_THAI.md", "\n".join(lines))
        except OSError:
            pass

    def shutdown(self, reason: str) -> None:
        """Tắt ngay: dừng claude/chỉ mục đang chạy, ghi trạng thái ĐÃ TẮT (câu dở được làm lại khi bật)."""
        log.info("TẮT hệ thống: %s", reason)
        self.runner.abort_all()
        if self.index_proc and self.index_proc.poll() is None:
            kill_tree(self.index_proc)
        for qid, (cur, pg, tmp) in list(self.live.items()):
            try:
                self.show_progress(cur, pg, tmp, stopped=True)
            except Exception:  # noqa: BLE001
                log.exception("ghi trạng thái tắt %s", qid)
        write_off_status(self.cfg, reason)
        if IS_WIN:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)   # cho phép máy ngủ lại
            except Exception:  # noqa: BLE001
                pass

    def watch(self) -> int:
        off = off_flag(self.home)
        if off.exists():
            log.info("Hệ thống đang TẮT (pdtat) — không chạy. Bật: pdbat")
            return 0
        lock = SingleInstance(self.home / "watcher.lock")
        if not lock.acquire():
            log.info("Watcher khác đang chạy — thoát")
            return 0
        pidf = self.home / "watcher.pid"
        pidf.write_text(str(os.getpid()), encoding="utf-8")
        if self.cfg.get("prevent_sleep", True):
            prevent_sleep()
        stop = self.home / "stop"
        stop.unlink(missing_ok=True)
        log.info("PD_Bridge watcher BẬT | câu hỏi: %s | dữ liệu: %s | claude: %s",
                 self.qdir, self.data_dir, self.runner.exe)
        self.housekeeping(force=True)
        last_act = now()
        idle_stop = float(self.cfg.get("idle_stop_minutes", 0) or 0) * 60
        try:
            while True:
                try:
                    self.tick()
                    self.write_status()
                except Exception as e:  # noqa: BLE001
                    log.exception("tick lỗi")
                    self.last_error = f"tick lỗi: {e}"
                if off.exists():
                    self.shutdown("tắt bằng lệnh pdtat")
                    return 0
                busy = self.busy() or bool(self.active)
                if busy:
                    last_act = now()
                if stop.exists() and not busy:
                    stop.unlink(missing_ok=True)
                    log.info("Nhận yêu cầu dừng — thoát")
                    write_off_status(self.cfg, "dừng theo yêu cầu")
                    return 0
                if idle_stop and not busy and now() - last_act > idle_stop:
                    off.write_text(ts_str(), encoding="utf-8")
                    self.shutdown(f"tự tắt sau {idle_stop / 60:g} phút không có câu hỏi")
                    return 0
                if not busy and self._code_mtime() != self.code_mtime:
                    log.info("bridge.py đã cập nhật — khởi động lại")
                    return 3
                if self.cfg.get("prevent_sleep", True):
                    prevent_sleep()
                idle = now() - last_act > 600
                time.sleep(float(self.cfg.get("idle_poll_seconds" if idle else "poll_seconds", 3 if idle else 1)))
        finally:
            if read_pid(pidf) == os.getpid():
                pidf.unlink(missing_ok=True)
            lock.release()

    def run_once(self, now_flag: bool) -> None:
        """Xử lý hết việc đang chờ rồi thoát (dùng cho kiểm thử / chạy tay)."""
        if self.index_proc is None:
            self.index_check()
        if self.index_proc:
            self.index_proc.wait()
            self.index_check()
        for _ in range(200):
            self.tick(ignore_debounce=now_flag, block=True)
            if self.paused():
                break
            new, rounds = self.scan()
            if new:
                if not now_flag:
                    time.sleep(float(self.cfg.get("poll_seconds", 1)))   # chờ file ổn định
                continue
            if not self.pending_jobs(rounds, now_flag) and \
                    (not self.weekly_due() or getattr(self, "_weekly_tried", False)):
                deb = float(self.cfg.get("debounce_seconds", 2))
                fresh = [f for r in rounds.values() for f in (r.get("q"), r.get("a"))
                         if f and f.exists() and now() - f.stat().st_mtime < deb + 1]
                if fresh and not now_flag:
                    time.sleep(float(self.cfg.get("poll_seconds", 1)))   # file vừa sửa: chờ ổn định
                    continue
                break

    def status(self) -> str:
        new, rounds = self.scan()
        on = watcher_running(self.home)
        lines = ["Hệ thống          : " + ("🟢 ĐANG BẬT (tắt: pdtat)" if on else "⏹ ĐÃ TẮT (bật: pdbat)"),
                 f"Thư mục PD_Bridge : {self.root}",
                 f"Thư mục dữ liệu   : {self.data_dir}",
                 f"Cấu hình/nhật ký  : {self.home}",
                 f"claude            : {self.runner.exe}",
                 f"Runner            : {self.cfg.get('runner')}"]
        pu = float(self.state.get("paused_until", 0))
        if pu > now():
            lines.append(f"TẠM DỪNG đến {ts_str(pu)} ({self.state.get('pause_reason')})")
        db = self.db_path()
        if db and db.exists():
            try:
                s = pd_index.stats(db)
                lines.append(f"Chỉ mục           : {s['files']} file, {s['fulltext']} toàn văn, cập nhật {s.get('built_at')}")
            except Exception:
                pass
        else:
            lines.append("Chỉ mục           : chưa có")
        lines.append(f"Câu hỏi mới chờ   : {', '.join(p.name for p in new) or '-'}")
        for qid in sorted(rounds, key=qnum):
            r = rounds[qid]
            mk = parse_marker(read_text(r["a"])) if r.get("a") else {}
            rt = parse_rating(read_text(r["a"]))[0] if r.get("a") else "-"
            lines.append(f"  {qid}: {r['q'].name if r.get('q') else '?'} | {mk.get('status', 'chưa có trả lời')}"
                         f" | v{mk.get('v', '-')} | đánh giá {rt}")
        lines.append(f"Tổng hợp tuần gần nhất: {self.state.get('weekly_done', '-')}")
        return "\n".join(lines)


# ============================================================================ doctor / CLI
def doctor(cfg: dict) -> int:
    ok = True
    home = home_dir()
    print(f"config: {home / 'config.json'} {'(có)' if (home / 'config.json').exists() else '(CHƯA CÓ — dùng mặc định)'}")
    print(f"PD_Bridge: {cfg['bridge_dir']}")
    if "onedrive" not in cfg["bridge_dir"].lower():
        print("  ! PD_Bridge chưa nằm trong thư mục OneDrive — câu trả lời sẽ không được đồng bộ")
    dd = cfg.get("data_dir")
    print(f"Dữ liệu: {dd} {'OK' if dd and Path(dd).is_dir() else '— KHÔNG THẤY'}")
    ok &= bool(dd and Path(dd).is_dir())
    exe = find_claude(cfg)
    print(f"claude: {exe or 'KHÔNG THẤY'}")
    ok &= bool(exe)
    st = State(home / "state.json")
    if exe:
        r = ClaudeRunner(cfg, st)
        print(f"  phiên bản: {r.version()}")
        a = r.auth_status()
        print(f"  đăng nhập: {a if a else '(không kiểm tra được)'}")
        if a and a.get("loggedIn") is False:
            ok = False
        fl = r.flags()
        for need in ("--permission-mode", "--allowedTools", "--add-dir", "--output-format"):
            if need not in fl:
                print(f"  ! CLI thiếu {need} — hãy cập nhật: claude update")
                ok = False
    if os.environ.get("ANTHROPIC_API_KEY") and not cfg.get("allow_api_key"):
        print("  (có ANTHROPIC_API_KEY trong môi trường — bridge sẽ bỏ nó khi gọi claude để không tốn tiền API)")
    for mod in ("pypdf", "fitz"):
        try:
            __import__(mod)
            print(f"python: {mod} OK")
            break
        except ImportError:
            continue
    else:
        print("python: chưa có pypdf (PDF sẽ không được đánh chỉ mục) — pip install pypdf")
    b = Bridge(cfg, st, verbose=False)
    db = b.db_path()
    print(f"Chỉ mục: {db} {'OK' if db and db.exists() else '(chưa có — watcher sẽ tạo)'}")
    print("KẾT QUẢ:", "OK" if ok else "CẦN XỬ LÝ")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PD_Bridge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("watch")
    o = sub.add_parser("once")
    o.add_argument("--now", action="store_true")
    sub.add_parser("status")
    sub.add_parser("doctor")
    c = sub.add_parser("claim")
    c.add_argument("--now", action="store_true")
    c.add_argument("--json", action="store_true")
    f = sub.add_parser("finish")
    f.add_argument("qid")
    f.add_argument("--model", default="chat")
    w = sub.add_parser("weekly")
    w.add_argument("--force", action="store_true")
    for name in ("start", "stop"):
        x = sub.add_parser(name)
        x.add_argument("--cho", type=float, default=0, help="chờ N giây trước khi đóng cửa sổ")
    st_ = [x for x in sub.choices.values() if x.prog.endswith(" status")][0]
    st_.add_argument("--cho", type=float, default=0)
    a = ap.parse_args(argv)
    cfg = load_config()
    home = home_dir()
    # watcher chạy nền: chỉ ghi watcher.log (không nhân đôi ra stdout)
    setup_logging(home, verbose=a.cmd in ("once", "weekly") or (a.cmd == "watch" and sys.stdout.isatty()))
    state = State(home / "state.json")
    if a.cmd == "doctor":
        return doctor(cfg)
    if a.cmd in ("start", "stop"):
        rc = start_system(cfg, home) if a.cmd == "start" else stop_system(cfg, home)
        time.sleep(a.cho)
        return rc
    b = Bridge(cfg, state)
    if a.cmd == "watch":
        return b.watch()
    if a.cmd == "once":
        b.run_once(a.now)
        return 0
    if a.cmd == "status":
        print(b.status())
        time.sleep(a.cho)
        return 0
    if a.cmd == "weekly":
        print(b.weekly(force=a.force))
        return 0
    if a.cmd == "claim":
        new, _ = b.scan()
        for p in new:
            if a.now or b.ready(p):
                b.claim_new(p)
        _, rounds = b.scan()
        items = []
        for qid, kind in b.pending_jobs(rounds, ignore_debounce=True):
            if kind == "followup":
                continue
            info = b.process(qid, kind, chat=True)
            if info:
                items.append(info)
        if a.json:
            print(json.dumps(items, ensure_ascii=False, indent=1))
        elif not items:
            print("Không có câu hỏi nào đang chờ.")
        else:
            for it in items:
                print(f"===== {it['qid']} (mode {it['mode']}) — sau khi ghi {it['answer_tmp']} hãy chạy: "
                      f"python \"{TOOLS_DIR / 'bridge.py'}\" finish {it['qid']}\n")
                print(it["prompt"])
                print()
        return 0
    if a.cmd == "finish":
        qid = a.qid.upper()
        lk = b.runs / f"{qid}.chat.lock"
        if not lk.exists():
            print(f"Không có {qid} đang làm trong chat (chạy claim trước).")
            return 1
        info = json.loads(lk.read_text(encoding="utf-8"))
        tmp = Path(info["answer_tmp"])
        if not b.answer_ok(tmp):
            print(f"Chưa có câu trả lời đủ dài trong {tmp}")
            return 1
        b.finalize(info, read_text(tmp), a.model)
        lk.unlink()
        print(f"Đã ghi {qid}_traloi.md")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
