# -*- coding: utf-8 -*-
"""MapleUtil — 메이플 경매장 매물을 환산주스탯에 자동 등록하는 유틸"""
import contextlib
import io
import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

# 패키징(exe)된 Playwright 가 번들 내부에서 브라우저를 찾지 않도록 표준 경로로 고정.
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"),
)

import tkinter as tk
from tkinter import font as tkfont
from tkinter import scrolledtext, ttk

import main as core
from widgets import ActivityBar, GradientHeader, RoundButton, Toggle

if "--selftest" in sys.argv:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        b.close()
    sys.exit(0)

APP_NAME = "MapleUtil"
APP_VERSION = "1.0"          # 새 버전을 낼 때 여기를 올리고 같은 번호로 릴리스 태그(v1.1)를 만든다
REPO = "Chen3301/maple-util"
RELEASE_PAGE = f"https://github.com/{REPO}/releases/latest"


def parse_ver(s):
    """'v1.2.3' -> (1, 2, 3) — 비교용"""
    nums = re.findall(r"\d+", s or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def fetch_latest_version(timeout=6):
    """GitHub 최신 릴리스 태그를 조회 (실패하면 None — 조용히 넘어간다)"""
    try:
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (_json.loads(r.read().decode("utf-8")) or {}).get("tag_name")
    except Exception:
        return None

# 팔레트
BG = "#0f1117"
CARD = "#171a23"
FIELD = "#212533"
FG = "#e6e8ef"
MUTED = "#8b90a3"
ACCENT = "#ff8a3d"
ACCENT_HOVER = "#ffa462"
GRAD1 = "#ff7a29"
GRAD2 = "#e0457b"
GREEN = "#4ade80"
RED = "#f87171"
LOG_BG = "#0c0e14"

_MUTEX = None


def claim_single_instance():
    """중복 실행 방지 (두 개가 뜨면 서로의 브라우저를 정리해 오류가 난다)"""
    global _MUTEX
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        _MUTEX = k32.CreateMutexW(None, False, "Global\\MapleUtilSingleton")
        return k32.GetLastError() != 183
    except Exception:
        return True


JOBS = [
    "렌", "히어로", "팔라딘", "다크나이트",
    "아크메이지(불,독)", "아크메이지(썬,콜)", "비숍",
    "보우마스터", "신궁", "패스파인더",
    "나이트로드", "섀도어", "듀얼블레이드",
    "바이퍼", "캡틴", "캐논마스터",
    "미하일", "소울마스터", "플레임위자드", "윈드브레이커", "나이트워커", "스트라이커",
    "아란", "에반", "메르세데스", "팬텀", "루미너스", "은월",
    "데몬슬레이어", "데몬어벤져", "블래스터", "배틀메이지", "와일드헌터", "메카닉", "제논",
    "카이저", "카인", "카데나", "엔젤릭버스터",
    "아델", "일리움", "아크", "칼리", "라라", "호영",
    "제로", "키네시스", "시아 아스텔",
]


def resource(name):
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / name
        if p.exists():
            return str(p)
    p = Path(__file__).parent / name
    return str(p) if p.exists() else None


def ensure_browser(log):
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    base = Path(base) if base else Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
    try:
        if base.exists() and any(d.name.startswith("chromium") for d in base.iterdir()):
            return True
    except Exception:
        pass
    log("첫 실행: 브라우저를 내려받습니다 (약 200MB, 몇 분 걸릴 수 있어요)...")
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        de = compute_driver_executable()
        cmd = list(de) if isinstance(de, (tuple, list)) else [str(de)]
        cmd += ["install", "chromium"]
        r = subprocess.run(cmd, env=get_driver_env(), capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            log("브라우저 설치 실패:\n" + (r.stderr or r.stdout or "")[-800:])
            return False
        log("브라우저 준비 완료.")
        return True
    except Exception as e:
        log(f"브라우저 설치 오류: {e}")
        return False


class QueueWriter(io.TextIOBase):
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s.strip():
            self.q.put(s.rstrip("\n"))
        return len(s)


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry("780x690")
        root.minsize(700, 600)
        root.configure(bg=BG)
        self.q = queue.Queue()
        self.cmd_q = queue.Queue()
        self.running = False

        ico = resource("app.ico") or resource("icon.ico")
        if ico:
            try:
                root.iconbitmap(ico)
            except Exception:
                pass
        self.avatar = None
        av = resource("avatar.png")
        if av:
            try:
                img = tk.PhotoImage(file=av)
                self.avatar = img.subsample(max(1, img.width() // 52))
            except Exception:
                self.avatar = None

        self._init_style()
        root.option_add("*TCombobox*Listbox.background", FIELD)
        root.option_add("*TCombobox*Listbox.foreground", FG)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        root.option_add("*TCombobox*Listbox.selectForeground", "#12141b")
        self._build(root)
        self._sync_auto()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._check_update, daemon=True).start()
        self.root.after(120, self.drain)

    def _check_update(self):
        """시작할 때 최신 릴리스를 조회해 새 버전이면 알림 띠를 띄운다"""
        tag = fetch_latest_version()
        if not tag or parse_ver(tag) <= parse_ver(APP_VERSION):
            return
        self.root.after(0, lambda: self._show_update(tag))

    def _show_update(self, tag):
        self.update_lbl.configure(
            text=f"새 버전 {tag} 이 나왔습니다  (현재 v{APP_VERSION})")
        self.update_bar.pack(fill="x", after=self.header)

    # ---------- 스타일 ----------

    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=FIELD)
        st.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG,
                     arrowcolor=FG, selectbackground=FIELD, selectforeground=FG,
                     borderwidth=0, padding=5)
        st.map("TCombobox",
               fieldbackground=[("readonly", FIELD), ("disabled", "#1a1d27")],
               foreground=[("readonly", FG), ("disabled", "#5f6478")],
               selectbackground=[("readonly", FIELD)],
               selectforeground=[("readonly", FG)],
               arrowcolor=[("disabled", "#5f6478")])
        st.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG,
                     borderwidth=0, padding=5)
        st.map("TEntry", fieldbackground=[("disabled", "#1a1d27")],
               foreground=[("disabled", "#5f6478")])
        st.configure("TSpinbox", fieldbackground=FIELD, foreground=FG, arrowcolor=FG,
                     borderwidth=0, padding=4)

    def _card(self, parent, title):
        outer = tk.Frame(parent, bg=BG)
        outer.pack(fill="x", pady=(0, 12))
        bar = tk.Frame(outer, bg=ACCENT, width=3)
        bar.pack(side="left", fill="y")
        body = tk.Frame(outer, bg=CARD, padx=16, pady=13)
        body.pack(side="left", fill="both", expand=True)
        tk.Label(body, text=title, bg=CARD, fg=MUTED,
                 font=("Malgun Gothic", 9, "bold")).pack(anchor="w", pady=(0, 10))
        return body

    # ---------- 화면 ----------

    def _build(self, root):
        self.header = GradientHeader(root, f"{APP_NAME}  v{APP_VERSION}",
                                     "경매장 매물 → 환산주스탯 자동 등록",
                                     GRAD1, GRAD2, avatar=self.avatar)
        self.header.pack(fill="x")

        # 새 버전 알림 (있을 때만 보인다)
        self.update_bar = tk.Frame(root, bg="#2b2419")
        self.update_lbl = tk.Label(self.update_bar, text="", bg="#2b2419", fg="#ffca7a",
                                   font=("Malgun Gothic", 9, "bold"))
        self.update_lbl.pack(side="left", padx=(18, 12), pady=9)
        tk.Label(self.update_bar, text="다운로드 페이지 열기", bg="#2b2419", fg=ACCENT,
                 cursor="hand2", font=("Malgun Gothic", 9, "underline")
                 ).pack(side="left", pady=9)
        for w in self.update_bar.winfo_children():
            w.bind("<Button-1>", lambda e: webbrowser.open(RELEASE_PAGE))
        self.update_bar.bind("<Button-1>", lambda e: webbrowser.open(RELEASE_PAGE))

        outer = tk.Frame(root, bg=BG, padx=18, pady=16)
        outer.pack(fill="both", expand=True)

        # 캐릭터
        c1 = self._card(outer, "캐릭터")
        self.auto_var = tk.BooleanVar(value=True)
        Toggle(c1, "경매장에 입장한 캐릭터를 자동으로 사용", self.auto_var,
               command=self._sync_auto, bg=CARD, on_color=GREEN,
               fg=FG, muted=MUTED).pack(anchor="w")
        r = tk.Frame(c1, bg=CARD)
        r.pack(fill="x", pady=(12, 0))
        tk.Label(r, text="닉네임", bg=CARD, fg=MUTED,
                 font=("Malgun Gothic", 9)).pack(side="left")
        self.nick_var = tk.StringVar()
        self.nick_entry = ttk.Entry(r, textvariable=self.nick_var, width=18)
        self.nick_entry.pack(side="left", padx=(10, 22))
        tk.Label(r, text="직업", bg=CARD, fg=MUTED,
                 font=("Malgun Gothic", 9)).pack(side="left")
        self.job_var = tk.StringVar(value="렌")
        self.job_combo = ttk.Combobox(r, textvariable=self.job_var, values=JOBS, width=18)
        self.job_combo.pack(side="left", padx=10)

        # 등록 설정
        c2 = self._card(outer, "등록 설정")
        r2 = tk.Frame(c2, bg=CARD)
        r2.pack(fill="x")
        tk.Label(r2, text="상위", bg=CARD, fg=MUTED,
                 font=("Malgun Gothic", 9)).pack(side="left")
        self.n_var = tk.IntVar(value=5)
        ttk.Spinbox(r2, from_=1, to=20, width=4,
                    textvariable=self.n_var).pack(side="left", padx=8)
        tk.Label(r2, text="개", bg=CARD, fg=MUTED,
                 font=("Malgun Gothic", 9)).pack(side="left")
        tk.Label(r2, text="이름 접미사", bg=CARD, fg=MUTED,
                 font=("Malgun Gothic", 9)).pack(side="left", padx=(26, 0))
        self.label_var = tk.StringVar(value="번호")
        ttk.Combobox(r2, textvariable=self.label_var, values=["번호", "가격"],
                     width=7, state="readonly").pack(side="left", padx=10)

        self.specup_var = tk.BooleanVar(value=True)
        Toggle(c2, "보스컷 [스펙업 순서 등록]까지 자동으로", self.specup_var,
               bg=CARD, on_color=GREEN, fg=FG, muted=MUTED).pack(anchor="w", pady=(14, 0))
        self.loop_var = tk.BooleanVar(value=True)
        Toggle(c2, "연속 모드 — 검색할 때마다 계속 등록", self.loop_var,
               bg=CARD, on_color=GREEN, fg=FG, muted=MUTED).pack(anchor="w", pady=(8, 0))

        # 버튼
        btns = tk.Frame(outer, bg=BG)
        btns.pack(fill="x", pady=(4, 10))
        self.run_btn = RoundButton(btns, "실행", self.run, ACCENT, ACCENT_HOVER,
                                   "#17110a", width=112, parent_bg=BG)
        self.run_btn.pack(side="left")
        self.stop_btn = RoundButton(btns, "중지", self.stop, "#333849", "#454b60",
                                    FG, width=112, parent_bg=BG)
        self.stop_btn.pack(side="left", padx=10)
        self.stop_btn.set_enabled(False)
        self.auc_btn = RoundButton(btns, "메이플옥션 창", lambda: self.open_site("auction"),
                                   "#333849", "#454b60", FG, width=128, parent_bg=BG)
        self.auc_btn.pack(side="left")
        self.sc_btn = RoundButton(btns, "환산주스탯 창", lambda: self.open_site("scouter"),
                                  "#333849", "#454b60", FG, width=128, parent_bg=BG)
        self.sc_btn.pack(side="left", padx=10)
        self.clear_btn = RoundButton(btns, "로그 지우기", self.clear_log,
                                     "#1c2029", "#2a2f3d", MUTED, width=112, parent_bg=BG)
        self.clear_btn.pack(side="right")

        self.activity = ActivityBar(outer, BG, ACCENT)
        self.activity.pack(fill="x", pady=(0, 6))

        stat = tk.Frame(outer, bg=BG)
        stat.pack(fill="x", pady=(0, 8))
        self.dot = tk.Canvas(stat, width=10, height=10, bg=BG, bd=0, highlightthickness=0)
        self.dot.pack(side="left", pady=(3, 0))
        self.dot_id = self.dot.create_oval(2, 2, 8, 8, fill=MUTED, outline="")
        self.status = tk.Label(stat, text="대기 중", bg=BG, fg=MUTED, anchor="w",
                               font=("Malgun Gothic", 9))
        self.status.pack(side="left", padx=8, fill="x", expand=True)

        mono = tkfont.Font(family="Consolas", size=9)
        self.log_box = scrolledtext.ScrolledText(
            outer, height=15, state="disabled", font=mono,
            bg=LOG_BG, fg="#c8ccd8", insertbackground=FG,
            relief="flat", borderwidth=0, padx=12, pady=10)
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_configure("ok", foreground=GREEN)
        self.log_box.tag_configure("warn", foreground="#ffb454")
        self.log_box.tag_configure("err", foreground=RED)
        self.log_box.tag_configure("head", foreground=ACCENT,
                                   font=("Consolas", 9, "bold"))
        self.log_box.tag_configure("dim", foreground="#6b7085")

    # ---------- 로그 ----------

    def log(self, msg):
        self.q.put(str(msg))

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _tag_for(self, line):
        s = line.strip()
        if s.startswith("+") or "완료" in s:
            return "ok"
        if s.startswith("!") or "실패" in s or "오류" in s:
            return "err"
        if s.startswith("==="):
            return "head"
        if s.startswith("---") or s.startswith("("):
            return "dim"
        if s.startswith("."):
            return "dim"
        return None

    def drain(self):
        try:
            while True:
                line = self.q.get_nowait()
                self._maybe_fill_character(line)
                if line.strip():
                    self.status.configure(text=line.strip()[:95])
                self.log_box.configure(state="normal")
                self.log_box.insert("end", line + "\n", self._tag_for(line) or "")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self.drain)

    def _maybe_fill_character(self, line):
        if not line.startswith("캐릭터 확인: "):
            return
        nick, _, job = line[len("캐릭터 확인: "):].partition("/")
        for var, widget, val in ((self.nick_var, self.nick_entry, nick.strip()),
                                 (self.job_var, self.job_combo, job.strip())):
            if not val or val.startswith("("):
                continue
            state = widget.cget("state")
            widget.configure(state="normal")
            var.set(val)
            widget.configure(state=state)

    def _sync_auto(self):
        state = "disabled" if self.auto_var.get() else "normal"
        self.nick_entry.configure(state=state)
        self.job_combo.configure(state=state)

    # ---------- 작업 ----------

    def _worker(self):
        writer = QueueWriter(self.q)
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            session = None
            while True:
                cmd = self.cmd_q.get()
                if cmd[0] == "quit":
                    break
                try:
                    if not ensure_browser(self.log):
                        continue
                    if session is not None and not session.alive():
                        session.close()
                        session = None
                    if session is None:
                        session = core.Session()
                    if cmd[0] == "open":
                        session.open_site(cmd[1])
                    elif cmd[0] == "run":
                        _, n, job, nick, specup, auto, loop, mode = cmd
                        while True:
                            ok = session.run_once("", n, job, specup=specup, auto_match=auto,
                                                  label_mode=mode, manual_nick=nick)
                            if not loop or core.CANCEL.is_set() or not ok:
                                break
                            self.log("--- 다음 검색 대기 중 (멈추려면 [중지]) ---")
                except Exception as e:
                    msg = str(e)
                    if any(k in msg for k in ("Connection closed", "Target closed",
                                              "has been closed", "Target page")):
                        self.log("브라우저 연결이 끊겼습니다. [실행]을 다시 누르면 새로 엽니다.")
                        try:
                            if session:
                                session.close()
                        except Exception:
                            pass
                        session = None
                    else:
                        self.log(f"오류: {msg[:300]}")
                finally:
                    self.running = False
                    self.root.after(0, self._idle_ui)

    def _idle_ui(self):
        self.run_btn.set_enabled(True)
        self.sc_btn.set_enabled(True)
        self.auc_btn.set_enabled(True)
        self.stop_btn.set_enabled(False)
        self.activity.stop()
        self.dot.itemconfigure(self.dot_id, fill=MUTED)

    def stop(self):
        core.CANCEL.set()
        self.log("중지 요청됨...")

    def _submit(self, cmd):
        if self.running:
            self.log("이미 실행 중입니다. [중지] 후 다시 시도하세요.")
            return
        core.CANCEL.clear()
        self.running = True
        self.run_btn.set_enabled(False)
        self.sc_btn.set_enabled(False)
        self.auc_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self.activity.start()
        self.dot.itemconfigure(self.dot_id, fill=GREEN)
        self.cmd_q.put(cmd)

    def run(self):
        self.log("경매장 창에서 아이템을 검색하세요. 결과가 나오면 자동으로 등록합니다.")
        self._submit(("run", self.n_var.get(), self.job_var.get().strip() or "렌",
                      self.nick_var.get().strip() or None, self.specup_var.get(),
                      self.auto_var.get(), self.loop_var.get(),
                      "price" if self.label_var.get() == "가격" else "number"))

    def open_site(self, which):
        self._submit(("open", which))


if __name__ == "__main__":
    if not claim_single_instance():
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo(APP_NAME, "MapleUtil 이 이미 실행 중입니다.\n기존 창을 사용해 주세요.")
        r.destroy()
        sys.exit(0)
    root = tk.Tk()
    App(root)
    root.mainloop()
