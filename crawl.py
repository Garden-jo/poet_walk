#!/usr/bin/env python3
"""
nanum.com/site/poet_walk 이미지 크롤러 (Playwright 버전)
cupid.js JS 챌린지를 헤드리스 Chromium으로 통과합니다.
"""

import json
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
MAX_POSTS   = 50
PAGE_DELAY  = 0.3   # 페이지 간 딜레이(초)

EXCLUDE_KEYWORDS = ['logo', 'icon', 'spacer', 'blank.', 'pixel.']

CONTENT_SELECTORS = [
    '.xe_content', '.read_body', '.view_content',
    '.board_view', 'article', '.post-content', '.content_area', '#content',
]

# ── 목록 페이지 → 게시물 URL ──────────────────────────────────────────────────

def fetch_post_urls(page) -> list[str]:
    page.goto(LISTING_URL, wait_until='networkidle', timeout=30000)
    html      = page.content()
    soup      = BeautifulSoup(html, 'html.parser')
    base      = urlparse(LISTING_URL)
    pretty_re = re.compile(r'^' + re.escape(BOARD_PATH) + r'/(\d+)')

    seen         = set()
    post_entries = []

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

        post_entries.append((post_id, resolved))

    post_entries.sort(key=lambda x: x[0], reverse=True)
    return [url for _, url in post_entries[:MAX_POSTS]]


# ── 게시물 페이지 → 이미지 URL ────────────────────────────────────────────────

def fetch_image_url(page, post_url: str) -> str | None:
    try:
        page.goto(post_url, wait_until='domcontentloaded', timeout=30000)
        html   = page.content()
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

        print('▶ 게시물 목록 가져오는 중…')
        post_urls = fetch_post_urls(page)
        print(f'  {len(post_urls)}개 게시물 발견')

        url_order = {url: i for i, url in enumerate(post_urls)}
        results   = []

        for i, post_url in enumerate(post_urls):
            time.sleep(PAGE_DELAY)
            image_url = fetch_image_url(page, post_url)
            if image_url:
                results.append({'image_url': image_url, 'post_url': post_url})
                print(f'  ✓ [{i+1}/{len(post_urls)}] {post_url}')
            else:
                print(f'  ✗ [{i+1}/{len(post_urls)}] (이미지 없음) {post_url}',
                      file=sys.stderr)

        browser.close()

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
