# -*- coding: utf-8 -*-
r"""메이플 경매장 → 환산주스탯(maplescouter) 아이템 자동 등록 유틸

사용법:
  venv\Scripts\python.exe main.py "<경매장 검색 URL>" -n 5 [--job 렌] [--prefix 도미네이터]
  venv\Scripts\python.exe main.py --test -n 3        # 캡처해둔 데이터로 테스트(검색 횟수 소모 없음)

동작:
  1) 저장된 넥슨 세션으로 경매장 링크 열기 → '필터 검색' 클릭 → 매물 목록 캡처
  2) 상위 N개 매물의 옵션(스타포스/추옵/작/잠재/에디셔널) 추출
  3) maplescouter 아이템 메이커에 하나씩 입력 → 즐겨찾기 추가
  4) 끝나면 브라우저를 열어둔 채 종료 → '아이템 적용하기'는 직접 클릭
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import sync_playwright

if getattr(sys, "frozen", False):  # PyInstaller 배포본
    BASE = Path(sys.executable).parent
else:
    BASE = Path(__file__).parent

# 로그인 정보는 프로그램 폴더가 아니라 사용자 데이터 폴더에 보관한다.
# (프로그램을 새 버전으로 덮어써도 로그인이 유지되고, 배포 압축에 섞이지도 않는다)
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MapleAuctionUtil"
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = BASE
PROFILE_DIR = str(DATA_DIR / "profile")
GRADE = {1: "레어", 2: "에픽", 3: "유니크", 4: "레전드리"}
SCOUTER_URL = "https://maplescouter.com/ko/item"

norm = lambda s: re.sub(r"[\s:+%]", "", s or "")
DEBUG = False

# [중지] 버튼용 — 대기 루프들이 주기적으로 확인해 즉시 빠져나온다
import threading
CANCEL = threading.Event()


def cancelled():
    return CANCEL.is_set()

COOKIE_FILE = DATA_DIR / "cookies.json"

# 예전 버전이 프로그램 폴더에 저장해 둔 로그인 정보가 있으면 새 위치로 옮긴다
try:
    _old = BASE / "cookies.json"
    if _old.exists() and not COOKIE_FILE.exists():
        COOKIE_FILE.write_text(_old.read_text(encoding="utf-8"), encoding="utf-8")
except Exception:
    pass


def save_cookies(ctx):
    """로그인 쿠키를 파일로 보관.
    넥슨 로그인 쿠키는 만료시각이 없는 '세션 쿠키'라 브라우저를 닫으면 사라진다.
    만료시각을 30일 뒤로 붙여 저장해 두면 다음 실행 때 다시 로그인할 필요가 없다."""
    try:
        cookies = ctx.cookies()
    except Exception:
        return False
    keep = []
    for c in cookies:
        c = dict(c)
        exp = c.get("expires", -1)
        if exp is None or exp == -1 or exp <= 0:
            c["expires"] = time.time() + 30 * 24 * 3600
        keep.append(c)
    try:
        COOKIE_FILE.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False


def load_cookies(ctx):
    if not COOKIE_FILE.exists():
        return False
    try:
        cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        fresh = [c for c in cookies if c.get("expires", 0) > time.time()]
        if fresh:
            ctx.add_cookies(fresh)
            return True
    except Exception as e:
        print(f"  ! 저장된 로그인 정보를 불러오지 못했습니다: {str(e)[:80]}")
    return False


def kill_stale_browsers():
    """이전 실행에서 남은 '유틸 전용 브라우저' 창을 정리한다.
    남아 있으면 사용자가 그 창에서 작업하는데 프로그램은 새 창을 보고 있어
    영영 진행되지 않는다. (평소 쓰는 크롬은 프로필 경로가 달라 영향 없음)"""
    import subprocess
    cmd = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
           f"Where-Object {{ $_.CommandLine -like '*{PROFILE_DIR}*' }} | "
           "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        time.sleep(1.5)
    except Exception:
        pass


def open_context(p):
    """작업용 전용 브라우저 컨텍스트.
    사용자의 평소 크롬과 완전히 분리된 프로필을 쓰므로, 이 안에서 한 로그인과
    즐겨찾기만 여기에 영구 저장된다 (평소 크롬의 쿠키·비밀번호에는 접근하지 않음)."""
    return p.chromium.launch_persistent_context(
        PROFILE_DIR, headless=False, locale="ko-KR",
        viewport={"width": 1500, "height": 950},
        # --mute-audio: 사이트 광고 동영상이 제멋대로 소리를 내는 것을 막는다
        args=["--disable-blink-features=AutomationControlled", "--mute-audio"],
    )


# ---------------- 경매장 ----------------

AUCTION_HOME = "https://auction.maplestory.nexon.com/buy"

# 검색어 입력칸(React controlled input)에 값 넣기
SET_KEYWORD_JS = """(kw) => {
  const i = [...document.querySelectorAll('input')].find(x => (x.placeholder||'').includes('아이템명'));
  if (!i) return false;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(i, kw);
  i.dispatchEvent(new Event('input', {bubbles: true}));
  return true;
}"""

GET_KEYWORD_JS = """() => {
  const i = [...document.querySelectorAll('input')].find(x => (x.placeholder||'').includes('아이템명'));
  return i ? i.value : null;
}"""


def safe_eval(pg, js, arg=None, tries=4, wait=1.5):
    """페이지 이동 중이면 컨텍스트가 사라지므로, 로딩을 기다렸다가 재시도한다."""
    for _ in range(tries):
        try:
            try:
                pg.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            return pg.evaluate(js, arg) if arg is not None else pg.evaluate(js)
        except Exception as e:
            msg = str(e)
            if ("Execution context was destroyed" in msg or "navigating" in msg.lower()
                    or "Target closed" in msg or "Target page" in msg):
                time.sleep(wait)
                continue
            raise
    return None


def page_kind(u):
    """현재 페이지가 로그인 중인지 / 캐릭터 선택인지 / 경매장 본화면인지"""
    u = (u or "").lower()
    if "auction.maplestory" not in u:
        return "login"           # 넥슨/네이버 로그인 등 외부 페이지
    if "character-select" in u:
        return "charselect"
    return "auction"


# 공지/이벤트 팝업 닫기 — '다시 보지 않기'류를 우선 클릭한다
DISMISS_POPUP_JS = """() => {
  const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>0 && r.height>0; };
  const prefer = ['다시 보지 않기', '다시보지 않기', '다시 보지않기', '다시보지않기',
                  '오늘 하루 보지 않기', '오늘하루 보지않기', '오늘 하루 열지 않기'];
  const fallback = ['닫기', 'Close', '확인'];
  const clickables = [...document.querySelectorAll('button, a, [role=button], label, span, div')]
      .filter(e => vis(e) && e.children.length <= 2);
  const hit = [];
  for (const list of [prefer, fallback]) {
    for (const el of clickables) {
      const t = nrm(el.textContent);
      if (!t || t.length > 20) continue;
      if (list.some(k => t.replace(/\\s/g,'') === k.replace(/\\s/g,''))) {
        try { el.click(); hit.push(t); } catch (e) {}
        break;
      }
    }
    if (hit.length) break;      // '다시 보지 않기'를 찾았으면 닫기까지 누르지 않는다
  }
  return hit;
}"""


def dismiss_popups(pg, rounds=3):
    """공지 팝업이 클릭을 가로막지 않도록 닫는다."""
    closed = []
    for _ in range(rounds):
        hit = safe_eval(pg, DISMISS_POPUP_JS) or []
        if not hit:
            break
        closed += hit
        time.sleep(1)
    if closed:
        print(f"  . 팝업 닫음: {closed}")
    return closed


# 경매장 화면에서 캐릭터 닉네임 찾기 (API 응답에는 이름이 없어 화면에서 읽는다)
FIND_NICK_JS = """() => {
  const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>0 && r.height>0; };
  const bad = /로그인|검색|판매|구매|시세|찜|완료|도움말|로그아웃|전투력|메이플|슬롯|미리보기|월드|캐릭터/;
  const leaves = [...document.querySelectorAll('body *')]
      .filter(e => e.children.length === 0 && vis(e))
      .map(e => ({el: e, t: nrm(e.textContent)}))
      .filter(o => o.t);
  const lvIdx = leaves.findIndex(o => /^Lv\\.?\\s*\\d+/.test(o.t));
  if (lvIdx < 0) return null;
  // 레벨 표시 바로 앞쪽에서 닉네임처럼 보이는 텍스트를 찾는다
  for (let i = lvIdx - 1; i >= 0 && i > lvIdx - 8; i--) {
    const t = leaves[i].t;
    if (t.length >= 2 && t.length <= 13 && !bad.test(t) && !/^(MVP|Lv)/i.test(t)
        && !/[|/]/.test(t) && !/^\\d+$/.test(t)) {
      return t;
    }
  }
  return null;
}"""


# 경매장 화면 상단의 캐릭터 이미지 주소
FIND_CHAR_IMG_JS = """() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>20 && r.height>20; };
  const img = [...document.querySelectorAll('img')].filter(vis)
      .find(i => /avatar\\.maplestory|Character\\/|character/i.test(i.src || ''));
  return img ? img.src : null;
}"""


# 화면의 'Lv.287 | 렌' 표기에서 직업을 읽는다 (API 응답이 없을 때의 대비)
FIND_JOB_JS = """() => {
  const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>0 && r.height>0; };
  const leaves = [...document.querySelectorAll('body *')]
      .filter(e => e.children.length === 0 && vis(e))
      .map(e => nrm(e.textContent)).filter(Boolean);
  const i = leaves.findIndex(t => /^Lv\\.?\\s*\\d+/.test(t));
  if (i < 0) return null;
  // 'Lv.287 | 렌 | 전투력 ...' 처럼 한 줄에 같이 있는 경우
  const m = leaves[i].match(/Lv\\.?\\s*\\d+\\s*[|]?\\s*([^|]+)/);
  if (m && m[1] && !/전투력/.test(m[1])) return m[1].trim();
  for (let k = i + 1; k < leaves.length && k < i + 4; k++) {
    const t = leaves[k];
    if (t && t.length <= 14 && !/전투력|\\d/.test(t) && t !== '|') return t;
  }
  return null;
}"""


def diagnose(pg, why=""):
    """왜 진행이 막혔는지 파악할 수 있게 현재 화면 상태를 남긴다."""
    try:
        print(f"  [진단] {why}")
        print(f"  [진단] URL: {pg.url[:140]}")
        print(f"  [진단] 제목: {pg.title()}")
        txt = pg.evaluate("() => document.body.innerText.replace(/\\s+/g,' ').slice(0,200)")
        print(f"  [진단] 화면: {txt}")
        btns = pg.evaluate("""() => [...document.querySelectorAll('button')]
            .map(b => (b.textContent||'').replace(/\\s+/g,' ').trim())
            .filter(t => t && t.length < 20).slice(0, 20)""")
        print(f"  [진단] 버튼: {btns}")
        shot = str(BASE / "search_fail.png")
        pg.screenshot(path=shot)
        print(f"  [진단] 스크린샷: {shot}")
    except Exception as e:
        print(f"  [진단] 실패: {str(e)[:120]}")


def auction_page(ctx):
    """현재 열려 있는 탭 중 경매장 본화면인 것"""
    try:
        pages = ctx.pages
    except Exception:
        return None
    for pg in pages:
        try:
            if page_kind(pg.url) == "auction":
                return pg
        except Exception:
            continue
    return None


HAS_SEARCH_UI_JS = """() => !![...document.querySelectorAll('input')]
    .find(i => (i.placeholder||'').includes('아이템명'))"""


def wait_ready(ctx, wait_minutes):
    """로그인·캐릭터 선택이 끝나 '검색 화면'이 뜰 때까지 대기. 준비된 page 반환.
    URL 만 보면 캐릭터 선택으로 넘어가기 직전의 순간을 입장 완료로 오인하므로,
    실제 검색창이 있는지까지 확인한다."""
    deadline = time.time() + wait_minutes * 60
    notified = set()
    on_auction_since = None
    last_report = time.time()
    while time.time() < deadline:
        if cancelled():
            print("중지했습니다.")
            return None
        try:
            pages = ctx.pages
        except Exception:
            return None
        if not pages:
            return None
        auction_pg = None
        kinds = []
        urls = []
        for pg in pages:
            try:
                kind = page_kind(pg.url)
            except Exception:
                continue
            kinds.append(kind)
            try:
                urls.append(pg.url[:90])
            except Exception:
                pass
            if kind == "auction" and auction_pg is None:
                auction_pg = pg
            if kind != "auction" and kind not in notified:
                notified.add(kind)
                print("→ 브라우저 창에서 넥슨 로그인을 진행해 주세요." if kind == "login"
                      else "→ 브라우저 창에서 경매장에 입장할 캐릭터를 선택해 주세요.")

        if auction_pg is not None:
            try:
                if safe_eval(auction_pg, HAS_SEARCH_UI_JS, tries=1):
                    return auction_pg
            except Exception:
                pass
            # 검색창을 못 찾아도 경매장 화면에 20초 이상 머물렀다면 진행한다
            if on_auction_since is None:
                on_auction_since = time.time()
            elif time.time() - on_auction_since > 20:
                return auction_pg
        else:
            on_auction_since = None

        if time.time() - last_report > 10:      # 멈춘 것처럼 보이지 않도록 상태 표시
            last_report = time.time()
            print(f"   (대기 중 | {', '.join(kinds) or '탭 없음'})")
            for pg in pages:
                try:
                    title = ""
                    try:
                        title = pg.title()
                    except Exception:
                        pass
                    txt = ""
                    try:
                        txt = pg.evaluate("""() => (document.body ? document.body.innerText : '')
                            .replace(/\\s+/g,' ').trim().slice(0, 200)""")
                    except Exception as e:
                        txt = f"(읽기 실패: {str(e)[:50]})"
                    print(f"      [{title[:30]}] {pg.url[:80]}")
                    print(f"      화면: {txt}")
                except Exception:
                    continue
        time.sleep(1)
    print("시간이 초과되었습니다. [중지] 후 다시 시도해 주세요.")
    return None


def login_nexon(wait_minutes=10):
    """최초 1회: 전용 브라우저에서 넥슨 로그인 + 캐릭터 선택까지 마치고 세션 저장"""
    with sync_playwright() as p:
        ctx = open_context(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(AUCTION_HOME, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"페이지 열기 실패: {str(e)[:120]}")
        try:
            pg.bring_to_front()
        except Exception:
            pass
        print("이 프로그램이 띄운 브라우저 창에서 넥슨 로그인 후 캐릭터를 선택해 주세요.")
        ready = wait_ready(ctx, wait_minutes)
        ok = ready is not None
        if ok:
            print("로그인 완료! 세션이 저장되었습니다. 이제 [실행]으로 검색하세요.")
            time.sleep(3)
        else:
            print("로그인이 확인되지 않았습니다.")
        try:
            ctx.close()
        except Exception:
            pass
        return ok


TOOLTIP_EP = "items/searches/tool-tip"


class SearchTracker:
    """검색 요청/응답을 함께 감시한다.
    요청 수를 세는 이유: 검색은 하루 100회 제한이라 이미 나간 검색이 있으면
    (우리가 눌렀든 사용자가 직접 눌렀든) 절대 버튼을 다시 누르면 안 된다."""

    def __init__(self):
        self.payloads = []
        self.requests = 0
        self.errors = []           # 검색 실패 응답(예: 결과가 너무 많음)
        self.character = None      # 경매장에 입장한 캐릭터 닉네임
        self.job = None            # 그 캐릭터의 직업
        self.char_image = None     # 캐릭터 이미지 주소
        self.last_payload = None   # 직전 검색 결과 (화면에 떠 있는 목록 재사용용)

    def attach(self, pg):
        try:
            pg.on("request", self._on_req)
            pg.on("response", self._on_resp)
        except Exception:
            pass

    def _on_req(self, req):
        try:
            # OPTIONS 는 브라우저가 보내는 사전확인 요청이라 실제 검색이 아니다
            if TOOLTIP_EP in req.url and req.method.upper() not in ("OPTIONS", "HEAD"):
                self.requests += 1
        except Exception:
            pass

    def _on_resp(self, resp):
        try:
            if DEBUG and "api.mskr.nexon.com" in resp.url:
                try:
                    m = resp.request.method
                except Exception:
                    m = "?"
                if m.upper() not in ("OPTIONS", "HEAD"):
                    path = resp.url.split("api.mskr.nexon.com", 1)[-1][:90]
                    print(f"   [API] {m} {resp.status} {path}")
        except Exception:
            pass
        try:
            if TOOLTIP_EP in resp.url:
                try:
                    method = resp.request.method.upper()
                except Exception:
                    method = ""
                if method in ("OPTIONS", "HEAD") or resp.status in (204, 304):
                    return                 # 사전확인 응답은 검색 결과가 아니다
                try:
                    body = resp.json()
                except Exception:
                    body = None
                if isinstance(body, dict) and isinstance(body.get("items"), list):
                    self.payloads.append(body)
                elif body is None and resp.status < 400:
                    return                 # 본문 없는 정상 응답은 무시
                else:                      # 검색이 거부된 경우(조건 부족 등)
                    msg = ""
                    if isinstance(body, dict):
                        msg = (body.get("message") or body.get("errorMessage")
                               or json.dumps(body, ensure_ascii=False)[:200])
                    self.errors.append({"status": resp.status, "msg": msg})
            elif "character-info" in resp.url:
                body = resp.json() or {}
                if isinstance(body.get("job"), str) and body["job"]:
                    self.job = body["job"]
                for k, v in body.items():
                    if "name" in k.lower() and isinstance(v, str) and v:
                        self.character = v
                        break
        except Exception:
            pass

    def reset(self):
        """검색 상태만 초기화 (캐릭터 정보와 직전 검색 결과는 유지)"""
        if self.payloads:
            self.last_payload = self.payloads[-1]
        self.payloads.clear()
        self.errors.clear()
        self.requests = 0


def capture_on_context(ctx, url, wait_minutes=10, tracker=None):
    """열려 있는 컨텍스트에서 로그인 → 검색 조건 적용 → 검색 실행 → 매물 목록 캡처.
    컨텍스트를 닫지 않으므로, 이어서 같은 브라우저의 새 탭으로 환산주스탯을 열 수 있다.

    로그인 리다이렉트를 거치면 URL의 검색 조건(keyword/정렬/필터)이 사라지므로,
    반드시 '로그인이 끝난 뒤에' 원래 링크로 다시 이동해서 조건을 복원한다.

    tracker 를 넘기면(Session 재사용) 리스너를 새로 붙이지 않는다."""
    external = tracker is not None
    if not external:
        tracker = SearchTracker()

    def attach(pg):
        if external:
            return          # Session 이 이미 리스너를 붙여 둠
        tracker.attach(pg)

    def browser_gone(e):
        m = str(e)
        return "has been closed" in m or "Target closed" in m or "Target page" in m

    keyword = ""
    if url:
        try:
            keyword = parse_qs(urlparse(url).query).get("keyword", [""])[0]
        except Exception:
            pass

    if True:
        try:
            ctx.on("page", attach)      # 새 탭(로그인 팝업 등)까지 커버
            for pg in ctx.pages:
                attach(pg)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            attach(page)
        except Exception as e:
            if browser_gone(e):
                print("브라우저가 닫혀 있습니다. 다시 [실행]하면 새로 엽니다.")
                return None
            raise

        # 1단계: 로그인 / 캐릭터 선택
        # 이미 검색 화면이 떠 있으면(연속 실행) 다시 불러오지 않는다 — 보던 화면이 날아가지 않도록
        ready = None
        existing = auction_page(ctx)
        if existing is not None and not url:
            try:
                if safe_eval(existing, HAS_SEARCH_UI_JS, tries=1):
                    ready = existing
                    print("경매장 검색 화면이 이미 열려 있습니다.")
            except Exception:
                pass
        if ready is None:
            try:
                page.goto(url or AUCTION_HOME, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                if browser_gone(e):
                    print("브라우저가 닫혔습니다.")
                    return None
                print(f"페이지 열기 실패: {str(e)[:120]}")
            print("경매장 준비 중...")
            ready = wait_ready(ctx, wait_minutes)
            if ready is None:
                print("경매장 화면에 도달하지 못했습니다.")
                return None
            print("경매장 입장 완료.")
        page = ready
        attach(page)
        try:                     # 중간에 중지하거나 창을 닫아도 로그인이 유지되도록 즉시 저장
            if save_cookies(ctx):
                print("로그인 정보를 저장했습니다.")
        except Exception:
            pass
        dismiss_popups(page)
        nick = safe_eval(page, FIND_NICK_JS)
        if nick and external:
            tracker.character = nick
        job_seen = tracker.job or safe_eval(page, FIND_JOB_JS)
        if job_seen and external:
            tracker.job = job_seen
        if external:
            print(f"캐릭터 확인: {tracker.character or '(못 찾음)'} / "
                  f"{tracker.job or '(직업 못 찾음)'}")
            img = safe_eval(page, FIND_CHAR_IMG_JS, tries=1)
            if img and img != tracker.char_image:
                tracker.char_image = img
                print(f"캐릭터이미지: {img}")

        # 2단계: 로그인 과정에서 사라진 검색 조건을 원래 링크로 복원
        if url:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                time.sleep(4)
            except Exception as e:
                print(f"검색 조건 적용 실패: {str(e)[:120]}")

        # 3단계: 검색 실행
        # 검색은 하루 100회 제한이므로 '검색 요청이 아직 한 번도 안 나간 경우'에만 버튼을 누른다.
        # 사이트가 자동 검색했거나 사용자가 직접 눌렀으면 결과만 기다린다.
        deadline = time.time() + wait_minutes * 60
        waiting_since = None
        announced = False
        heartbeat = time.time()
        clicks = 0
        while time.time() < deadline and not tracker.payloads:
            if cancelled():
                print("중지했습니다.")
                break
            try:
                if not ctx.pages:
                    print("브라우저가 닫혔습니다.")
                    break
            except Exception:
                print("브라우저가 닫혔습니다.")
                break

            if tracker.errors:
                e = tracker.errors[-1]
                msg = e.get("msg") or f"HTTP {e.get('status')}"
                print(f"! 경매장이 검색을 거부했습니다: {msg}")
                if "많" in msg or e.get("status") in (400, 429):
                    print("  → 검색 조건이 너무 넓습니다. 경매장에서 아이템명이나 필터를")
                    print("    더 좁혀서 검색한 뒤, 그 주소를 다시 넣어주세요.")
                break

            if tracker.requests > 0:
                if waiting_since is None:
                    print("검색 결과를 기다리는 중...")
                    waiting_since = time.time()
                if waiting_since and time.time() - waiting_since > 45:
                    print("검색 결과가 오지 않습니다. 창에서 직접 검색해 주세요.")
                    break
                time.sleep(1)
                continue

            if not keyword:
                # 링크(검색 조건) 없이 실행한 경우: 조건 없이 누르면 '결과가 너무 많습니다'
                # 오류만 나고 검색 횟수를 소모하므로, 사용자가 직접 검색할 때까지 기다린다.
                if not announced:
                    print("브라우저 창에서 직접 검색해 주세요. 결과는 자동으로 인식합니다.")
                    announced = True
                    heartbeat = time.time()
                elif time.time() - heartbeat > 20:
                    heartbeat = time.time()
                    pg = auction_page(ctx)
                    kw = safe_eval(pg, GET_KEYWORD_JS, tries=1) if pg else None
                    print(f"   (검색 대기 중 | 검색어 {kw!r} | 요청 {tracker.requests}건 "
                          f"| 결과 {len(tracker.payloads)}건)")
                    # 경매장이 화면에 띄운 안내(요청 자체가 안 나가는 경우)를 그대로 보여준다
                    warn = safe_eval(pg, """() => {
                      const t = document.body ? document.body.innerText : '';
                      const m = t.match(/[^\\n]*(너무 많습니다|범위를 좁혀|검색 조건)[^\\n]*/);
                      return m ? m[0].replace(/\\s+/g,' ').trim().slice(0, 90) : null;
                    }""", tries=1) if pg else None
                    if warn:
                        print(f"   ! 경매장 안내: {warn}")
                        print("     → 아이템명을 입력하거나 필터(부위·잠재 등급 등)를 지정한 뒤 검색해 주세요.")
                time.sleep(2)
                continue

            if clicks >= 3:
                print("자동 검색이 되지 않습니다. 브라우저 창에서 직접 검색해 주세요 (결과는 자동 인식).")
                time.sleep(3)
                continue

            pg = auction_page(ctx) or page
            if pg is None:
                print("경매장 탭을 찾지 못했습니다.")
                break
            page = pg
            # 검색 UI가 렌더링될 때까지 대기 (SPA·보안검사 등)
            try:
                page.wait_for_selector("input[placeholder*='아이템명']", timeout=30000, state="visible")
            except Exception:
                diagnose(page, "검색 UI를 찾지 못했습니다")
                time.sleep(2)
                continue
            try:
                # 링크의 검색어가 화면에 반영될 때까지 기다린다.
                # (조건 없이 검색하면 '결과가 너무 많습니다' 오류가 뜨고 검색 횟수만 소모)
                cur = safe_eval(page, GET_KEYWORD_JS)
                if keyword and not cur:
                    for _ in range(15):
                        time.sleep(1)
                        cur = safe_eval(page, GET_KEYWORD_JS)
                        if cur:
                            break
                    if not cur and safe_eval(page, SET_KEYWORD_JS, keyword):
                        time.sleep(1)
                        cur = safe_eval(page, GET_KEYWORD_JS)
                        if cur:
                            print(f"검색어 입력: {cur}")
                if keyword and not cur:
                    print("검색어가 화면에 적용되지 않았습니다. 창에서 직접 검색해 주세요.")
                    print("(조건 없이 검색하면 '결과가 너무 많습니다' 오류가 납니다)")
                    time.sleep(5)
                    continue
                if tracker.requests > 0:       # 준비하는 사이 검색이 나갔으면 누르지 않는다
                    continue
                clicks += 1
                if safe_eval(page, CLICK_BTN_JS, "필터 검색"):
                    print("검색 실행...")
                elif keyword:
                    page.locator("input[placeholder*='아이템명']").first.press("Enter", timeout=10000)
                    print("검색 실행(Enter)...")
                else:
                    print("검색 버튼을 찾지 못했습니다. 창에서 직접 검색해 주세요.")
            except Exception as e:
                print(f"검색 시도 실패: {str(e)[:100]}")
            # 클릭 후에는 결과를 충분히 기다린다 (중복 클릭 방지)
            for _ in range(20):
                time.sleep(1)
                if tracker.payloads or tracker.requests > 0:
                    break

        if tracker.payloads:
            time.sleep(3)  # 남은 응답 수집
    if not tracker.payloads:
        return None
    return tracker.payloads[-1]


def capture_auction(url, wait_minutes=10):
    """단독 실행용: 컨텍스트를 열어 캡처하고 닫는다."""
    with sync_playwright() as p:
        ctx = open_context(p)
        try:
            return capture_on_context(ctx, url, wait_minutes)
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def parse_items(payload, top_n):
    """tool-tip 응답에서 상위 N개 매물의 입력용 데이터 추출"""
    out = []
    for it in payload.get("items", [])[:top_n]:
        tt = it["toolTip"]
        ui = tt["upgradeInfo"]
        ex = tt["exOptionStat"] or {}
        up = tt["upgradeStat"] or {}
        pot = ui.get("potential") or {}
        add = ui.get("additionalPotential") or {}
        out.append({
            "name": it["itemName"],
            "part": (tt.get("categories") or ["", ""])[-1],
            "price": int(it["price"]),
            "starforce": it.get("starforce", 0),
            "ex": {"str": ex.get("str", 0), "dex": ex.get("dex", 0),
                   "int": ex.get("int", 0), "luk": ex.get("luk", 0),
                   "pad": ex.get("pad", 0), "mad": ex.get("mad", 0), "all": ex.get("all", 0)},
            "up": {"str": up.get("str", 0), "dex": up.get("dex", 0),
                   "int": up.get("int", 0), "luk": up.get("luk", 0),
                   "pad": up.get("pad", 0), "mad": up.get("mad", 0)},
            "pot_grade": pot.get("grade", 0),
            "pot_lines": [e["text"] for e in pot.get("entries", [])],
            "add_grade": add.get("grade", 0),
            "add_lines": [e["text"] for e in add.get("entries", [])],
            "raw_tooltip": tt,          # 보관함 직접 기록용 원본
        })
    return out


# ---------------- maplescouter ----------------

# NBSP 등 특수 공백이 섞인 텍스트도 잡도록 모든 매칭/클릭은 JS에서 공백 정규화 후 수행
CLICK_BTN_JS = """(t) => {
  const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
  const b = [...document.querySelectorAll('button')].find(x => nrm(x.textContent) === nrm(t));
  if (b) { b.click(); return true; }
  return false;
}"""

CLICK_TEXT_JS = """(t) => {
  const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>0 && r.height>0; };
  const els = [...document.querySelectorAll('body *')].filter(e => vis(e) && nrm(e.textContent) === nrm(t));
  if (!els.length) return false;
  els[els.length-1].click();
  return true;
}"""

MENU_TEXTS_JS = """() => {
  const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>0 && r.height>0; };
  return [...new Set([...document.querySelectorAll('body *')]
    .filter(e => vis(e))
    .map(e => nrm(e.textContent)).filter(t => t && t.length < 30))];
}"""

# '~' 플레이스홀더를 가진 스탯 입력칸들을 DOM 순서대로, 각자의 라벨과 함께 수집
STAT_INPUTS_JS = """() => {
  const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
  const inputs = [...document.querySelectorAll('input')].filter(i => (i.placeholder||'').includes('~'));
  return inputs.map(inp => {
    let label = '';
    let node = inp;
    for (let d = 0; d < 5 && !label; d++) {
      let prev = node.previousElementSibling;
      while (prev && !label) {
        const t = nrm(prev.textContent);
        if (t && t.length <= 12 && !t.includes('~')) label = t;
        prev = prev.previousElementSibling;
      }
      node = node.parentElement;
      if (!node) break;
    }
    return {ph: inp.placeholder, label};
  });
}"""


def menu_texts(pg):
    return pg.evaluate(MENU_TEXTS_JS)


def pick(pg, trigger, option, label, optional=False):
    """트리거 버튼 클릭 → 옵션 텍스트(퍼지 매칭) 클릭. 성공 여부 반환"""
    if not pg.evaluate(CLICK_BTN_JS, trigger):
        if not optional:
            print(f"  ! [{label}] 트리거 '{trigger}' 없음")
            if DEBUG:
                bt = pg.evaluate("() => [...document.querySelectorAll('button')]"
                                 ".map(b => (b.textContent||'').trim()).filter(Boolean)")
                print("    현재 버튼:", bt)
        return False
    time.sleep(0.8)
    cands = menu_texts(pg)
    target = next((c for c in cands if c == option), None)
    if target is None:
        target = next((c for c in cands if norm(c) == norm(option)), None)
    if target is None:
        target = next((c for c in cands if norm(option) and norm(option) in norm(c)), None)
    if target is None:
        if not optional:
            print(f"  ! [{label}] '{option}' 매칭 실패")
        pg.keyboard.press("Escape")
        time.sleep(0.3)
        return False
    if not pg.evaluate(CLICK_TEXT_JS, target):
        if not optional:
            print(f"  ! [{label}] '{target}' 클릭 실패")
        return False
    time.sleep(0.5)
    if DEBUG:
        print(f"  . [{label}] '{target}' 클릭 (트리거 '{trigger}')")
    return True


def current_job_trigger(pg):
    """직업 드롭다운 트리거 버튼의 현재 텍스트 찾기 (직업 정보 섹션의 첫 버튼)"""
    return pg.evaluate("""() => {
      const labels = [...document.querySelectorAll('*')].filter(e => e.textContent.trim() === '직업 선택' && e.children.length === 0);
      for (const lb of labels) {
        let cur = lb.parentElement;
        for (let i = 0; i < 4 && cur; i++, cur = cur.parentElement) {
          const b = cur.querySelector('button');
          if (b) return b.textContent.trim();
        }
      }
      return null;
    }""")


def fill_stats(pg, it):
    """라벨 기반으로 스타포스/추옵/작 수치를 채운다 (직업 무관).
    같은 라벨이 두 번 나오면 첫 번째=추옵, 두 번째=작."""
    ex, up = it["ex"], it["up"]
    wanted = {
        "스타포스": [it["starforce"]],
        "STR": [ex["str"], up["str"]],
        "DEX": [ex["dex"], up["dex"]],
        "INT": [ex["int"], up["int"]],
        "LUK": [ex["luk"], up["luk"]],
        "공격력": [ex["pad"], up["pad"]],
        "마력": [ex["mad"], up["mad"]],
        "올스탯": [ex["all"]],
    }
    stats = pg.evaluate(STAT_INPUTS_JS)
    seen = {}
    filled = []
    loc = pg.locator("input[placeholder*='~']")
    for idx, s in enumerate(stats):
        lab = s["label"]
        key = next((k for k in wanted if lab.startswith(k)), None)
        if key is None:
            continue
        n = seen.get(key, 0)
        seen[key] = n + 1
        if n >= len(wanted[key]):
            continue
        val = wanted[key][n]
        try:
            loc.nth(idx).fill(str(val))
            filled.append(f"{lab}{'(작)' if n == 1 else ''}={val}")
        except Exception as e:
            print(f"  ! 입력 실패 {lab}: {str(e)[:80]}")
    if DEBUG:
        print("  . 수치 입력:", ", ".join(filled))


# 보관함(즐겨찾기)에 실제로 담긴 개수 — localStorage 가 진실의 원천
BOOKMARK_COUNT_JS = """() => {
  try {
    const raw = localStorage.getItem('equipBookmarkList');
    if (!raw) return 0;
    const d = JSON.parse(raw);
    const list = (d.state && d.state.bookmarkList) || [];
    return list.length;
  } catch (e) { return 0; }
}"""

SCOUTER_STATE_JS = """() => {
  const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
  const btns = [...document.querySelectorAll('button')].map(b => nrm(b.textContent));
  const t = document.body ? document.body.innerText : '';
  return {ready: btns.includes('즐겨찾기 추가'), err: t.includes('에러가 발생했습니다')};
}"""


def scouter_ready(pg, seconds=25):
    """아이템 메이커 UI가 뜰 때까지 대기. (ready, err) 반환"""
    for _ in range(seconds):
        time.sleep(1)
        st = safe_eval(pg, SCOUTER_STATE_JS) or {}
        if st.get("ready"):
            return True, False
        if st.get("err"):
            return False, True
    return False, False


def open_scouter(pg, tries=3):
    """환산주스탯 아이템 메이커를 연다. 사이트가 에러 페이지를 띄우면 재시도.
    이미 아이템 메이커가 열려 있으면 다시 불러오지 않는다(보관함 상태 유지)."""
    st = safe_eval(pg, SCOUTER_STATE_JS) or {}
    if st.get("ready"):
        dismiss_popups(pg)
        return True
    for i in range(tries):
        try:
            pg.goto(SCOUTER_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  ! 페이지 열기 실패: {str(e)[:100]}")
        ready, err = scouter_ready(pg)
        if ready:
            dismiss_popups(pg)
            return True
        print(f"  ! 환산주스탯이 열리지 않음 (오류화면={err}) — 재시도 {i + 1}/{tries}")
        if err:
            try:                                  # 에러 화면의 '다시 시도'
                if safe_eval(pg, CLICK_BTN_JS, "다시 시도"):
                    ready, _ = scouter_ready(pg, 15)
                    if ready:
                        return True
            except Exception:
                pass
        time.sleep(2)
    diagnose(pg, "환산주스탯 아이템 메이커를 열지 못했습니다")
    print("  ! 사이트 데이터가 꼬였을 수 있습니다. 브라우저의 환산주스탯 탭에서")
    print("    [하드리셋]을 누르면 해결되는 경우가 많습니다 (저장된 즐겨찾기는 지워집니다).")
    return False


# ---------- 보관함 직접 기록 (클릭 없이 저장소에 바로 써넣기) ----------

# 경매장 스탯 키 → 환산주스탯 저장 키
STAT_MAP = {
    "str": "str", "dex": "dex", "int": "int", "luk": "luk",
    "mhp": "max_hp", "mmp": "max_mp",
    "pad": "attack_power", "mad": "magic_power",
    "speed": "speed", "jump": "jump",
    "dam": "damage", "bdr": "boss_damage", "imdr": "ignore_monster_armor",
    "all": "all_stat", "hpr": "max_hp_rate",
}

CLASS_GROUP = {
    "전사": ["히어로", "팔라딘", "다크나이트", "소울마스터", "미하일", "블래스터",
           "데몬슬레이어", "데몬어벤져", "아란", "카이저", "제로", "아델", "렌"],
    "마법사": ["아크메이지(불,독)", "아크메이지(썬,콜)", "비숍", "플레임위자드", "배틀메이지",
            "에반", "루미너스", "일리움", "라라", "키네시스", "시아 아스텔"],
    "궁수": ["보우마스터", "신궁", "패스파인더", "윈드브레이커", "와일드헌터", "메르세데스", "카인"],
    "도적": ["나이트로드", "섀도어", "듀얼블레이드", "나이트워커", "팬텀", "은월", "카데나",
           "칼리", "호영"],
    "해적": ["바이퍼", "캡틴", "캐논마스터", "스트라이커", "메카닉", "엔젤릭버스터", "아크", "제논"],
}


def class_group_of(job):
    for grp, jobs in CLASS_GROUP.items():
        if job in jobs:
            return grp
    return "전사"


def opt_block(src, req_level=0, req_dec=0):
    """경매장 스탯 dict → 환산주스탯 옵션 블록(문자열 값)"""
    out = {v: "0" for v in STAT_MAP.values()}
    out.update({"armor": "0", "max_mp_rate": "0"})
    for k, v in STAT_MAP.items():
        out[v] = str((src or {}).get(k, 0) or 0)
    out["base_equipment_level"] = req_level
    out["equipment_level_decrease"] = req_dec
    return out


def price_text(won):
    """가격을 '39억', '1.5억' 처럼 읽기 좋게"""
    uk = won / 1e8
    return f"{uk:.0f}억" if uk >= 10 or uk == int(uk) else f"{uk:.1f}억"


def make_labels(items, mode="number"):
    """스펙업 순서 등록에 쓸 이름을 만든다.
    mode='number': 같은 아이템이 여러 개면 뒤에 번호(도미네이터1,2,3),
                   하나뿐이면 이름만 (이글아이 원더러코트)
    mode='price' : 이름 뒤에 가격 (도미네이터 펜던트 39억)"""
    if mode == "price":
        return [f"{it['name']} {price_text(it['price'])}" for it in items]
    counts = {}
    for it in items:
        counts[it["name"]] = counts.get(it["name"], 0) + 1
    seen = {}
    labels = []
    for it in items:
        nm = it["name"]
        if counts[nm] > 1:
            seen[nm] = seen.get(nm, 0) + 1
            labels.append(f"{nm}{seen[nm]}")
        else:
            labels.append(nm)
    return labels


def pad3(lines):
    lines = [l for l in (lines or []) if l][:3]
    return lines + [""] * (3 - len(lines))


def build_bookmark(it, job, stamp):
    """경매장 매물 하나를 환산주스탯 보관함 항목으로 변환.
    name 은 반드시 실제 아이템명이어야 사이트가 장비를 인식한다(넘버링은 스펙업 등록칸에만 사용)."""
    tt = it["raw_tooltip"]
    req = tt.get("reqLevel", 0) or 0
    dec = (tt.get("stat") or {}).get("reduceReq", 0) or 0
    icon = ""
    try:
        fb = (tt.get("itemIcon") or {}).get("fallBackUrl") or ""
        code = fb.rsplit("/", 1)[-1].split(".")[0]
        icon = f"https://open.api.nexon.com/static/maplestory/item/icon/{code}"
    except Exception:
        pass
    ui = tt.get("upgradeInfo") or {}
    pot = ui.get("potential") or {}
    add = ui.get("additionalPotential") or {}
    scroll = ui.get("scroll") or {}
    return {
        "slot": it["part"], "part": it["part"], "name": it["name"],
        "iconUrl": icon,
        "starforce": str(it["starforce"]),
        "starforce_scroll_flag": "사용" if tt.get("isAmazingHyperUpgradeUsed") else "미사용",
        "scroll_upgrade": str(scroll.get("remaining", 0) or 0),
        "totalOption": opt_block(tt.get("stat"), req, dec),
        "baseOption": opt_block(tt.get("baseStat"), req, 0),
        "addOption": opt_block(tt.get("exOptionStat")),
        "etcOption": opt_block(tt.get("upgradeStat")),
        "starforceOption": opt_block(tt.get("starforceStat")),
        "potential_grade": GRADE.get(pot.get("grade", 0), ""),
        "potential_option_1": pad3([e.get("text") for e in pot.get("entries", [])]),
        "additional_potential_grade": GRADE.get(add.get("grade", 0), ""),
        "additional_potential_option_1": pad3([e.get("text") for e in add.get("entries", [])]),
        "exceptionalOption": {k: "0" for k in ("str", "dex", "int", "luk", "max_hp", "max_mp",
                                               "attack_power", "magic_power")} | {"exceptional_upgrade": 0},
        "hasExceptional": False,
        "soul_name": None, "soul_option": None, "ring_level": 0, "itemScore": "0",
        "character_name": f"ItemMaker{stamp}",
        "class_group": class_group_of(job),
        "cuttable_count": "255", "title": "", "bookMark": True, "isEquipped": False,
    }


APPEND_BOOKMARKS_JS = """(items) => {
  const KEY = 'equipBookmarkList';
  let d;
  try { d = JSON.parse(localStorage.getItem(KEY)) } catch (e) { d = null }
  if (!d || !d.state) d = {state: {bookmarkList: []}, version: 0};
  if (!Array.isArray(d.state.bookmarkList)) d.state.bookmarkList = [];
  d.state.bookmarkList.push(...items);
  localStorage.setItem(KEY, JSON.stringify(d));
  return d.state.bookmarkList.length;
}"""


def register_direct(pg, items, job, prefix, nickname=None, label_mode="number"):
    """드롭다운 클릭 없이 보관함 저장소에 직접 기록한다 (훨씬 빠름).
    성공하면 등록 개수, 실패하면 None 을 반환한다(호출부가 클릭 방식으로 대체)."""
    if not open_scouter(pg):
        return None
    before = safe_eval(pg, BOOKMARK_COUNT_JS) or 0
    stamp = time.strftime("%y%m%d_%H%M%S")
    entries = []
    for idx, it in enumerate(items, 1):
        if not it.get("raw_tooltip"):
            return None                     # 원본 데이터가 없으면 직접 기록 불가
        key = f"{stamp}{idx:03d}"
        it["_key"] = f"ItemMaker{key}"      # 나중에 이 아이템을 정확히 집어내기 위한 표식
        entries.append(build_bookmark(it, job, key))
    n = safe_eval(pg, APPEND_BOOKMARKS_JS, entries)
    if n is None:
        return None
    try:
        pg.reload(wait_until="domcontentloaded")     # 저장소 → 화면 반영
    except Exception:
        pass
    open_scouter(pg)                                  # 오류 화면이면 재시도까지 처리
    after = safe_eval(pg, BOOKMARK_COUNT_JS) or 0
    if after < before + len(items):
        print(f"  ! 직접 기록 검증 실패 (보관함 {before} → {after})")
        return None
    set_nickname(pg, nickname)
    for label, it in zip(make_labels(items, label_mode), items):
        print(f"  + {label}: {it['starforce']}성, "
              f"잠재 {GRADE.get(it['pot_grade'], '없음')}, {it['price'] / 1e8:.2f}억")
    print(f"\n완료: {len(items)}개 등록. (보관함 총 {after}개)")
    return len(items)


SET_NICK_JS = """(nick) => {
  const i = [...document.querySelectorAll('input')].find(x => (x.placeholder||'').includes('닉네임'));
  if (!i) return false;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(i, nick);
  i.dispatchEvent(new Event('input', {bubbles: true}));
  return true;
}"""


def set_nickname(pg, nick):
    """경매장에 입장한 캐릭터 닉네임을 환산주스탯에 입력해 내 캐릭터 기준으로 계산되게 한다."""
    if not nick:
        return False
    try:
        cur = safe_eval(pg, """() => {
          const i = [...document.querySelectorAll('input')].find(x => (x.placeholder||'').includes('닉네임'));
          return i ? i.value : null;
        }""")
        if cur == nick:
            return True
        if not safe_eval(pg, SET_NICK_JS, nick):
            return False
        pg.locator("input[placeholder*='닉네임']").first.press("Enter", timeout=10000)
        print(f"캐릭터 닉네임 적용: {nick}")
        time.sleep(4)
        return True
    except Exception as e:
        print(f"  ! 닉네임 입력 실패: {str(e)[:100]}")
        return False


def register_on_page(pg, items, job, prefix, nickname=None):
    """이미 열린 탭에서 환산주스탯 아이템 메이커에 매물을 등록한다."""
    if not open_scouter(pg):
        print("환산주스탯 페이지를 열지 못해 등록을 중단합니다.")
        return 0
    time.sleep(2)
    set_nickname(pg, nickname)

    if True:
        ok_count = 0
        for idx, it in enumerate(items, 1):
            label = f"{prefix}{idx}"
            price_uk = it["price"] / 1e8
            print(f"[{idx}/{len(items)}] {label}: {it['name']} {it['starforce']}성, "
                  f"잠재 {GRADE.get(it['pot_grade'],'없음')}, {price_uk:.2f}억")

            # 직업 (현재 트리거 텍스트를 찾아 클릭)
            cur = current_job_trigger(pg)
            if DEBUG:
                print(f"  . 현재 직업 트리거: {cur!r}")
            if cur and cur != job:
                pick(pg, cur, job, "직업")
            # 부위/이름
            if not pick(pg, "선택", it["part"], "부위"):
                # 이미 같은 부위가 선택돼 있으면 트리거 텍스트가 부위명
                pick(pg, it["part"], it["part"], "부위", optional=True)
            if not pick(pg, "선택", it["name"], "이름"):
                pick(pg, it["name"], it["name"], "이름", optional=True)
            time.sleep(1)

            # 옵션 프리셋 = 커스텀(기본). 스타포스/추옵/작 입력 (라벨 기반, 직업 무관)
            fill_stats(pg, it)

            # 잠재
            if it["pot_grade"]:
                pick(pg, "잠재 등급 선택", GRADE[it["pot_grade"]], "잠재등급")
                for ln in it["pot_lines"]:
                    pick(pg, "잠재 옵션 선택", ln, f"잠재:{ln}", optional=True)
            # 에디셔널
            if it["add_grade"]:
                pick(pg, "에디셔널 등급 선택", GRADE[it["add_grade"]], "에디등급")
                for ln in it["add_lines"]:
                    pick(pg, "에디셔널 옵션 선택", ln, f"에디:{ln}", optional=True)

            # 즐겨찾기 추가 (버튼이 실제로 눌렸는지, 보관함이 늘었는지로 확인)
            n_before = safe_eval(pg, BOOKMARK_COUNT_JS) or 0
            before = menu_texts(pg)
            clicked = safe_eval(pg, CLICK_BTN_JS, "즐겨찾기 추가")
            time.sleep(1.5)
            n_after = safe_eval(pg, BOOKMARK_COUNT_JS) or 0
            new = [t for t in menu_texts(pg) if t not in before]
            if not clicked:
                print(f"  ! {label} 실패: '즐겨찾기 추가' 버튼을 찾지 못했습니다")
                diagnose(pg, "아이템 메이커 화면이 아닙니다")
                break
            if any("필수" in t for t in new):
                print(f"  ! {label} 추가 실패(필수 정보 누락): {new}")
                pg.screenshot(path=str(BASE / f"fail_{idx}.png"), full_page=True)
            elif n_after <= n_before:
                print(f"  ! {label} 추가 안 됨 (보관함 {n_before} → {n_after})")
                pg.screenshot(path=str(BASE / f"fail_{idx}.png"), full_page=True)
            else:
                ok_count += 1
                print(f"  + {label} 즐겨찾기 추가 완료 (보관함 {n_after}개)")

            # 다음 아이템을 위해 입력 초기화
            try:
                pg.evaluate(CLICK_BTN_JS, "입력 초기화")
                time.sleep(0.8)
            except Exception:
                pass

        total = safe_eval(pg, BOOKMARK_COUNT_JS) or 0
        print(f"\n완료: {ok_count}/{len(items)}개 등록. (보관함 총 {total}개)")
        print("계속 검색해서 더 담을 수 있습니다. 다 담았으면 [아이템 적용하기]를 눌러 비교하세요.")
        return ok_count


# ---------- 보스컷: 추가 스펙 시뮬레이터 + 스펙업 순서 등록 ----------

# 라벨 주변의 스위치(shadcn Switch)를 찾아 원하는 상태로 만든다
SET_TOGGLE_JS = """([label, want]) => {
  const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
  const btns = [...document.querySelectorAll('button')].filter(b => /peer/.test(String(b.className)));
  for (const b of btns) {
    let n = b, ctx = '';
    for (let d = 0; d < 4 && n; d++, n = n.parentElement) {
      const t = nrm(n.textContent);
      if (t && t.length < 40) { ctx = t; break; }
    }
    if (ctx.includes(label)) {
      const on = b.getAttribute('data-state') === 'checked';
      if (on !== want) b.click();
      return {found: true, was: on};
    }
  }
  return {found: false};
}"""

# 아이템 선택 창의 '보관함' 영역에서 뒤에서 offset 번째 항목을 더블클릭 (장착/해제).
# 이름이 모두 같을 수 있으므로(같은 아이템 여러 매물) 위치로 지정한다.
DBLCLICK_ITEM_JS = """([offsetFromEnd, total]) => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>0 && r.height>0; };
  const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
  // '보관함' 제목을 가진 컨테이너를 찾는다
  let box = null;
  const labels = [...document.querySelectorAll('*')].filter(
      e => e.children.length === 0 && nrm(e.textContent) === '보관함' && vis(e));
  for (const lb of labels) {
    let n = lb.parentElement;
    for (let d = 0; d < 6 && n; d++, n = n.parentElement) {
      const imgs = n.querySelectorAll('img[src*="item/icon"]');
      if (imgs.length >= total) { box = n; break; }
    }
    if (box) break;
  }
  const imgs = [...(box || document).querySelectorAll('img[src*="item/icon"]')].filter(vis);
  if (imgs.length < offsetFromEnd) return {ok: false, seen: imgs.length};
  const img = imgs[imgs.length - offsetFromEnd];
  const tgt = img.parentElement || img;
  for (const type of ['mousedown','mouseup','click','mousedown','mouseup','click','dblclick']) {
    tgt.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window, detail: 2}));
  }
  return {ok: true, seen: imgs.length};
}"""

# 특정 섹션(예: '추가 스펙 시뮬레이터') 안에 있는 버튼만 클릭한다.
# '적용' 같은 이름은 화면에 여러 개 있어서(보스 시간 적용 등) 섹션을 한정해야 한다.
CLICK_IN_SECTION_JS = """([section, btnText]) => {
  const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>0 && r.height>0; };
  const heads = [...document.querySelectorAll('*')].filter(
      e => e.children.length === 0 && vis(e) && nrm(e.textContent) === section);
  for (const h of heads) {
    let n = h.parentElement;
    for (let d = 0; d < 8 && n; d++, n = n.parentElement) {
      const btn = [...n.querySelectorAll('button')].filter(vis)
          .find(b => nrm(b.textContent) === btnText);
      if (btn) { btn.click(); return true; }
    }
  }
  return false;
}"""

# 장착된 아이템(내 장비 영역)에서 같은 아이콘을 찾아 더블클릭 → 해제
DBLCLICK_EQUIPPED_JS = """(iconUrl) => {
  const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width>0 && r.height>0; };
  let box = null;
  const labels = [...document.querySelectorAll('*')].filter(
      e => e.children.length === 0 && vis(e) && nrm(e.textContent) === '내 장비');
  for (const lb of labels) {
    let n = lb.parentElement;
    for (let d = 0; d < 6 && n; d++, n = n.parentElement) {
      if (n.querySelectorAll('img[src*="item/icon"]').length > 3) { box = n; break; }
    }
    if (box) break;
  }
  if (!box) return false;
  const img = [...box.querySelectorAll('img[src*="item/icon"]')].filter(vis)
      .find(i => (i.src||'').includes(iconUrl));
  if (!img) return false;
  const tgt = img.parentElement || img;
  for (const type of ['mousedown','mouseup','click','mousedown','mouseup','click','dblclick']) {
    tgt.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window, detail: 2}));
  }
  return true;
}"""

# 보관함 목록에서 특정 아이템의 현재 위치를 계산한다.
# 장착/해제를 반복하면 순서가 바뀌므로, 매번 저장소를 다시 읽어 위치를 구해야 정확하다.
FIND_SLOT_JS = """(key) => {
  try {
    const d = JSON.parse(localStorage.getItem('equipBookmarkList'));
    const list = ((d.state || {}).bookmarkList) || [];
    const visible = list.filter(b => !b.isEquipped);   // 장착중인 건 보관함에 안 보인다
    const i = visible.findIndex(b => b.character_name === key);
    if (i < 0) return null;
    return {offsetFromEnd: visible.length - i, total: visible.length};
  } catch (e) { return null; }
}"""

# 적용 결과(보스300 최종뎀)를 읽어 아이템별로 값이 달라지는지 확인한다
READ_FINAL_DMG_JS = """() => {
  const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
  const t = document.body ? nrm(document.body.innerText) : '';
  const m = t.match(/최종뎀\\s*([0-9.]+)%/);
  return m ? m[1] : null;
}"""

FILL_SPECUP_JS = """([name, price]) => {
  const set = (el, v) => {
    const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    s.call(el, v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
  };
  const inputs = [...document.querySelectorAll('input')];
  const nameEl = inputs.find(i => (i.placeholder||'').includes('저장할 아이템 이름'));
  const priceEl = inputs.find(i => (i.placeholder||'') === '0');
  if (!nameEl || !priceEl) return false;
  nameEl.focus(); set(nameEl, name);
  priceEl.focus(); set(priceEl, String(price));
  return {name: nameEl.value, price: priceEl.value};
}"""


RESULT_URL = "https://maplescouter.com/ko/result?name={nick}&preset=00000"

# 스펙업 순서 등록 목록은 여기에 저장된다 — 실제로 등록됐는지 확인용
SPECUP_COUNT_JS = """() => {
  try {
    const d = JSON.parse(localStorage.getItem('bookMarkSimulList'));
    return ((d.state || {}).simulBookmarkList || []).length;
  } catch (e) { return 0; }
}"""

# 이미 등록된 이름 목록 — 사이트는 같은 이름을 다시 등록해주지 않는다
SPECUP_NAMES_JS = """() => {
  try {
    const d = JSON.parse(localStorage.getItem('bookMarkSimulList'));
    return ((d.state || {}).simulBookmarkList || []).map(x => String(x.name || ''));
  } catch (e) { return []; }
}"""


def open_result(pg, nickname=None, tries=3):
    """보스컷(결과) 페이지를 연다. 사이트가 에러를 띄우면 재시도."""
    for i in range(tries):
        dismiss_popups(pg)          # 공지 팝업이 토글 클릭을 막는 경우가 있다
        st = safe_eval(pg, SET_TOGGLE_JS, ["추가 스펙 시뮬레이터", True])
        if (st or {}).get("found"):
            return True
        try:
            pg.goto(RESULT_URL.format(nick=nickname or ""),
                    wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  ! 결과 페이지 열기 실패: {str(e)[:100]}")
        for k in range(25):
            time.sleep(1)
            s = safe_eval(pg, SCOUTER_STATE_JS) or {}
            if s.get("err"):
                break
            if k in (3, 8):
                dismiss_popups(pg)
            if (safe_eval(pg, SET_TOGGLE_JS, ["추가 스펙 시뮬레이터", True]) or {}).get("found"):
                return True
        print(f"  ! 보스컷 페이지가 열리지 않음 — 재시도 {i + 1}/{tries}")
        safe_eval(pg, CLICK_BTN_JS, "다시 시도")
        time.sleep(4)
    return False


def wait_item_window(pg, seconds=8):
    """아이템 창이 실제로 열릴 때까지만 기다린다 (고정 대기 대신)"""
    for _ in range(int(seconds * 4)):
        time.sleep(0.25)
        n = safe_eval(pg, """() => {
          const vis = el => { const r = el.getBoundingClientRect(); return r.width>0 && r.height>0; };
          return [...document.querySelectorAll('img[src*="item/icon"]')].filter(vis).length;
        }""", tries=1)
        if n and n > 3:
            return True
    return False


def wait_dmg(pg, prev, seconds=8):
    """[적용] 후 수치가 갱신될 때까지만 기다린다"""
    last = prev
    for _ in range(int(seconds * 4)):
        time.sleep(0.25)
        cur = safe_eval(pg, READ_FINAL_DMG_JS, tries=1)
        if cur and cur != prev:
            return cur
        last = cur or last
    return last


def register_specup(pg, items, prefix, nickname=None, label_mode="number"):
    """아이템마다 '장착 → 적용 → 스펙업 순서 등록' 사이클을 반복한다."""
    print("\n=== 스펙업 순서 등록 시작 ===")
    if not open_result(pg, nickname):
        diagnose(pg, "보스컷 페이지를 열지 못했습니다")
        return 0
    time.sleep(2)

    total = safe_eval(pg, BOOKMARK_COUNT_JS) or len(items)
    n_items = len(items)
    labels = make_labels(items, label_mode)
    existing = safe_eval(pg, SPECUP_NAMES_JS) or []
    ok = skipped = 0
    last_dmg = None
    for idx, it in enumerate(items, 1):
        label = labels[idx - 1]
        price_uk = round(it["price"] / 1e8, 2)
        # 방금 추가한 N개는 보관함의 마지막 N개 → 뒤에서 (N-idx+1)번째
        # 표식으로 현재 위치를 다시 계산 (순서가 바뀌어도 정확히 그 아이템을 집는다)
        offset = n_items - idx + 1
        slot = safe_eval(pg, FIND_SLOT_JS, it.get("_key")) if it.get("_key") else None
        if slot:
            offset, total = slot["offsetFromEnd"], slot["total"]
        if any(label in nm for nm in existing):
            skipped += 1
            print(f"[{idx}/{n_items}] {label} — 같은 이름이 이미 등록되어 있어 건너뜁니다.")
            continue
        print(f"[{idx}/{n_items}] {label} ({price_uk}억) 등록 중...")

        # 스펙업 순서 등록의 [아이템 창 열기] → 해당 아이템 더블클릭 → 닫기
        if not (safe_eval(pg, CLICK_IN_SECTION_JS, ["스펙업 순서 등록", "아이템 창 열기"])
                or safe_eval(pg, CLICK_BTN_JS, "아이템 창 열기")):
            print("  ! '아이템 창 열기' 버튼 없음")
            break
        wait_item_window(pg)
        picked = safe_eval(pg, DBLCLICK_ITEM_JS, [offset, total]) or {}
        if not picked.get("ok"):
            print(f"  ! 아이템 창에서 항목을 찾지 못했습니다 (보이는 아이콘 {picked.get('seen')}개)")
            pg.keyboard.press("Escape")
            time.sleep(0.5)
            continue
        time.sleep(0.6)
        pg.keyboard.press("Escape")
        time.sleep(0.6)

        # 아이템 적용 토글 ON → 추가 스펙 시뮬레이터의 [적용] 클릭 (이걸 눌러야 등록이 된다)
        prev_dmg = safe_eval(pg, READ_FINAL_DMG_JS, tries=1)
        safe_eval(pg, SET_TOGGLE_JS, ["아이템 적용하기", True])
        time.sleep(0.5)
        if not safe_eval(pg, CLICK_IN_SECTION_JS, ["추가 스펙 시뮬레이터", "적용"]):
            print("  ! 추가 스펙 시뮬레이터의 [적용] 버튼을 찾지 못했습니다")
        dmg = wait_dmg(pg, prev_dmg)
        # 0% 면 장착이 안 된 것 — 위치를 다시 계산해 한 번 더 시도한다
        if dmg in (None, "0.000", "0"):
            print(f"  . 최종뎀 {dmg}% — 장착이 안 된 것 같아 다시 시도합니다")
            slot = safe_eval(pg, FIND_SLOT_JS, it.get("_key")) if it.get("_key") else None
            if slot:
                if (safe_eval(pg, CLICK_IN_SECTION_JS, ["스펙업 순서 등록", "아이템 창 열기"])
                        or safe_eval(pg, CLICK_BTN_JS, "아이템 창 열기")):
                    wait_item_window(pg)
                    safe_eval(pg, DBLCLICK_ITEM_JS, [slot["offsetFromEnd"], slot["total"]])
                    time.sleep(0.6)
                    pg.keyboard.press("Escape")
                    time.sleep(0.6)
                    safe_eval(pg, SET_TOGGLE_JS, ["아이템 적용하기", True])
                    time.sleep(0.4)
                    safe_eval(pg, CLICK_IN_SECTION_JS, ["추가 스펙 시뮬레이터", "적용"])
                    dmg = wait_dmg(pg, dmg)
        print(f"  . 적용 후 최종뎀: {dmg}%")
        if dmg and dmg == last_dmg:
            print("    ! 앞 아이템과 값이 같습니다 — 장착이 안 바뀌었을 수 있습니다")
        last_dmg = dmg

        # 이름·가격 입력 후 등록
        filled = safe_eval(pg, FILL_SPECUP_JS, [label, price_uk])
        if not filled:
            print("  ! 스펙업 입력칸을 찾지 못했습니다")
            break
        time.sleep(0.4)
        before_n = safe_eval(pg, SPECUP_COUNT_JS) or 0
        if not safe_eval(pg, CLICK_BTN_JS, "등록하기"):
            print("  ! '등록하기' 버튼 없음")
            break
        # 실제로 목록에 들어갔는지 확인 (클릭만으로 성공 판정하지 않는다)
        after_n = before_n
        for _ in range(24):
            time.sleep(0.25)
            after_n = safe_eval(pg, SPECUP_COUNT_JS, tries=1) or 0
            if after_n > before_n:
                break
        if after_n > before_n:
            ok += 1
            existing = safe_eval(pg, SPECUP_NAMES_JS) or existing
            print(f"  + {label} 등록 완료 (목록 {after_n}개)")
        else:
            # 한 건 실패해도 나머지는 계속 시도한다
            print(f"  ! {label} 등록 실패 (입력값: {filled})")
            pg.screenshot(path=str(BASE / f"specup_fail_{idx}.png"))

        # 다음 아이템을 위해 장착 해제 (장착되면 '내 장비'로 옮겨가므로 아이콘으로 찾는다)
        icon = ""
        try:
            fb = ((it.get("raw_tooltip") or {}).get("itemIcon") or {}).get("fallBackUrl") or ""
            icon = fb.rsplit("/", 1)[-1].split(".")[0]
        except Exception:
            pass
        if icon and (safe_eval(pg, CLICK_IN_SECTION_JS, ["스펙업 순서 등록", "아이템 창 열기"])
                     or safe_eval(pg, CLICK_BTN_JS, "아이템 창 열기")):
            time.sleep(2)
            if not safe_eval(pg, DBLCLICK_EQUIPPED_JS, icon):
                safe_eval(pg, DBLCLICK_ITEM_JS, [offset, total])
            time.sleep(1)
            pg.keyboard.press("Escape")
            time.sleep(1)

    msg = f"스펙업 순서 등록: 신규 {ok}개"
    if skipped:
        msg += f", 이미 있던 것 {skipped}개"
    print(f"{msg} (전체 {len(items)}개 중)")
    if skipped and not ok:
        print("  같은 이름이 이미 있어 전부 건너뛰었습니다.")
        print("  → 새로 등록하려면 환산주스탯 [스펙업 순서]에서 기존 항목을 지우거나,")
        print("     프로그램의 [접미사]를 '가격'으로 바꿔서 다른 이름으로 등록하세요.")
    return ok


def keep_open(ctx):
    """브라우저를 닫을 때까지 프로그램 유지"""
    print("(즐겨찾기는 이 브라우저에만 저장됩니다 — 브라우저를 닫으면 프로그램이 종료됩니다)")
    try:
        while ctx.pages:
            time.sleep(2)
    except Exception:
        pass


def register_items(items, job, prefix):
    """단독 실행용: 새 브라우저를 열어 등록"""
    with sync_playwright() as p:
        ctx = open_context(p)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        register_on_page(pg, items, job, prefix)
        keep_open(ctx)


class Session:
    """전용 브라우저를 띄운 채 유지하면서 검색·등록을 여러 번 반복할 수 있는 세션.

    경매장 탭과 환산주스탯 탭을 각각 유지하므로, 다른 아이템을 다시 검색하면
    기존 보관함에 이어서 계속 담긴다. (Playwright sync API 특성상 이 객체는
    생성한 스레드에서만 사용해야 한다.)"""

    def __init__(self):
        kill_stale_browsers()           # 이전 실행의 유령 창 정리 (창이 두 개면 진행이 막힘)
        self._pw = sync_playwright().start()
        self.ctx = open_context(self._pw)
        if load_cookies(self.ctx):      # 지난번 로그인 상태 복원
            print("저장된 로그인 정보를 불러왔습니다.")
        else:
            print("저장된 로그인 정보가 없습니다 — 브라우저에서 로그인이 필요합니다.")
        try:
            pg = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
            pg.bring_to_front()
        except Exception:
            pass
        self.tracker = SearchTracker()
        self.scouter = None
        self._last_nav = ""
        self.ctx.on("page", self._attach)
        for pg in self.ctx.pages:
            self._attach(pg)

    def _attach(self, pg):
        self.tracker.attach(pg)
        try:                    # 로그인 과정 추적용 — 화면 이동을 그대로 기록
            pg.on("framenavigated", self._on_nav)
        except Exception:
            pass

    def _on_nav(self, frame):
        if not DEBUG:
            return                                  # 평소엔 이동 로그를 남기지 않는다
        try:
            if frame.parent_frame is not None:      # 최상위 프레임만
                return
            u = frame.url or ""
            if u.startswith("about:") or u == self._last_nav:
                return
            self._last_nav = u
            print(f"   → 이동: {u[:100]}")
        except Exception:
            pass

    def alive(self):
        try:
            return bool(self.ctx.pages)
        except Exception:
            return False

    def close(self):
        save_cookies(self.ctx)
        for fn in (lambda: self.ctx.close(), lambda: self._pw.stop()):
            try:
                fn()
            except Exception:
                pass

    def _auction_tab(self):
        pg = auction_page(self.ctx)
        if pg is None:
            try:
                pg = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
            except Exception:
                raise RuntimeError("브라우저가 닫혔습니다. 다시 [실행]하면 새로 엽니다.")
            self._attach(pg)
        return pg

    def login(self, wait_minutes=10):
        pg = self._auction_tab()
        try:
            if page_kind(pg.url) != "auction":
                pg.goto(AUCTION_HOME, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"페이지 열기 실패: {str(e)[:120]}")
        try:
            pg.bring_to_front()
        except Exception:
            pass
        print("이 프로그램이 띄운 브라우저 창에서 넥슨 로그인 후 캐릭터를 선택해 주세요.")
        ok = wait_ready(self.ctx, wait_minutes) is not None
        if ok:
            time.sleep(3)
            save_cookies(self.ctx)      # 다음 실행 때 재로그인하지 않도록 저장
            print("로그인 완료! 저장했습니다. 다음부터는 바로 [실행]하면 됩니다.")
        else:
            print("로그인이 확인되지 않았습니다.")
        return ok

    def login_scouter(self, wait_minutes=10):
        pg = self.scouter_tab()
        try:
            pg.goto(SCOUTER_URL, wait_until="domcontentloaded", timeout=60000)
            pg.bring_to_front()
        except Exception as e:
            print(f"페이지 열기 실패: {str(e)[:120]}")
        print("환산주스탯 탭에서 우측 상단 [로그인]으로 로그인해 주세요.")
        js = """() => {
          const nrm = s => (s||'').replace(/\\s+/g,' ').trim();
          return ![...document.querySelectorAll('button, a')].map(b => nrm(b.textContent)).includes('로그인');
        }"""
        for i in range(wait_minutes * 60):
            time.sleep(1)
            if not self.alive():
                return False
            try:
                if pg.evaluate(js):
                    print("환산주스탯 로그인 확인됨.")
                    return True
            except Exception:
                pass
            if i > 0 and i % 30 == 0:
                try:
                    pg.reload(wait_until="domcontentloaded")
                except Exception:
                    pass
        print("로그인이 확인되지 않았습니다. (선택 사항이라 없어도 사용 가능)")
        return False

    def scouter_tab(self):
        if self.scouter is not None:
            try:
                _ = self.scouter.url          # 닫혔는지 확인
            except Exception:
                self.scouter = None
        if self.scouter is None:
            try:
                self.scouter = self.ctx.new_page()
            except Exception:
                raise RuntimeError("브라우저가 닫혔습니다. 다시 [실행]하면 새로 엽니다.")
            self._attach(self.scouter)
        return self.scouter

    def open_site(self, which):
        """브라우저에서 해당 사이트 탭을 열어 앞으로 가져온다"""
        if which == "scouter":
            pg = self.scouter_tab()
            if not open_scouter(pg):
                print("환산주스탯을 여는 데 실패했습니다.")
                return False
            print("환산주스탯 창을 열었습니다.")
        else:
            pg = self._auction_tab()
            try:
                if page_kind(pg.url) != "auction":
                    pg.goto(AUCTION_HOME, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"경매장을 여는 데 실패했습니다: {str(e)[:100]}")
                return False
            print("메이플옥션 창을 열었습니다.")
        try:
            pg.bring_to_front()
        except Exception:
            pass
        return True

    def search(self, url, wait_minutes=10):
        """경매장에서 검색을 실행하고 매물 목록을 반환 (여러 번 호출 가능)"""
        self.tracker.reset()
        return capture_on_context(self.ctx, url, wait_minutes, self.tracker)

    def use_current(self, top_n, job, specup=False, auto_match=True,
                    label_mode="number", manual_nick=None):
        """새로 검색하지 않고, 브라우저에 이미 떠 있는 검색 결과를 그대로 등록한다.
        (검색 횟수를 소모하지 않는다)"""
        payload = self.tracker.payloads[-1] if self.tracker.payloads else self.tracker.last_payload
        if payload is None:
            print("아직 받아둔 검색 결과가 없습니다.")
            print("  → 경매장 창에서 한 번 검색하면 그 결과를 바로 쓸 수 있습니다.")
            return False
        return self._register(payload, top_n, job, specup, auto_match, label_mode, manual_nick)

    def run_once(self, url, top_n, job, prefix=None, wait_minutes=10, specup=False,
                 auto_match=True, label_mode="number", manual_nick=None):
        payload = self.search(url, wait_minutes)
        if payload is None:
            print("매물 목록을 가져오지 못했습니다.")
            return False
        return self._register(payload, top_n, job, specup, auto_match, label_mode,
                              manual_nick, prefix)

    def _register(self, payload, top_n, job, specup, auto_match, label_mode,
                  manual_nick, prefix=None):
        total_found = payload.get("total", len(payload.get("items", [])))
        print(f"경매장 목록 수신: 검색결과 {total_found}건 중 상위 {top_n}개를 사용합니다.")
        items = parse_items(payload, top_n)
        if not items:
            print("매물이 없습니다.")
            return False
        for i, it in enumerate(items, 1):
            print(f"   {i}. {it['name']} {it['starforce']}성 "
                  f"{GRADE.get(it['pot_grade'], '잠재없음')} {price_text(it['price'])}")
        pfx = prefix or items[0]["name"].split()[0]
        nickname = self.tracker.character if auto_match else manual_nick
        if auto_match and self.tracker.job:
            if self.tracker.job != job:
                print(f"직업 자동 적용: {self.tracker.job} (선택값 {job} 대신)")
            job = self.tracker.job
        elif auto_match:
            print(f"직업을 자동으로 확인하지 못해 선택값({job})을 사용합니다.")
        print(f"\n환산주스탯 탭으로 이동해 보관함에 등록합니다... (사이트 로딩에 10~30초 걸릴 수 있습니다)")
        pg = self.scouter_tab()
        try:
            pg.bring_to_front()
        except Exception:
            pass
        # 빠른 경로(저장소 직접 기록) → 실패하면 기존 클릭 방식으로 대체
        if register_direct(pg, items, job, pfx, nickname, label_mode) is None:
            print("직접 기록에 실패해 클릭 방식으로 진행합니다 (조금 느립니다).")
            register_on_page(pg, items, job, pfx, nickname=nickname)
        if specup:
            register_specup(pg, items, pfx, nickname, label_mode)
        save_cookies(self.ctx)          # 갱신된 세션 보존
        print("=== 이번 실행 완료 ===")
        return True


def run_flow(url, top_n, job, prefix=None, wait_minutes=10):
    """CLI 단발 실행: 검색·등록 후 브라우저를 닫을 때까지 대기"""
    s = Session()
    try:
        ok = s.run_once(url, top_n, job, prefix, wait_minutes)
        keep_open(s.ctx)
        return ok
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", help="경매장 검색 URL")
    ap.add_argument("-n", "--top", type=int, default=5, help="상위 몇 개 (기본 5)")
    ap.add_argument("--job", default="렌", help="직업 (기본 렌)")
    ap.add_argument("--prefix", default=None, help="아이템 이름 접두사 (기본: 아이템명)")
    ap.add_argument("--test", action="store_true", help="캡처해둔 데이터로 테스트")
    ap.add_argument("--debug", action="store_true", help="상세 로그")
    args = ap.parse_args()
    global DEBUG
    DEBUG = args.debug

    if args.test:
        data = json.load(open(BASE / "listing_captured.json", encoding="utf-8"))
        payload = [c["body"] for c in data if "tool-tip" in c["url"]][-1]
        items = parse_items(payload, args.top)
        if not items:
            print("매물이 없습니다."); sys.exit(1)
        prefix = args.prefix or (items[0]["name"].split()[0])
        print(f"{len(items)}개 매물 파싱 완료. maplescouter에 등록 시작...\n")
        register_items(items, args.job, prefix)
        return

    if not args.url:
        print("경매장 URL을 입력하세요."); sys.exit(1)
    if not run_flow(args.url, args.top, args.job, args.prefix):
        sys.exit(1)


if __name__ == "__main__":
    main()
