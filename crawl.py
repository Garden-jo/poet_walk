#!/usr/bin/env python3
"""
nanum.com/site/poet_walk 이미지 크롤러
GitHub Actions 에서 매일 실행 → images.json 업데이트
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ── 설정 ──────────────────────────────────────────────────────────────────────

LISTING_URL = 'https://www.nanum.com/site/poet_walk'
BOARD_PATH  = '/site/poet_walk'
MID         = 'poet_walk'
MAX_POSTS   = 50          # 크롤링할 최대 게시물 수
BATCH_SIZE  = 3           # 동시 fetch 수 (서버 부하 방지)
BATCH_DELAY = 0.5         # 배치 간 딜레이(초)

HEADERS = {
    'User-Agent':      'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Cache-Control':   'no-cache',
}

session = requests.Session()
session.headers.update(HEADERS)

# ── 1단계: 목록 페이지 → 게시물 URL 추출 ─────────────────────────────────────

def fetch_post_urls() -> list[str]:
    resp = session.get(LISTING_URL, headers={'Referer': LISTING_URL}, timeout=15)
    resp.raise_for_status()

    # ── 디버그 ──
    print(f'  HTTP {resp.status_code}, 최종 URL: {resp.url}')
    soup_debug = BeautifulSoup(resp.text, 'html.parser')
    title = soup_debug.find('title')
    print(f'  페이지 타이틀: {title.text.strip() if title else "(없음)"}')
    all_a = soup_debug.find_all('a', href=True)
    print(f'  전체 <a> 태그 수: {len(all_a)}')
    print(f'  처음 10개 href:')
    for a in all_a[:10]:
        print(f'    {a["href"]}')
    # ── 디버그 끝 ──

    soup        = BeautifulSoup(resp.text, 'html.parser')
    base        = urlparse(resp.url)
    pretty_re   = re.compile(r'^' + re.escape(BOARD_PATH) + r'/(\d+)')

    seen         = set()
    post_entries = []

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith('#') or href.startswith('javascript'):
            continue

        # 절대 URL 변환
        if href.startswith('http'):
            resolved = href
        elif href.startswith('//'):
            resolved = f'{base.scheme}:{href}'
        elif href.startswith('/'):
            resolved = f'{base.scheme}://{base.netloc}{href}'
        elif href.startswith('?'):
            resolved = f'{base.scheme}://{base.netloc}{base.path}{href}'
        else:
            continue

        parsed  = urlparse(resolved)
        post_id = None

        # Pattern A: pretty URL  /site/poet_walk/12345
        m = pretty_re.match(parsed.path)
        if m:
            post_id = int(m.group(1))

        # Pattern B: XE 쿼리  ?document_srl=12345
        if post_id is None:
            qs  = parse_qs(parsed.query)
            srl = qs.get('document_srl', [None])[0]
            qm  = qs.get('mid', [None])[0]
            if srl and (qm is None or qm == MID):
                try:
                    post_id = int(srl)
                except ValueError:
                    pass

        if post_id is None or post_id in seen:
            continue
        seen.add(post_id)

        # XE 쿼리 URL → pretty URL 정규화
        if 'document_srl' in parsed.query:
            resolved = f'{base.scheme}://{base.netloc}{BOARD_PATH}/{post_id}'

        post_entries.append((post_id, resolved))

    post_entries.sort(key=lambda x: x[0], reverse=True)   # 최신순
    return [url for _, url in post_entries[:MAX_POSTS]]


# ── 2단계: 게시물 페이지 → 이미지 URL 추출 ───────────────────────────────────

CONTENT_SELECTORS = [
    '.xe_content', '.read_body', '.view_content',
    '.board_view', 'article', '.post-content', '.content_area', '#content',
]

EXCLUDE_KEYWORDS = ['logo', 'icon', 'spacer', 'blank.', 'pixel.']


def fetch_image_url(post_url: str) -> str | None:
    parsed      = urlparse(post_url)
    segs        = parsed.path.strip('/').split('/')
    listing_ref = f'{parsed.scheme}://{parsed.netloc}/{"/".join(segs[:2])}'

    try:
        resp = session.get(post_url, headers={'Referer': listing_ref}, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f'  ✗ fetch 실패: {post_url} — {e}', file=sys.stderr)
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')

    # 우선순위 1: 본문 영역 선택자
    for sel in CONTENT_SELECTORS:
        container = soup.select_one(sel)
        if container:
            imgs = _extract_images(container, parsed)
            if imgs:
                return _pick_largest(imgs)

    all_imgs = _extract_images(soup, parsed)

    # 우선순위 2: 첨부 경로 포함 이미지
    attach = [i for i in all_imgs
              if any(p in i['src'] for p in ['/files/attach/', '/attach/images/', '/upload/'])]
    if attach:
        return _pick_largest(attach)

    # 우선순위 3: 100×100 이상인 이미지
    large = [i for i in all_imgs if i['w'] >= 100 and i['h'] >= 100]
    if large:
        return _pick_largest(large)

    return None


def _extract_images(container, base: 'ParseResult') -> list[dict]:  # type: ignore[name-defined]
    imgs = []
    for img in container.find_all('img'):
        src = (img.get('src') or img.get('data-src') or
               img.get('data-original') or img.get('data-lazy') or '').strip()
        if not src or src.startswith('data:'):
            continue

        # 절대 URL 변환
        if src.startswith('//'):
            src = f'{base.scheme}:{src}'
        elif src.startswith('/'):
            src = f'{base.scheme}://{base.netloc}{src}'
        elif not src.startswith('http'):
            src = urljoin(f'{base.scheme}://{base.netloc}{base.path}', src)

        # UI 요소 제외
        sl = src.lower()
        if any(k in sl for k in EXCLUDE_KEYWORDS):
            continue
        if sl.endswith('.gif') and 'ani' in sl:
            continue

        try:
            w = int(img.get('width') or 0)
            h = int(img.get('height') or 0)
        except (ValueError, TypeError):
            w, h = 0, 0

        imgs.append({'src': src, 'w': w, 'h': h})
    return imgs


def _pick_largest(imgs: list[dict]) -> str:
    best = imgs[0]
    for img in imgs[1:]:
        if img['w'] * img['h'] > best['w'] * best['h']:
            best = img
    return best['src']


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    print('▶ 게시물 목록 가져오는 중…')
    post_urls = fetch_post_urls()
    print(f'  {len(post_urls)}개 게시물 발견')

    url_order = {url: i for i, url in enumerate(post_urls)}
    results   = []

    for i in range(0, len(post_urls), BATCH_SIZE):
        batch = post_urls[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=BATCH_SIZE) as ex:
            futures = {ex.submit(fetch_image_url, url): url for url in batch}
            for future in as_completed(futures):
                post_url  = futures[future]
                image_url = future.result()
                if image_url:
                    results.append({'image_url': image_url, 'post_url': post_url})
                    print(f'  ✓ {post_url}')
                else:
                    print(f'  ✗ (이미지 없음) {post_url}', file=sys.stderr)
        time.sleep(BATCH_DELAY)

    # 최신순 정렬 유지
    results.sort(key=lambda x: url_order.get(x['post_url'], 9999))

    output = {
        'updated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'count':      len(results),
        'images':     results,
    }

    with open('images.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f'\n✅ images.json 저장 완료 ({len(results)}개 이미지)')


if __name__ == '__main__':
    main()
