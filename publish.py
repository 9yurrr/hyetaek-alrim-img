"""혜택알림 자동 발행: 보조금24(공공서비스) + 기업마당(지원사업 공고) → HTML → Blogger.

사용법
  python publish.py auth            # 최초 1회: 구글 로그인 → refresh token을 .env에 저장
  python publish.py preview [N]     # 발행 없이 상위 N개를 preview/*.html로 저장
  python publish.py run [N]         # 아직 안 올린 서비스 N개 발행 (기본 3)
  python publish.py run ID ID ...   # 지정한 서비스ID만 발행
  python publish.py hubs            # 카테고리 모음 페이지(/p/...) 생성·갱신
  python publish.py related         # 기존 글에 '함께 보면 좋은 혜택' 링크 넣기
  python publish.py selftest        # 렌더/라벨 로직 점검

.env (이 폴더)
  DATA_GO_KR_API_KEY=...            # 공공데이터포털 '행정안전부_대한민국 공공서비스 정보' 활용신청된 키
  BLOGGER_CLIENT_ID=... / BLOGGER_CLIENT_SECRET=...   # Google Cloud OAuth 데스크톱 클라이언트
  BLOGGER_REFRESH_TOKEN=...         # auth 명령이 채움
"""
import html, json, os, re, sys, threading, time, urllib.parse, webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

HERE = Path(__file__).parent  # = 이미지 저장소 루트 (GitHub Actions에서도 동일)
ENV_PATH = HERE.parent / ".env"  # 로컬 키 파일은 저장소 밖(hyetaek-alrim/.env)에 둠. Actions에선 Secrets → 환경변수
CI = os.environ.get("GITHUB_ACTIONS") == "true"
STATE_PATH = HERE / "state.json"
BLOG_ID = "6460584705311207680"
GOV24 = "https://api.odcloud.kr/api/gov24/v3"


def _clean_secret(k, v):
    """Secrets에 붙여넣을 때 섞이기 쉬운 공백·따옴표·'NAME=' 접두어 제거."""
    v = v.strip().strip('"').strip("'").strip()
    return v[len(k) + 1:].strip() if v.startswith(k + "=") else v


def load_env():
    env = {k: _clean_secret(k, v) for k, v in os.environ.items() if k.startswith(("DATA_GO_KR_", "BLOGGER_"))}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"'))
    return env


def save_env_value(key, value):
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    lines = [l for l in lines if not l.startswith(key + "=")] + [f"{key}={value}"]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- 보조금24 ----------

def gov24_get(path, env, **params):
    params["serviceKey"] = env["DATA_GO_KR_API_KEY"]
    r = requests.get(f"{GOV24}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_candidates(env, pages=5, per_page=200):
    """조회수 높은 순으로 후보를 모음. 전국 대상(중앙행정기관)을 우선."""
    items = []
    for p in range(1, pages + 1):
        d = gov24_get("serviceList", env, page=p, perPage=per_page)
        items += d.get("data", [])
        if p * per_page >= d.get("totalCount", 0):
            break
    items = [x for x in items if not is_expired(x.get("신청기한"))]
    items.sort(key=lambda x: (x.get("소관기관유형") != "중앙행정기관", -int(x.get("조회수") or 0)))
    return items


def fetch_detail(env, service_id):
    if service_id.startswith("PBLN_"):
        return {}  # 기업마당은 목록에 내용이 다 있음
    d = gov24_get("serviceDetail", env, **{"cond[서비스ID::EQ]": service_id})
    rows = d.get("data", [])
    return rows[0] if rows else {}


# ---------- 기업마당 (중기부 지원사업 공고) ----------

def html_to_text(h):
    h = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", "\n", str(h or ""))
    return html.unescape(re.sub(r"<[^>]+>", "", h)).replace("\xa0", " ")


def bizinfo_to_svc(it):
    """기업마당 공고 → 보조금24와 같은 키로 맞춤 (render/make_card 재사용)."""
    return {
        "서비스ID": it["pblancId"],
        "서비스명": it.get("pblancNm", ""),
        "소관기관명": " · ".join(dict.fromkeys(x for x in (it.get("jrsdInsttNm"), it.get("excInsttNm")) if x)),
        "지원대상": it.get("trgetNm", ""),
        "지원내용": html_to_text(it.get("bsnsSumryCn")),
        "신청기한": it.get("reqstBeginEndDe", ""),
        "신청방법": it.get("reqstMthPapersCn", ""),
        "문의처": it.get("refrncNm", ""),
        "상세조회URL": it.get("pblancUrl", ""),
        "온라인신청사이트URL": it.get("rceptEngnHmpgUrl") or "",
        "수정일시": it.get("updtPnttm", ""),
        "사용자구분": it.get("hashtags", ""),
        "출처": "중소벤처기업부 기업마당",
    }


MONEY = r"지원금|보조금|보증|자금|융자|대출|이차보전|이자|바우처|수당|장려금|감면|환급|택배비|임대료|인건비|지원사업|무상|지원 계획"
NOT_MONEY = r"선발|선정계획|포상|유공|시상|지정계획|우수기업|설명회|박람회|전시회|세미나|행사|교육생|아카데미|기업인의 날|기반구축|실증|R&D|기술개발|IR|상담회"


def is_money_notice(x):
    """검색 수요가 있는 '받는 돈' 공고만. 포상·행사·지정 공고는 제외. ponytail: 키워드 휴리스틱, 오분류 보이면 규칙 추가"""
    t = x["서비스명"] + " " + x["지원내용"][:300]
    return bool(re.search(MONEY, t)) and not re.search(NOT_MONEY, x["서비스명"])


def fetch_bizinfo(env, rows=100):
    r = requests.get("https://apis.data.go.kr/1421000/bizinfo/pblancBsnsService", timeout=30, params={
        "serviceKey": env["DATA_GO_KR_API_KEY"], "pageNo": 1, "numOfRows": rows, "dataType": "json"})
    r.raise_for_status()
    items = r.json()["response"]["body"]["items"]["item"]
    items = [bizinfo_to_svc(it) for it in (items if isinstance(items, list) else [items])]
    # 마감 지난 것 제외, 돈이 되는 공고만, 마감일이 있는 공고(시의성) 우선
    items = [x for x in items if not is_expired(x["신청기한"]) and is_money_notice(x)]
    items.sort(key=lambda x: deadline_end(x["신청기한"]) is None)
    return items


def all_candidates(env):
    """보조금24(상시 혜택)와 기업마당(마감 있는 공고)을 번갈아."""
    a, b = fetch_candidates(env), fetch_bizinfo(env)
    out = []
    for i in range(max(len(a), len(b))):
        out += [x[i] for x in (a, b) if i < len(x)]
    return out


# ---------- 렌더링 ----------

LABEL_RULES = [
    ("청년", r"청년|대학생|사회초년"),
    ("신혼·출산", r"출산|임신|임산부|신혼|영유아|육아|보육|아동수당|난임"),
    ("어르신", r"노인|어르신|65세|고령|기초연금"),
    ("장애인", r"장애"),
    ("저소득", r"저소득|기초생활|차상위|수급자|긴급복지|근로장려금|자녀장려금|중위소득"),
    ("농어민", r"농업|농업인|어업|어업인|농어|임업|축산"),
    ("소상공인", r"소상공인|자영업|중소기업|창업"),
]


def labels_for(svc):
    # '지원 제외 대상' 이후는 오히려 대상이 아닌 사람들이라 분류에서 뺀다 (내일배움카드: 제외 대상의 '대학생·자영업자'로 오분류됐었음)
    target = re.split(r"제외", str(svc.get("지원대상") or ""))[0]
    text = " ".join([str(svc.get("서비스명") or ""), target, str(svc.get("서비스목적요약") or ""), str(svc.get("사용자구분") or "")])
    labels = [name for name, pat in LABEL_RULES if re.search(pat, text)]
    if is_closing_soon(svc.get("신청기한")):
        labels.append("마감임박")
    return labels or ["기타"]


DATE_RE = r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})"


def deadline_end(deadline):
    """신청기한 문자열의 마지막 날짜. 상시·날짜없음이면 None. ('20261031' 형식도 처리)"""
    s = str(deadline or "")
    found = re.findall(DATE_RE, s) or re.findall(r"(20\d{2})(\d{2})(\d{2})", s)
    for y, m, d in reversed(found):
        try:
            return date(int(y), int(m), int(d))
        except ValueError:
            continue
    return None


def is_closing_soon(deadline, today=None):
    today = today or date.today()
    end = deadline_end(deadline)
    return bool(end) and today <= end <= today + timedelta(days=7)


def is_expired(deadline, today=None):
    end = deadline_end(deadline)
    return bool(end) and end < (today or date.today())


def clean(v):
    v = str(v or "").strip()
    return "" if v in ("None", "null", "해당없음", "-") else v


BULLET = r"^[ㅇ○◦•·▶☞※□■\-]\s*"


def para(text):
    """공공데이터 원문(줄바꿈·'ㅇ/○' 기호)을 HTML로. 기호 줄은 목록, 들여쓴 이어지는 줄은 앞 항목에 붙임."""
    text = clean(text).replace("||", "\n")
    if not text:
        return ""
    out, items = [], []
    for raw in text.replace("\r", "").split("\n"):
        if not raw.strip():
            continue
        line = raw.strip()
        if re.match(BULLET, line):
            items.append(html.escape(re.sub(BULLET, "", line)))
        elif items and raw[:1] in (" ", "\t"):
            items[-1] += "<br/>" + html.escape(line)
        else:
            if items:
                out.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"); items = []
            out.append(f"<p>{html.escape(line)}</p>")
    if items:
        out.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
    return "".join(out)


def one_line(text, n=60):
    t = re.sub(BULLET, "", clean(text).replace("||", ", "))
    t = re.sub(r"\s+", " ", re.sub(r"\s*[○◦•▶※]\s*", " · ", t)).strip(" ·")
    return html.escape(t[:n] + ("…" if len(t) > n else "")) or "공고문 참고"


def display_name(name):
    """제목·카드용으로 공고명 다듬기: [지역]→지역, 연도·'공고/안내'·괄호 부제 제거."""
    n = re.sub(r"\[([^\]]+)\]\s*", lambda m: m.group(1) + " ", str(name or ""))
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"20\d\d년(도)?\s*|\s*(모집\s*)?(공고|안내)(\s*안내)?\s*$", " ", n)
    n = re.sub(r"\s*(공고|안내)\s*$", "", re.sub(r"\s+", " ", n).strip())
    return re.sub(r"\s+", " ", n).strip() or str(name)


def short_org(org):
    return re.split(r"\s*·\s*", str(org or ""))[0].strip()


def merged(svc, detail):
    s = {k: clean(v) for k, v in svc.items()}
    s.update({k: clean(v) for k, v in detail.items() if clean(v)})  # 상세가 더 길고 정확함
    return s


def render(svc, detail, image_url=None):
    s = merged(svc, detail)
    name = s.get("서비스명", "")
    org = s.get("소관기관명", "")
    title = f"{display_name(name)} 신청 방법·지원 대상 정리 ({short_org(org)})"
    src = s.get("상세조회URL") or f"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/{s.get('서비스ID','')}"
    online = s.get("온라인신청사이트URL")
    updated = re.sub(r"\D", "", s.get("수정일시", ""))[:8]
    updated_txt = f"{updated[:4]}.{updated[4:6]}.{updated[6:8]}" if len(updated) == 8 else date.today().strftime("%Y.%m.%d")

    rows = [
        ("누가", one_line(s.get("지원대상"))),
        ("무엇을", one_line(s.get("지원내용"))),
        ("언제까지", one_line(s.get("신청기한"), 40)),
        ("어디서", one_line(s.get("접수기관명") or s.get("접수기관") or s.get("신청방법"), 40)),
    ]
    sections = [
        ("지원 대상", s.get("지원대상")),
        ("선정 기준", "" if s.get("선정기준") == s.get("지원대상") else s.get("선정기준")),
        ("지원 내용", s.get("지원내용")),
        ("신청 방법", s.get("신청방법")),
        ("구비 서류", s.get("구비서류")),
        ("문의처", s.get("문의처") or s.get("전화문의")),
        ("근거 법령", s.get("법령")),
    ]
    # 목록 요약(본문 첫 문단). 목적 설명이 없는 공고는 '짧은 이름: 지원내용 한 줄'로
    summary = s.get("서비스목적") or s.get("서비스목적요약") or (
        f"{display_name(name)}: {html.unescape(one_line(s.get('지원내용'), 90))}" if s.get("지원내용") else display_name(name))
    body = [f'<p><img src="{image_url}" alt="{html.escape(name)} 지원 대상·내용·신청기한 요약" width="1200" height="675"/></p>'] if image_url else []
    body += [
        f"<p>{html.escape(re.sub(BULLET, '', summary))}</p>",
        "<h2>한눈에 보기</h2><table>"
        + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
        + f"<tr><th>담당 기관</th><td>{html.escape(org)}</td></tr></table>",
    ]
    for h, v in sections:
        if para(v):
            body.append(f"<h2>{h}</h2>{para(v)}")
    links = f'<a href="{html.escape(src)}" rel="nofollow noopener" target="_blank">정부24 원문 보기</a>'
    if online:
        links += f' · <a href="{html.escape(online)}" rel="nofollow noopener" target="_blank">온라인 신청 바로가기</a>'
    body.append(f"<h2>공식 원문</h2><p>{links}</p>")
    body.append(f"<p><small>출처: {html.escape(s.get('출처', '행정안전부 보조금24'))}(공공데이터포털) · 기준일 {updated_txt}</small></p>")
    return title, "\n".join(body), labels_for(s)


# ---------- 정보 카드 이미지 ----------

FONT_DIR = HERE / "fonts"


def _font(weight, size):
    from PIL import ImageFont
    return ImageFont.truetype(str(FONT_DIR / f"Pretendard-{weight}.otf"), size)


def _wrap(draw, text, font, width, max_lines):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) > width:
            lines.append(cur); cur = ch.lstrip()
            if len(lines) == max_lines:
                lines[-1] = lines[-1][:-1] + "…"; return lines
        else:
            cur += ch
    return lines + [cur] if cur else lines


def make_card(svc, path):
    """1200x675 흑백 정보 카드. 혜택명 + 누가/지원/신청기한 + 기관."""
    from PIL import Image, ImageDraw
    W, H, M = 1200, 675, 72
    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)
    ink, sub, mute, line = "#111111", "#444444", "#8a8a8a", "#e3e3e3"
    unescape = lambda s: html.unescape(s)

    d.text((M, 56), "혜택알림", font=_font("ExtraBold", 28), fill=ink)
    cat = " · ".join(labels_for(svc))
    d.text((W - M - d.textlength(cat, font=_font("Medium", 24)), 60), cat, font=_font("Medium", 24), fill=mute)
    d.line((M, 112, W - M, 112), fill=line, width=2)

    y = 150
    for l in _wrap(d, display_name(svc.get("서비스명", "")), _font("Bold", 60), W - 2 * M, 2):
        d.text((M, y), l, font=_font("Bold", 60), fill=ink); y += 78

    y = max(y + 28, 330)
    rows = [("누가", svc.get("지원대상")), ("지원", svc.get("지원내용")), ("신청기한", svc.get("신청기한"))]
    for k, v in rows:
        d.text((M, y), k, font=_font("SemiBold", 28), fill=mute)
        val = _wrap(d, unescape(one_line(v, 80)), _font("Medium", 30), W - 2 * M - 170, 1)
        d.text((M + 170, y - 2), val[0] if val else "공고문 참고", font=_font("Medium", 30), fill=sub)
        y += 62

    d.line((M, H - 96, W - M, H - 96), fill=line, width=2)
    d.text((M, H - 72), short_org(clean(svc.get("소관기관명"))), font=_font("SemiBold", 26), fill=ink)
    src = "출처: " + svc.get("출처", "행정안전부 보조금24")
    d.text((W - M - d.textlength(src, font=_font("Regular", 22)), H - 70), src, font=_font("Regular", 22), fill=mute)
    img.save(path, "PNG", optimize=True)
    return path


# ---------- Blogger ----------

SCOPE = "https://www.googleapis.com/auth/blogger"


def cmd_auth(env):
    """루프백 OAuth: 브라우저 로그인 → refresh token 저장."""
    port = 8765
    redirect = f"http://127.0.0.1:{port}/"
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            got.update(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write("인증 완료. 이 창을 닫아도 됩니다.".encode())
        def log_message(self, *a): pass

    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": env["BLOGGER_CLIENT_ID"], "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent"})
    print("브라우저에서 99yurrr@gmail.com 으로 로그인해 주세요:\n", url)
    webbrowser.open(url)
    for _ in range(300):
        if "code" in got: break
        time.sleep(1)
    srv.server_close()
    if "code" not in got:
        sys.exit("인증 코드를 받지 못했습니다.")
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "code": got["code"][0], "client_id": env["BLOGGER_CLIENT_ID"], "client_secret": env["BLOGGER_CLIENT_SECRET"],
        "redirect_uri": redirect, "grant_type": "authorization_code"}, timeout=30)
    r.raise_for_status()
    save_env_value("BLOGGER_REFRESH_TOKEN", r.json()["refresh_token"])
    print("refresh token 저장 완료 (.env)")


def access_token(env):
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": env["BLOGGER_CLIENT_ID"], "client_secret": env["BLOGGER_CLIENT_SECRET"],
        "refresh_token": env["BLOGGER_REFRESH_TOKEN"], "grant_type": "refresh_token"}, timeout=30)
    if not r.ok:  # 값은 찍지 않고 길이·구글 오류코드만 (Secrets 오입력 진단용)
        lens = {k: len(env.get(k, "")) for k in ("BLOGGER_CLIENT_ID", "BLOGGER_CLIENT_SECRET", "BLOGGER_REFRESH_TOKEN")}
        sys.exit(f"구글 토큰 발급 실패 {r.status_code} {r.json().get('error')}: {r.json().get('error_description')} / 길이 {lens}")
    return r.json()["access_token"]


_CHO = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
_JUNG = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i"]
_JONG = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l", "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t", "p", "t"]


def romanize(text):
    """국어의 로마자 표기(음운변화 생략한 단순판) — URL 슬러그용."""
    out = []
    for ch in text:
        c = ord(ch) - 0xAC00
        if 0 <= c < 11172:
            out.append(_CHO[c // 588] + _JUNG[(c % 588) // 28] + _JONG[c % 28])
        elif ch.isascii() and ch.isalnum():
            out.append(ch.lower())
        else:
            out.append(" ")
    return re.sub(r"\s+", "-", "".join(out).strip()).strip("-")


def slug_title(name, org=""):
    # Blogger는 발행 시점 제목으로 URL을 만들고 이후 제목을 바꿔도 URL은 유지됨
    name = re.sub(r"\[.*?\]|\(.*?\)|20\d\d년?|상반기|하반기|공고|안내", " ", name)  # 지역태그·연도·군말 제거
    for o in re.split(r"[\s·]+", org or ""):
        if len(o) >= 2:
            name = name.replace(o, " ")  # 기관명은 주소에서 뺌 (내용 단어가 잘려나가는 것 방지)
    s = romanize(name)
    if len(s) <= 40:
        return s
    cut = s[:40].rsplit("-", 1)[0]
    return cut if len(cut) >= 20 else s[:40].rstrip("-")


def blogger_insert(token, title, content, labels):
    r = requests.post(f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"title": title, "content": content, "labels": labels}, timeout=60)
    r.raise_for_status()
    return r.json()


def blogger_set_title(token, post_id, title):
    r = requests.patch(f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts/{post_id}",
                       headers={"Authorization": f"Bearer {token}"}, json={"title": title}, timeout=60)
    r.raise_for_status()
    return r.json()


# ---------- 명령 ----------

def load_state():
    return json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}


def cmd_preview(env, n):
    out = HERE / "preview"; out.mkdir(exist_ok=True)
    for svc in fetch_candidates(env, pages=1)[:n]:
        title, content, labels = render(svc, fetch_detail(env, svc["서비스ID"]))
        (out / f"{svc['서비스ID']}.html").write_text(
            f"<meta charset='utf-8'><h1>{html.escape(title)}</h1><p>라벨: {', '.join(labels)}</p>{content}", encoding="utf-8")
        print(svc["서비스ID"], labels, title)


IMG_REPO = HERE
IMG_CDN = "https://cdn.jsdelivr.net/gh/9yurrr/hyetaek-alrim-img@main/cards"


def push_cards(paths):
    import subprocess
    git = lambda *a: subprocess.run(["git", "-C", str(IMG_REPO), *a], check=True, capture_output=True, text=True)
    git("pull", "--rebase", "-q", "origin", "main")
    git("add", *[str(p.relative_to(IMG_REPO)) for p in paths])
    git("commit", "-m", f"Add {len(paths)} cards")
    git("push", "origin", "main")


def wait_cdn(url, tries=10):
    for _ in range(tries):
        if requests.head(url, timeout=15).status_code == 200:
            return
        time.sleep(3)
    raise RuntimeError(f"CDN에 이미지가 아직 없음: {url}")


def cmd_run(env, n, only=None):
    state = load_state()
    token = access_token(env)
    picked = []
    pool = [x for x in all_candidates(env) if x["서비스ID"] in only] if only else all_candidates(env)
    for svc in pool:
        if svc["서비스ID"] not in state:
            picked.append((svc, fetch_detail(env, svc["서비스ID"])))
            if len(picked) >= n:
                break
    # 이미지 먼저 한 번에 올리고 → 글 발행 (글에 깨진 이미지가 걸리지 않게)
    cards = [make_card(merged(s, d), IMG_REPO / "cards" / f"{s['서비스ID']}.png") for s, d in picked]
    if cards:
        push_cards(cards)
    for i, (svc, detail) in enumerate(picked):
        sid = svc["서비스ID"]
        img = f"{IMG_CDN}/{sid}.png"
        wait_cdn(img)
        title, content, labels = render(svc, detail, image_url=img)
        content = with_related(content, related_block(token, labels, hubs=load_hubs()))
        post = blogger_insert(token, slug_title(svc["서비스명"], svc.get("소관기관명")) or sid, content, labels)
        blogger_set_title(token, post["id"], title)
        state[sid] = {"postId": post["id"], "url": post["url"], "updated": svc.get("수정일시"), "at": datetime.now().isoformat(timespec="seconds")}
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        print("발행:", post["url"])
        if i < len(picked) - 1:
            time.sleep(20)  # ponytail: 고정 간격, Blogger 스팸 판정 보이면 하루 단위 분산으로
    if picked:
        update_hubs(token)


# ---------- 카테고리 허브 페이지 · 관련 글 (내부 링크) ----------
# /search/label/* 은 Blogger robots.txt가 막아 구글이 못 읽음 → 수집 가능한 /p/ 페이지로 카테고리 모음을 만든다

HUBS = [("청년", "cheongnyeon"), ("신혼·출산", "sinhon-chulsan"), ("소상공인", "sosanggongin"), ("어르신", "eoreusin"),
        ("저소득", "jeosodeuk"), ("장애인", "jangaein"), ("농어민", "nongeomin"), ("마감임박", "magam-imbak")]
HUBS_PATH = HERE / "hubs.json"
API = f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}"


def load_hubs():
    return json.loads(HUBS_PATH.read_text(encoding="utf-8")) if HUBS_PATH.exists() else {}


def posts_with_label(token, label, n=100):
    r = requests.get(f"{API}/posts", headers={"Authorization": f"Bearer {token}"}, timeout=30,
                     params={"labels": label, "maxResults": n, "fetchBodies": "false", "status": "live"})
    r.raise_for_status()
    return r.json().get("items", [])


def related_block(token, labels, hubs, exclude_url=None, n=4):
    label = next((l for l in labels if l in dict(HUBS)), None)
    if not label:  # 카테고리 없는 글('기타')은 최근 글로 이어줌
        r = requests.get(f"{API}/posts", headers={"Authorization": f"Bearer {token}"}, timeout=30,
                         params={"maxResults": n + 1, "fetchBodies": "false", "status": "live"})
        r.raise_for_status()
        items = [p for p in r.json().get("items", []) if p["url"] != exclude_url][:n]
        lis = "".join(f'<li><a href="{p["url"]}">{html.escape(p["title"])}</a></li>' for p in items)
        return f"<!--related--><h2>최근 올라온 혜택</h2><ul>{lis}</ul><!--/related-->" if lis else ""
    items = [p for p in posts_with_label(token, label, n + 1) if p["url"] != exclude_url][:n]
    lis = "".join(f'<li><a href="{p["url"]}">{html.escape(p["title"])}</a></li>' for p in items)
    hub = hubs.get(label, {}).get("url")
    more = f'<p><a href="{hub}">{html.escape(label)} 지원금·혜택 전체 보기 →</a></p>' if hub else ""
    return f"<!--related--><h2>함께 보면 좋은 {html.escape(label)} 혜택</h2><ul>{lis}</ul>{more}<!--/related-->" if lis or more else ""


def with_related(content, block):
    """'공식 원문' 앞에 관련 글 블록을 넣음 (이미 있으면 교체)."""
    content = re.sub(r"<!--related-->.*?<!--/related-->", "", content, flags=re.S)
    return content.replace("<h2>공식 원문</h2>", block + "<h2>공식 원문</h2>", 1) if block else content


def update_hubs(token):
    """카테고리별 모음 페이지를 만들거나 최신 목록으로 갱신."""
    H = {"Authorization": f"Bearer {token}"}
    hubs = load_hubs()
    for label, slug in HUBS:
        posts = posts_with_label(token, label)
        title = f"{label} 지원금·혜택 모음"
        lis = "".join(f'<li><a href="{p["url"]}">{html.escape(p["title"])}</a></li>' for p in posts)
        body = (f"<p>{html.escape(label)} 대상 정부·지자체 지원금과 혜택을 한곳에 모았습니다. "
                f"매일 새 지원금이 추가되며, 각 글에서 지원 대상·지원 내용·신청 기간·신청 방법을 확인할 수 있습니다.</p>"
                + (f"<ul>{lis}</ul>" if lis else "<p>아직 등록된 글이 없습니다. 곧 업데이트됩니다.</p>")
                + f"<p><small>기준일 {date.today():%Y.%m.%d} · 출처: 행정안전부 보조금24, 중소벤처기업부 기업마당</small></p>")
        if label in hubs:
            requests.patch(f"{API}/pages/{hubs[label]['id']}", headers=H, json={"title": title, "content": body}, timeout=60).raise_for_status()
        else:
            for wait in (0, 30, 60, 120):  # Blogger 페이지 생성은 속도 제한(429)이 빡빡함
                time.sleep(wait)
                r = requests.post(f"{API}/pages", headers=H, json={"title": slug, "content": body}, timeout=60)  # 영문 제목으로 /p/slug.html 확보
                if r.status_code != 429:
                    break
            r.raise_for_status()
            page = r.json()
            requests.patch(f"{API}/pages/{page['id']}", headers=H, json={"title": title}, timeout=60).raise_for_status()
            hubs[label] = {"id": page["id"], "url": page["url"]}
            HUBS_PATH.write_text(json.dumps(hubs, ensure_ascii=False, indent=1), encoding="utf-8")
        print("허브:", label, len(posts), "편", hubs[label]["url"])
        time.sleep(3)


def cmd_backfill_related(env):
    """기존 글에 관련 글 블록을 넣거나 갱신."""
    token = access_token(env)
    hubs = load_hubs()
    H = {"Authorization": f"Bearer {token}"}
    for p in requests.get(f"{API}/posts", headers=H, params={"maxResults": 100, "status": "live"}, timeout=30).json().get("items", []):
        new = with_related(p["content"], related_block(token, p.get("labels", []), hubs, exclude_url=p["url"]))
        if new != p["content"]:
            requests.patch(f"{API}/posts/{p['id']}", headers=H, json={"content": new}, timeout=60).raise_for_status()
            print("관련 글:", p["title"][:30])


def cmd_selftest():
    svc = {"서비스ID": "X1", "서비스명": "청년월세 한시 특별지원", "소관기관명": "국토교통부",
           "지원대상": "ㅇ 만 19~34세 무주택 청년\r\n   (소득) 중위 60% 이하\r\nㅇ 부모와 따로 거주", "지원내용": "월 최대 20만원, 최장 12개월",
           "신청기한": "2026.10.01 ~ 2026.10.05", "접수기관": "복지로", "수정일시": "20260930120000"}
    detail = {"선정기준": svc["지원대상"], "문의처": "콜센터/129||국토부/1599", "수정일시": "2026-09-30", "자치법규": "None"}
    title, content, labels = render(svc, detail)
    assert "청년월세" in title and "국토교통부" in title
    assert "<li>만 19~34세 무주택 청년<br/>(소득) 중위 60% 이하</li>" in content
    assert "<h2>선정 기준</h2>" not in content, "지원대상과 같으면 생략"
    assert "<p>콜센터/129</p><p>국토부/1599</p>" in content
    assert "None" not in content and "기준일 2026.09.30" in content
    assert labels_for(svc)[0] == "청년"
    assert labels_for({"서비스명": "국민내일배움카드", "지원대상": "○ 국민 누구나\n○ 지원 제외 대상\n ② 대학생\n ③ 자영업자"}) == ["기타"]
    assert is_closing_soon("2026.10.01 ~ 2026.10.05", today=date(2026, 10, 1))
    assert not is_closing_soon("상시신청", today=date(2026, 10, 1))
    assert is_expired("2026.05.04~2026.05.20", today=date(2026, 10, 1))
    assert not is_expired("상시신청", today=date(2026, 10, 1))
    assert "<script>" not in render({**svc, "지원내용": "<script>x</script>"}, {})[1]
    biz = lambda name, body="": {"서비스명": name, "지원내용": body}
    assert is_money_notice(biz("[경기] 안산시 소상공인 특례보증 추가 지원 계획 공고"))
    assert is_money_notice(biz("[강원] 화천군 농특산물 직거래 택배비 지원 공고"))
    assert not is_money_notice(biz("[부산] 2026년 MICE 우수기업 및 유공 선발 공고", "지원사업"))
    assert not is_money_notice(biz("[제주] 2026년 향토음식점 지정계획 공고"))
    assert deadline_end("20261001 ~ 20261031") == date(2026, 10, 31)
    assert display_name("[경남] 2026년 가족친화인증기업 문화활동비 지원사업 참여기업 모집 공고(일ㆍ생활균형지원사업)") == "경남 가족친화인증기업 문화활동비 지원사업 참여기업"
    assert display_name("[제주] 2026년 하반기 착한가격업소(탐나는 점빵) 모집 공고 안내") == "제주 하반기 착한가격업소"
    assert display_name("[강원] 화천군 2026년 농특산물 직거래 택배비 지원 공고") == "강원 화천군 농특산물 직거래 택배비 지원"
    assert display_name("국민내일배움카드") == "국민내일배움카드"
    assert short_org("경상남도 · 경남여성가족재단") == "경상남도"
    assert romanize("근로·자녀장려금") == "geunro-janyeojangryeogeum", romanize("근로·자녀장려금")
    assert slug_title("유아학비 (누리과정) 지원") == "yuahakbi-jiwon"
    assert len(slug_title("가" * 60)) <= 40
    assert slug_title("국토교통부 전세보증금반환보증 보증료 지원", "국토교통부").startswith("jeonsebojeunggeum")
    assert render(svc, detail, image_url="https://x/c.png")[1].startswith('<p><img src="https://x/c.png"')
    import tempfile
    from PIL import Image
    p = make_card(merged(svc, detail), Path(tempfile.gettempdir()) / "hx_card_test.png")
    assert Image.open(p).size == (1200, 675)
    biz_s = {"서비스ID": "PBLN_1", "서비스명": "[제주] 2026년 하반기 착한가격업소(탐나는 점빵) 모집 공고 안내", "소관기관명": "제주특별자치도 · 기초자치단체", "지원내용": "도민 및 관광객 대상 착한가격업소 지원"}
    c = render(biz_s, {})[1]
    assert "<p>제주 하반기 착한가격업소: 도민 및 관광객 대상 착한가격업소 지원</p>" in c, c[:200]
    blk = "<!--related--><h2>함께 보면 좋은 청년 혜택</h2><ul><li>x</li></ul><!--/related-->"
    once = with_related("<p>a</p><h2>공식 원문</h2>", blk)
    assert once.index("함께 보면") < once.index("공식 원문") and with_related(once, blk) == once
    print("selftest ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    args = sys.argv[2:]
    only = [a for a in args if len(a) > 4] or None  # 서비스ID 직접 지정: run 105100000001 135200005003 ...
    n = len(only) if only else (int(args[0]) if args else 3)
    if cmd == "run" and not CI:
        # 출력·에러를 logs/run.log에 누적 (스케줄러 실행은 콘솔이 붙어 있어 isatty로 구분 불가)
        (HERE / "logs").mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(HERE / "logs" / "run.log", "a", encoding="utf-8", buffering=1)
        print(f"\n=== {datetime.now():%Y-%m-%d %H:%M} run {' '.join(args)} ===")
    if cmd == "selftest":
        cmd_selftest()
    else:
        env = load_env()
        {"auth": lambda: cmd_auth(env), "preview": lambda: cmd_preview(env, n), "run": lambda: cmd_run(env, n, only),
         "hubs": lambda: update_hubs(access_token(env)), "related": lambda: cmd_backfill_related(env)}[cmd]()
