from flask import Flask, jsonify
import requests
import re
import json
from datetime import datetime, timezone, timedelta
import time
import logging
from urllib.parse import unquote, urljoin

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ========== CONFIGURATION ==========
ARABIC_SOURCES = {
    "shoofelmatch": "https://www.shoofelmatch.com/",
    "koora_online": "https://koora-online.tv/",
    "xkoora": "https://www.xkoora.net/",
    "livekoora": "https://livekoora.info/",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en;q=0.9",
    "Referer": "https://www.google.com/",
}

cache = {}
CACHE_TTL = 300

# ========== UTILITIES ==========

def fetch_url(url, headers=None, timeout=20, use_cache=True, cache_ttl=CACHE_TTL):
    now = time.time()
    cache_key = url

    if use_cache and cache_key in cache and now - cache[cache_key]['time'] < cache_ttl:
        return cache[cache_key]['data']

    try:
        r = requests.get(url, headers=headers or HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        if use_cache:
            cache[cache_key] = {'data': r.text, 'time': now}
        return r.text
    except Exception as e:
        logging.error(f"Fetch failed for {url}: {e}")
        return None

def extract_ay_match_containers(html):
    """Extract AY_Match containers by finding start tags and matching end tags"""
    containers = []
    start_pattern = r'<div[^>]*class=["\']([^"\']*AY_Match[^"\']*)["\'][^>]*>'

    for match in re.finditer(start_pattern, html, re.IGNORECASE):
        class_name = match.group(1)
        pos = match.end()
        depth = 1
        while pos < len(html) and depth > 0:
            next_open = html.find('<div', pos)
            next_close = html.find('</div>', pos)

            if next_close == -1:
                break

            if next_open != -1 and next_open < next_close:
                depth += 1
                pos = next_open + 4
            else:
                depth -= 1
                pos = next_close + 6

        if depth == 0:
            container_content = html[match.end():pos - 6]
            containers.append((class_name, container_content))

    return containers

def extract_stream_url(html_content):
    """Extract ONLY the working gwoo.online iframe URL"""
    # Look for gwoo.online albaplayer iframe
    gwoo_match = re.search(
        r'https://zzz\.gwoo\.online/albaplayer/[^"\'<>\s]+',
        html_content,
        re.IGNORECASE
    )
    if gwoo_match:
        return gwoo_match.group(0)

    # Fallback: any iframe src that looks like a player
    iframe_match = re.search(
        r'<iframe[^>]*src=["\'](https?://[^"\']*(?:gwoo\.online|albaplayer|player|stream)[^"\']*)["\']',
        html_content,
        re.IGNORECASE
    )
    if iframe_match:
        url = iframe_match.group(1)
        if url.startswith('//'):
            url = 'https:' + url
        return url

    return None

def parse_match_container(content, class_name, source_name):
    """Parse match - clean, minimal output"""
    if not content or len(content) < 20:
        return None

    info = {
        'source': source_name,
        'status': 'unknown',
    }

    # Status
    class_lower = class_name.lower()
    if 'live' in class_lower:
        info['status'] = 'live'
    elif 'comming-soon' in class_lower or 'coming-soon' in class_lower:
        info['status'] = 'upcoming'
    elif 'not-started' in class_lower:
        info['status'] = 'not_started'
    elif 'finished' in class_lower:
        info['status'] = 'finished'

    # Teams
    home_match = re.search(
        r'<div[^>]*class=["\'][^"\']*MT_Team\s+TM1[^"\']*["\'][^>]*>.*?<img[^>]*alt=["\']([^"\']+)["\']',
        content, re.DOTALL | re.IGNORECASE
    )
    away_match = re.search(
        r'<div[^>]*class=["\'][^"\']*MT_Team\s+TM2[^"\']*["\'][^>]*>.*?<img[^>]*alt=["\']([^"\']+)["\']',
        content, re.DOTALL | re.IGNORECASE
    )

    if home_match:
        info['home_team'] = home_match.group(1).strip()
    if away_match:
        info['away_team'] = away_match.group(1).strip()

    if 'home_team' not in info or 'away_team' not in info:
        all_alts = re.findall(r'<img[^>]*alt=["\']([^"\']+)["\']', content)
        if len(all_alts) >= 2:
            info['home_team'] = all_alts[0].strip()
            info['away_team'] = all_alts[1].strip()
        elif len(all_alts) == 1:
            info['home_team'] = all_alts[0].strip()

    if 'home_team' in info and 'away_team' in info:
        info['match'] = f"{info['home_team']} vs {info['away_team']}"
    elif 'home_team' in info:
        info['match'] = info['home_team']
    else:
        return None

    # Time
    time_match = re.search(r'(\d{1,2}:\d{2})', content)
    if time_match:
        info['time'] = time_match.group(1)

    # Score - only small numbers
    score_match = re.search(r'(\d{1,2})\s*[-]\s*(\d{1,2})', content)
    if score_match:
        num1 = int(score_match.group(1))
        num2 = int(score_match.group(2))
        if num1 <= 15 and num2 <= 15:
            info['score'] = f"{num1} - {num2}"

    # Channel
    channel_keywords = ['beIN', 'بي إن', 'bein', 'Abu Dhabi', 'أبو ظبي', 'SSC', 'KSA', 'ON Sport', 'STC', 'دبي', 'Dubai', 'Saudi', 'السعودية']
    content_lower = content.lower()
    for kw in channel_keywords:
        if kw.lower() in content_lower:
            info['channel'] = kw
            break

    # Get stream page URL from href
    links = re.findall(r'href=["\']([^"\']*)["\']', content)
    stream_page = None
    for link in links:
        if link.startswith('http'):
            abs_link = link
        elif link.startswith('/'):
            base = ARABIC_SOURCES.get(source_name, '')
            abs_link = urljoin(base, link)
        else:
            continue

        # Check if it's a stream page (not social/media)
        if any(kw in abs_link.lower() for kw in ['drix.online', 'koora', 'koray', 'match', 'watch', 'live']):
            if not any(noise in abs_link.lower() for noise in ['facebook', 'twitter', 'whatsapp', 'telegram', 't.me']):
                stream_page = abs_link
                break

    if stream_page:
        info['stream_page'] = stream_page
        # Deep extract: fetch the stream page and get iframe
        stream_html = fetch_url(stream_page, headers=HEADERS, use_cache=True, cache_ttl=120)
        if stream_html:
            iframe_url = extract_stream_url(stream_html)
            if iframe_url:
                info['stream_url'] = iframe_url

    return info

def extract_matches_from_html(html, source_name):
    if not html:
        return []
    matches = []
    containers = extract_ay_match_containers(html)
    for class_name, content in containers:
        match_info = parse_match_container(content, class_name, source_name)
        if match_info:
            matches.append(match_info)
    return matches

def get_thesportsdb_matches():
    matches = []
    league_ids = [4328, 4335, 4331, 4332, 4334, 4337]
    for lid in league_ids:
        try:
            data = fetch_url(f"https://www.thesportsdb.com/api/v1/json/3/eventsnextleague.php?id={lid}", use_cache=True)
            if data:
                json_data = json.loads(data)
                events = json_data.get('events', [])
                for event in events:
                    matches.append({
                        'id': event.get('idEvent'),
                        'home_team': event.get('strHomeTeam'),
                        'away_team': event.get('strAwayTeam'),
                        'league': event.get('strLeague'),
                        'date': event.get('dateEvent'),
                        'time': event.get('strTime'),
                        'source': 'thesportsdb',
                    })
        except Exception as e:
            logging.error(f"TheSportsDB error: {e}")
    return matches

# ========== ROUTES ==========

@app.route('/')
def index():
    return jsonify({
        "status": "online",
        "message": "Clean Sports API - gwoo.online streams",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

@app.route('/test')
def test():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})

@app.route('/matches/today')
def get_today_matches():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_matches = []

    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            matches = extract_matches_from_html(html, name)
            all_matches.extend(matches)

    if not all_matches:
        db_matches = get_thesportsdb_matches()
        for m in db_matches:
            if m.get('date') == today:
                all_matches.append(m)
    if not all_matches:
        all_matches = get_thesportsdb_matches()[:20]

    # Sort by status
    status_order = {'live': 0, 'upcoming': 1, 'not_started': 2, 'finished': 3}
    all_matches.sort(key=lambda x: status_order.get(x.get('status', 'unknown'), 4))

    return jsonify({
        "date": today,
        "total": len(all_matches),
        "live": sum(1 for m in all_matches if m.get('status') == 'live'),
        "upcoming": sum(1 for m in all_matches if m.get('status') in ['upcoming', 'not_started']),
        "matches": all_matches
    })

@app.route('/matches/live')
def get_live_matches():
    all_matches = []

    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=False)
        if html:
            matches = extract_matches_from_html(html, name)
            for m in matches:
                if m.get('status') == 'live':
                    all_matches.append(m)

    return jsonify({
        "count": len(all_matches),
        "matches": all_matches
    })

@app.route('/streams')
def get_streams():
    """Get only matches with working stream URLs"""
    all_matches = []

    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            matches = extract_matches_from_html(html, name)
            for m in matches:
                if m.get('stream_url'):  # Only include if stream URL found
                    all_matches.append(m)

    # Sort by status
    status_order = {'live': 0, 'upcoming': 1, 'not_started': 2, 'finished': 3}
    all_matches.sort(key=lambda x: status_order.get(x.get('status', 'unknown'), 4))

    # Group by status
    grouped = {}
    for m in all_matches:
        status = m.get('status', 'unknown')
        if status not in grouped:
            grouped[status] = []
        grouped[status].append(m)

    return jsonify({
        "total": len(all_matches),
        "grouped": grouped,
        "matches": all_matches
    })

@app.route('/channels/bein')
def get_bein_matches():
    all_matches = []

    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            matches = extract_matches_from_html(html, name)
            for m in matches:
                if m.get('channel') or 'bein' in str(m).lower() or 'بي إن' in str(m):
                    m['channel'] = m.get('channel', 'Bein Sports')
                    all_matches.append(m)

    return jsonify({"channel": "Bein Sports", "count": len(all_matches), "matches": all_matches})

@app.route('/scrape/<source_name>')
def scrape_source(source_name):
    url = ARABIC_SOURCES.get(source_name)
    if not url:
        return jsonify({"error": f"Unknown source: {source_name}"}), 404

    html = fetch_url(url, use_cache=False)
    if not html:
        return jsonify({"error": "Failed to fetch"}), 500

    matches = extract_matches_from_html(html, source_name)
    with_streams = [m for m in matches if m.get('stream_url')]

    return jsonify({
        "source": source_name,
        "matches_found": len(matches),
        "with_streams": len(with_streams),
        "matches": matches[:10],
    })

@app.route('/sources')
def get_sources():
    return jsonify({"sources": list(ARABIC_SOURCES.keys())})

@app.route('/config')
def get_config():
    return jsonify({
        "mode": "streaming",
        "show_ads": True,
        "maintenance": False,
        "maintenance_message": "التطبيق تحت الصيانة، نعود قريباً",
        "latest_version": "1.0.0",
        "update_url": "https://github.com/abderridr/ursport-app/releases/download/v1.0.0/app-arm64-v8a-release.apk",
        "update_required": False
    })

# ========== LEGACY ROUTES ==========

@app.route('/categories')
def get_categories():
    return jsonify([
        {"id": 4328, "name": "Premier League", "name_ar": "الدوري الإنجليزي"},
        {"id": 4335, "name": "La Liga", "name_ar": "الدوري الإسباني"},
        {"id": 4331, "name": "Bundesliga", "name_ar": "الدوري الألماني"},
        {"id": 4332, "name": "Serie A", "name_ar": "الدوري الإيطالي"},
        {"id": 4334, "name": "Ligue 1", "name_ar": "الدوري الفرنسي"},
        {"id": 4337, "name": "Champions League", "name_ar": "دوري أبطال أوروبا"},
    ])

@app.route('/channels/<int:cat_id>')
def get_channels(cat_id):
    return jsonify([
        {"id": 1, "name": "Bein Sports HD 1", "category_id": cat_id},
        {"id": 2, "name": "Bein Sports HD 2", "category_id": cat_id},
        {"id": 3, "name": "Bein Sports HD 3", "category_id": cat_id},
        {"id": 4, "name": "Bein Sports HD 4", "category_id": cat_id},
        {"id": 5, "name": "Abu Dhabi Sports", "category_id": cat_id},
    ])

@app.route('/stream/<int:channel_id>')
def get_stream_legacy(channel_id):
    return jsonify({
        "error": "Use /streams endpoint",
        "channel_id": channel_id
    }), 503

@app.route('/events')
def get_events():
    return get_today_matches()

@app.route('/event/<int:event_id>')
def get_event(event_id):
    return jsonify({"id": event_id, "message": "Use /matches/today"})

@app.route('/verify/<int:channel_id>')
def verify_stream(channel_id):
    return jsonify({"channel_id": channel_id, "status": "unavailable", "message": "Use /streams endpoint"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3000)
