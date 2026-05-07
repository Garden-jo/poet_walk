#!/usr/bin/env python3
"""
nanum.com/site/poet_walk 이미지 크롤러 (Playwright + 증분 업데이트)

- 첫 실행: 전체 페이지 순회 → 모든 게시물 수집
- 이후 실행: 기존 images.json에 없는 새 게시물만 수집 후 앞에 추가
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ── 설정 ──────────────────────────────────────────────────────────────────────

LISTING_URL = 'https://www.nanum.com/site/poet_walk'
BOARD_PATH  = '/site/poet_walk'
MID         = 'poet_walk'
PAGE_DELAY  = 0.3    # 페이지 간 딜레이(초)
OUTPUT_FILE = 'images.json'

EXCLUDE_KEYWORDS = ['logo', 'icon', 'spacer', 'blank.', 'pixel.']
CONTENT_SELECTORS = [
    '.xe_content', '.read_body', '.view_content',
    '.board_view', 'article', '.post-content', '.content_area', '#content',
]

# ── 기존 데이터 로드 ──────────────────────────────────────────────────────────

def load_existing() -> tuple[list[dict], set[str]]:
    """기존 images.json 로드. 반환: (기존 목록, 알려진 post_url 집합)"""
    if not os.path.exists(OUTPUT_FILE):
        return [], set()
    try:
        with open(OUTPUT_FILE, encoding='utf-8') as f:
            data = json.load(f)
        existing = data.get('images', [])
        known    = {item['post_url'] for item in existing}
        print(f'  기존 데이터: {len(existing)}개')
        return existing, known
    except Exception as e:
        print(f'  기존 파일 읽기 실패: {e}', file=sys.stderr)
        return [], set()

# ── 목록 한 페이지 → 게시물 URL 추출 ─────────────────────────────────────────

def fetch_page_post_urls(page, page_url: str) -> list[tuple[int, str]]:
    """목록 한 페이지에서 (post_id, post_url) 목록 반환"""
    page.goto(page_url, wait_until='networkidle', timeout=30000)
    html      = page.content()
    soup      = BeautifulSoup(html, 'html.parser')
    base      = urlparse(page_url)
    pretty_re = re.compile(r'^' + re.escape(BOARD_PATH) + r'/(\d+)')

    entries = []
    seen    = set()

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith('#') or href.startswith('javascript'):
            continue

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

        m = pretty_re.match(parsed.path)
        if m:
            post_id = int(m.group(1))

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

        if 'document_srl' in parsed.query:
            resolved = f'{base.scheme}://{base.netloc}{BOARD_PATH}/{post_id}'

        entries.append((post_id, resolved))

    entries.sort(key=lambda x: x[0], reverse=True)
    return entries

# ── 전체 목록 순회 (증분) ─────────────────────────────────────────────────────

def fetch_new_post_urls(page, known_urls: set[str]) -> list[str]:
    """known_urls에 없는 새 게시물 URL만 반환 (최신순)"""
    new_entries = []
    page_num    = 1

    while True:
        page_url = LISTING_URL if page_num == 1 else f'{LISTING_URL}?page={page_num}'
        print(f'  목록 페이지 {page_num} 크롤링…')

        entries = fetch_page_post_urls(page, page_url)

        if not entries:
            print(f'  → 게시물 없음, 순회 종료')
            break

        hit_known = False
        for post_id, post_url in entries:
            if post_url in known_urls:
                hit_known = True
                break
            new_entries.append((post_id, post_url))

        if hit_known:
            print(f'  → 기존 게시물 발견, 순회 종료')
            break

        page_num += 1
        time.sleep(0.5)

    new_entries.sort(key=lambda x: x[0], reverse=True)
    return [url for _, url in new_entries]

# ── 게시물 페이지 → 이미지 URL ────────────────────────────────────────────────

def fetch_image_url(page, post_url: str) -> str | None:
    try:
        page.goto(post_url, wait_until='domcontentloaded', timeout=30000)
        html = page.content()
    except Exception as e:
        print(f'  ✗ fetch 실패: {post_url} — {e}', file=sys.stderr)
        return None

    parsed = urlparse(post_url)
    soup   = BeautifulSoup(html, 'html.parser')

    for sel in CONTENT_SELECTORS:
        container = soup.select_one(sel)
        if container:
            imgs = _extract_images(container, parsed)
            if imgs:
                return _pick_largest(imgs)

    all_imgs = _extract_images(soup, parsed)

    attach = [i for i in all_imgs
              if any(p in i['src'] for p in ['/files/attach/', '/attach/images/', '/upload/'])]
    if attach:
        return _pick_largest(attach)

    large = [i for i in all_imgs if i['w'] >= 100 and i['h'] >= 100]
    if large:
        return _pick_largest(large)

    return None

# ── 내부 유틸 ─────────────────────────────────────────────────────────────────

def _extract_images(container, base) -> list[dict]:
    imgs = []
    for img in container.find_all('img'):
        src = (img.get('src') or img.get('data-src') or
               img.get('data-original') or img.get('data-lazy') or '').strip()
        if not src or src.startswith('data:'):
            continue

        if src.startswith('//'):
            src = f'{base.scheme}:{src}'
        elif src.startswith('/'):
            src = f'{base.scheme}://{base.netloc}{src}'
        elif not src.startswith('http'):
            src = urljoin(f'{base.scheme}://{base.netloc}{base.path}', src)

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
    existing, known_urls = load_existing()
    is_first_run = len(existing) == 0

    if is_first_run:
        print('▶ 첫 실행 — 전체 게시물 수집 시작')
    else:
        print('▶ 증분 업데이트 — 새 게시물만 수집')

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            user_agent=(
                'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
            ),
            locale='ko-KR',
        )
        page = context.new_page()

        new_post_urls = fetch_new_post_urls(page, known_urls)
        print(f'\n  새 게시물: {len(new_post_urls)}개')

        if not new_post_urls:
            print('✅ 새 게시물 없음 — images.json 유지')
            browser.close()
            return

        # 새 게시물 이미지 수집
        new_results = []
        for i, post_url in enumerate(new_post_urls):
            time.sleep(PAGE_DELAY)
            image_url = fetch_image_url(page, post_url)
            if image_url:
                new_results.append({'image_url': image_url, 'post_url': post_url})
                print(f'  ✓ [{i+1}/{len(new_post_urls)}] {post_url}')
            else:
                print(f'  ✗ [{i+1}/{len(new_post_urls)}] (이미지 없음) {post_url}',
                      file=sys.stderr)

        browser.close()

    # 새 결과를 기존 목록 앞에 붙이기 (최신순 유지)
    merged = new_results + existing

    output = {
        'updated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'count':      len(merged),
        'images':     merged,
    }

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f'\n✅ images.json 저장 완료 (총 {len(merged)}개 / 신규 {len(new_results)}개)')


if __name__ == '__main__':
    main()
