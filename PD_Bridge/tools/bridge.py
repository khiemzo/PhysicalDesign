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
    "effort": {"nhanh": "low", "chuan": "medium", "sau": "high", "tong_hop": "low"},
    "max_turns": {"nhanh": 30, "chuan": 80, "sau": 200, "tong_hop": 40},
    "timeout_min": {"nhanh": 20, "chuan": 45, "sau": 100, "tong_hop": 30},
    "default_mode": "chuan",
    "debounce_seconds": 2,       # file ngừng thay đổi 2 giây là nhận
    "poll_seconds": 1,           # quét thư mục câu hỏi mỗi giây (0 token)
    "keep_rounds": 5,
    "trash_after_minutes": 60,
    "index_refresh_hours": 24,
    "weekly": {"weekday": 4, "time": "16:55"},   # 0=Thứ Hai ... 4=Thứ Sáu
    "allow_api_key": False,      # False: xoá ANTHROPIC_API_KEY khỏi môi trường -> không tốn tiền API
    "max_attempts": 3,
    "chat_lock_hours": 3,
    "prevent_sleep": True,
    "pre_search_hits": 12,
    "attach_inline_chars": 14000,    # tổng ký tự file đính kèm đưa thẳng vào đề bài
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


def summary_section(answer: str, limit: int = 1200) -> str:
    m = re.search(r"^##\s*Tóm tắt\s*$(.*?)(?=^##\s|\Z)", answer, re.M | re.S | re.I)
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
            resume: str | None = None, max_turns: int | None = None) -> RunResult:
        res = RunResult()
        if not self.exe:
            res.kind, res.text = "login", "Không tìm thấy lệnh claude (chưa cài Claude Code)"
            return res
        cmd = self.build_cmd(model, job, add_dirs, deny_dirs, allowed, resume, max_turns)
        timeout = int((self.cfg.get("timeout_min") or {}).get(job, 45)) * 60
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
        try:
            out, err = proc.communicate(prompt.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            try:
                out, err = proc.communicate(timeout=30)
            except Exception:
                out, err = b"", b""
            res.kind, res.text = "timeout", f"Quá thời gian {timeout // 60} phút"
            (run_dir / f"stdout{tag}.txt").write_bytes(out or b"")
            return res
        out_s = (out or b"").decode("utf-8", errors="replace")
        err_s = (err or b"").decode("utf-8", errors="replace")
        (run_dir / f"stdout{tag}.json").write_text(out_s, encoding="utf-8")
        if err_s.strip():
            (run_dir / f"stderr{tag}.txt").write_text(err_s, encoding="utf-8")
        res.raw = out_s
        data = None
        for cand in (out_s.strip(), out_s.strip().splitlines()[-1] if out_s.strip() else ""):
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
        log.info("   claude %s model=%s -> %s (%d lượt, %.0fs)", job, model, res.kind, res.num_turns, now() - t0)
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
        self.gui = self.root / "du_lieu_gui"          # file đính kèm người hỏi gửi, lưu lâu dài
        if not (self.tools / "pd_index.py").exists():
            self.tools = TOOLS_DIR
        self.runs = self.home / "runs"
        self.data_dir = Path(cfg["data_dir"]) if cfg.get("data_dir") else None
        for d in (self.qdir, self.tong_hop, self.kien_thuc, self.runs):
            d.mkdir(parents=True, exist_ok=True)
        self.runner = ClaudeRunner(cfg, state)
        self.seen: dict[str, tuple] = {}          # tên file -> (size, mtime, last_change)
        self.worker: threading.Thread | None = None
        self.active: str | None = None
        self.index_proc: subprocess.Popen | None = None
        self.last_house = 0.0
        self.code_mtime = self._code_mtime()

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
        n_att = self.sweep_attachments(qid)
        mode, model = self.resolve_mode(stem, text)
        self.write_placeholder(qid, stem, mode, 1, "queued", status_msg)
        log.info("Câu hỏi mới: '%s' -> %s (mode %s, model %s%s)", p.name, target.name, mode, model,
                 f", {n_att} file đính kèm" if n_att else "")
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
            dest = self.gui / f"{qid}_{stem}"
            self.gui.mkdir(parents=True, exist_ok=True)
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
            dest = Path(meta.get("dir") or (self.gui / f"{qid}_{orig}"))
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
            if not qf or qid == self.active:
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
    def kien_thuc_snippets(self, question: str, limit_chars: int = 2200) -> str:
        terms = [t.lower() for t in pd_index.extract_terms(question, 20)]
        if not terms:
            return ""
        blocks = []
        for f in sorted(self.kien_thuc.glob("*.md")):
            if f.name.lower().startswith("boi_canh"):
                continue
            try:
                txt = read_text(f)
            except OSError:
                continue
            parts = re.split(r"(?m)^(?=#{2,4}\s|- (?:✅|⚠️|❌))", txt)
            for part in parts:
                low = part.lower()
                score = sum(1 for t in terms if t in low)
                if score:
                    blocks.append((score, f.name, part.strip()))
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
        "nhanh": ("NHANH — ưu tiên kien_thuc + chỉ mục trên máy (Innovus Text Command Reference…), "
                  "đọc tối thiểu; web chỉ để kiểm chứng 1–2 điểm (cú pháp/tuỳ chọn). Xong trong vài phút."),
        "chuan": ("CHUẨN — tìm kỹ trên máy (script/report/log liên quan + tài liệu tool), đọc đúng phần cần; "
                  "research web 2–4 nguồn uy tín; đối chiếu lý thuyết với dữ liệu thật trên máy."),
        "sau": ("SÂU — nghiên cứu nhiều vòng: (1) thu thập đủ dữ liệu trên máy (report/log/script các run liên "
                "quan, so sánh giữa các run), (2) tài liệu Cadence/Synopsys trên máy, (3) web ≥5 nguồn: app note, "
                "paper, tài liệu hãng, diễn đàn, (4) lập giả thuyết → kiểm chứng bằng số liệu → so sánh các "
                "phương án (bảng ưu/nhược/rủi ro), (5) tự phản biện trước khi kết luận."),
    }

    def build_prompt(self, qid: str, stem: str, mode: str, question: str, answer_tmp: Path,
                     edit: dict | None, raw_mode: str | None) -> str:
        att = self.attachment_context(qid)
        search_text = question + " " + parse_prefix(stem)[2]
        tools = self.tools
        py = Path(self.cfg.get("python") or sys.executable).stem.lower()   # python (Windows) / python3
        if py not in ("python", "python3", "py"):
            py = "python"
        kt = self.kien_thuc_snippets(question)
        bc = self.boi_canh()
        parts = [
            f"Câu hỏi {qid} ({stem}) từ PD_Bridge. Làm theo quy trình trong CLAUDE.md.",
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
        if att:
            parts += ["", "DỮ LIỆU NGƯỜI HỎI GỬI KÈM (ưu tiên phân tích; đã tóm tắt sẵn, mở file gốc khi cần chi tiết):", att]
        if bc:
            parts += ["", "BỐI CẢNH DỰ ÁN (kien_thuc/boi_canh_du_an.md):", bc]
        if kt:
            parts += ["", "KIẾN THỨC TÍCH LUỸ LIÊN QUAN (✅ = đã được xác nhận đúng; ❌ = đã bị đánh giá sai, tránh lặp):", kt]
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
            "bằng công cụ Write (file dài thì Write phần đầu rồi Edit/append phần sau; KHÔNG cắt ngắn nội dung).",
            "Bắt đầu bằng '## Tóm tắt'. Không in lại câu trả lời ra màn hình — cuối cùng chỉ in một dòng: XONG",
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
        self.write_placeholder(qid, stem, mode, v, "running",
                               f"⏳ Đang xử lý (mode {mode}, model {model_short(model)}) — bắt đầu {ts_str(fmt='%H:%M')}.")
        log.info("%s: bắt đầu (mode %s, model %s, v%d)", qid, mode, model, v)
        if self.cfg.get("runner") == "routine":
            res = self.run_routine(prompt, answer_tmp, mode)
        else:
            res = self.run_with_fallback(prompt, mode, model, run_dir, answer_tmp, job=mode,
                                         extra_dirs=[self.attachments_dir(qid)])
        return self.handle_result(info, res, model)

    def preflight(self) -> str | None:
        """Kiểm tra đăng nhập (0 token). -> None nếu ổn, ngược lại lý do."""
        if self.cfg.get("runner") == "routine":
            return None
        if not self.runner.exe:
            return "chua cai claude"
        st = self.runner.auth_status()
        if not st:
            return None
        if st.get("loggedIn") is False:
            return "chua dang nhap"
        meth = str(st.get("authMethod", "")).lower()
        if not self.cfg.get("allow_api_key") and ("api" in meth and "key" in meth or meth == "console"):
            return "dang dung API key (se ton tien API) — dang nhap lai bang tai khoan Claude: claude auth login"
        return None

    def run_with_fallback(self, prompt: str, mode: str, model: str, run_dir: Path, answer_tmp: Path,
                          job: str, extra_dirs: list | None = None) -> RunResult:
        cwd = self.root
        add_dirs = [d for d in (self.data_dir, run_dir, self.tools, *(extra_dirs or [])) if d and Path(d).exists()]
        deny = [self.data_dir] if self.data_dir else []
        allowed = self.allowed_tools(web=True)
        res = self.runner.run(prompt, model, job, cwd, add_dirs, deny, allowed, run_dir)
        fb = self.cfg["models"].get("fallback", "sonnet")
        if res.kind == "limit" and model_short(model) != model_short(fb):
            log.warning("   hết hạn mức %s -> chuyển %s", model_short(model), fb)
            res = self.runner.run(prompt, fb, job, cwd, add_dirs, deny, allowed, run_dir, tag="_fb")
            res.model = res.model or fb
        if res.kind == "max_turns" and not self.answer_ok(answer_tmp) and res.session_id:
            log.info("   hết số lượt -> yêu cầu viết câu trả lời với thông tin đã có")
            fin = ("Bạn đã dùng hết số lượt nghiên cứu. Hãy viết NGAY câu trả lời hoàn chỉnh nhất có thể "
                   "với thông tin đã thu thập (bắt đầu bằng '## Tóm tắt', ghi rõ phần nào chưa kịp kiểm chứng) "
                   f"vào file: {answer_tmp}\nCuối cùng in: XONG")
            res2 = self.runner.run(fin, res.model or model, job, cwd, add_dirs, deny, ["Write", "Edit", "Read"],
                                   run_dir, tag="_fin", resume=res.session_id, max_turns=8)
            if res2.ok or self.answer_ok(answer_tmp):
                res2.ok, res2.kind = True, "ok"
                res2.model = res2.model or res.model
                return res2
        return res

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
            rt["next"] = now() + (120 if res.kind == "transient" else 300) * n
            self.state.save()
        log.error("%s: thất bại (%s, lần %d): %s", qid, res.kind, n, res.text[:300])
        if n >= int(self.cfg.get("max_attempts", 3)):
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
        body = MARK_RX.sub("", body).strip()
        body = re.sub(r"\n?XONG\s*$", "", body).rstrip()
        # bỏ dòng đánh giá nếu Claude tự thêm (tránh trùng)
        body = RATING_RX.sub("", body)
        header = (f"# {qid} — {stem}\n\n"
                  f"> mode: **{mode}** · model: {model_short(model)} · {ts_str()} · {mins} phút"
                  f"{' · lần ' + str(v) if v > 1 else ''}{note}\n\n")
        footer = (f"\n\n---\n\n**Đánh giá** — sửa `chua` thành `dung`, `mot_phan` hoặc `sai`"
                  f" (tuỳ chọn thêm ghi chú sau `ghi_chu:`):\n\n"
                  f"danh_gia: chua\n\nghi_chu:\n\n"
                  f"<!-- pd_bridge q={qid} v={v} hash={info['hash']} status={status} mode={mode} "
                  f"model={model_short(model)} -->\n")
        atomic_write(self.qdir / f"{qid}_traloi.md", header + body + footer)
        self.state.sub("last_q")[qid] = {"text": info["question"][:6000], "hash": info["hash"], "v": v}
        self.state.save()
        rec = {"type": "answer", "q": qid, "v": v, "ts": ts_str(fmt="%Y-%m-%d %H:%M:%S"), "t": now(),
               "name": stem, "mode": mode, "model": model_short(model), "status": status, "minutes": mins,
               "question": info["question"][:1500], "summary": summary_section(body)}
        self.journal(rec)
        with open(self.tong_hop / "nhat_ky.md", "a", encoding="utf-8") as f:
            f.write(f"- {ts_str()} **{qid}** v{v} [{mode}/{model_short(model)}, {mins}′] {stem}"
                    f"{' — ❌ lỗi' if status == 'error' else ''}\n")
        log.info("%s: %s -> %s_traloi.md (%d phút)", qid, "XONG" if status == "done" else "LỖI", qid, mins)

    def journal(self, rec: dict) -> None:
        with open(self.tong_hop / "nhat_ky.jsonl", "a", encoding="utf-8") as f:
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
            if qid == self.active or self.chat_locked(qid):
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

    def housekeeping(self, force: bool = False) -> None:
        if not force and now() - self.last_house < 3600:
            return
        self.last_house = now()
        try:
            self.purge_trash()
            self.prune_runs()
            self.refresh_ratings()
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
            loc = self.qdir / f"{qid}_traloi.md"
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
        except Exception:  # noqa: BLE001
            log.exception("Lỗi khi xử lý %s", args)
        finally:
            self.active = None

    def start_worker(self, name: str, fn, *args) -> None:
        self.active = name
        self.worker = threading.Thread(target=self._work, args=(fn, *args), daemon=True)
        self.worker.start()

    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

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
        if self.busy():
            return
        if self.paused():
            return
        jobs = self.pending_jobs(rounds, ignore_debounce)
        if jobs:
            why = self.preflight()
            if why:
                self.state.set("paused_until", now() + 10 * 60)
                self.state.set("pause_reason", why)
                log.error("Loi: %s — thử lại sau 10 phút", why)
                return
            qid, kind = jobs[0]
            if block:
                self.active = qid
                self._work(self.process, qid, kind)
            else:
                self.start_worker(qid, self.process, qid, kind)
            return
        if self.weekly_due():
            if block:
                if getattr(self, "_weekly_tried", False):
                    return
                self._weekly_tried = True
                self._work(self.weekly)
            else:
                self.start_worker("weekly", self.weekly)

    def watch(self) -> int:
        lock = SingleInstance(self.home / "watcher.lock")
        if not lock.acquire():
            log.info("Watcher khác đang chạy — thoát")
            return 0
        if self.cfg.get("prevent_sleep", True):
            prevent_sleep()
        stop = self.home / "stop"
        stop.unlink(missing_ok=True)
        log.info("PD_Bridge watcher bắt đầu | câu hỏi: %s | dữ liệu: %s | claude: %s",
                 self.qdir, self.data_dir, self.runner.exe)
        self.housekeeping(force=True)
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("tick lỗi")
            if stop.exists() and not self.busy():
                stop.unlink(missing_ok=True)
                log.info("Nhận yêu cầu dừng — thoát")
                return 0
            if not self.busy() and self._code_mtime() != self.code_mtime:
                log.info("bridge.py đã cập nhật — khởi động lại")
                return 3
            if self.cfg.get("prevent_sleep", True):
                prevent_sleep()
            time.sleep(float(self.cfg.get("poll_seconds", 5)))

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
                break

    def status(self) -> str:
        new, rounds = self.scan()
        lines = [f"Thư mục PD_Bridge : {self.root}",
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
    if "onedrive" in cfg["bridge_dir"].lower() and "alchip" not in cfg["bridge_dir"].lower():
        print("  ! PD_Bridge chưa nằm trong OneDrive Alchip (chạy lại cai_dat.ps1 sau khi thêm tài khoản)")
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
    sub.add_parser("stop")
    a = ap.parse_args(argv)
    cfg = load_config()
    home = home_dir()
    # watcher chạy nền: chỉ ghi watcher.log (không nhân đôi ra stdout)
    setup_logging(home, verbose=a.cmd in ("once", "weekly") or (a.cmd == "watch" and sys.stdout.isatty()))
    state = State(home / "state.json")
    if a.cmd == "doctor":
        return doctor(cfg)
    if a.cmd == "stop":
        (home / "stop").write_text("stop", encoding="utf-8")
        print("Đã gửi yêu cầu dừng watcher.")
        return 0
    b = Bridge(cfg, state)
    if a.cmd == "watch":
        return b.watch()
    if a.cmd == "once":
        b.run_once(a.now)
        return 0
    if a.cmd == "status":
        print(b.status())
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
