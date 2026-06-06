from flask import Flask, jsonify
import requests
import re
import json
from datetime import datetime, timezone, timedelta
import time
import logging
from urllib.parse import unquote, urljoin, quote

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ========== CONFIGURATION ==========
ARABIC_SOURCES = {
    "shoofelmatch": "https://www.shoofelmatch.com/",
    "koora_online": "https://koora-online.tv/",
    "xkoora": "https://www.xkoora.net/",
    "livekoora": "https://livekoora.info/",
}

STREAM_DOMAINS = [
    'drix.online', 'top.drix.online', 'live.drix.online',
    'yalla-shoots.com', 'yalla-shoot.com', 'yallashoot.com',
    'koora-live.tv', 'koora-live.com', 'koora.tv',
    'beinmatch', 'bein-match', 'beinsports',
    'livestream', 'stream', 'player', 'embed',
    'livekooracom', 'koora-online.mov', 'koray.live',
    'gwoo.online', 'albaplayer', 'sport4all',
]

# URLs to exclude from stream_all_urls
NOISE_PATTERNS = [
    r'wp-content', r'wp-includes', r'wp-admin',
    r'facebook.com', r'twitter.com', r'whatsapp.com', r'telegram.me',
    r'feed/', r'comments/feed',
    r'\.css', r'\.js', r'\.woff', r'\.png', r'\.jpg', r'\.webp', r'\.gif',
    r'\.jpeg', r'\.svg', r'\.ico',
    r'sharer\.php', r'share\?url', r'intent/tweet',
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en;q=0.9",
    "Referer": "https://www.google.com/",
}

cache = {}
CACHE_TTL = 300
DEEP_CACHE_TTL = 60

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

def is_stream_url(url):
    url_lower = url.lower()
    for domain in STREAM_DOMAINS:
        if domain in url_lower:
            return True
    if any(ext in url_lower for ext in ['.m3u8', '.mp4', '.ts', 'stream', 'live', 'play', 'embed']):
        return True
    return False

def is_noise_url(url):
    """Check if URL is noise (assets, social shares, etc.)"""
    for pattern in NOISE_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False

def extract_deep_stream(stream_page_url, referer=None):
    """Extract actual video stream URL from a stream page"""
    headers = dict(HEADERS)
    if referer:
        headers['Referer'] = referer

    html = fetch_url(stream_page_url, headers=headers, use_cache=True, cache_ttl=DEEP_CACHE_TTL)
    if not html:
        return None

    result = {
        'source_page': stream_page_url,
        'stream_type': 'unknown',
        'found_urls': []
    }

    # Pattern 1: iframe src
    iframe_match = re.search(r'<iframe[^>]*src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if iframe_match:
        iframe_url = iframe_match.group(1)
        if iframe_url.startswith('//'):
            iframe_url = 'https:' + iframe_url
        elif iframe_url.startswith('/'):
            iframe_url = urljoin(stream_page_url, iframe_url)
        result['iframe_url'] = iframe_url
        result['found_urls'].append(iframe_url)

    # Pattern 2: video source
    video_match = re.search(r'<video[^>]*src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if video_match:
        result['video_url'] = video_match.group(1)
        result['found_urls'].append(video_match.group(1))

    # Pattern 3: .m3u8 URLs
    m3u8_matches = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html)
    if m3u8_matches:
        result['m3u8_urls'] = m3u8_matches
        result['found_urls'].extend(m3u8_matches)

    # Pattern 4: .mp4 URLs
    mp4_matches = re.findall(r'https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*', html)
    if mp4_matches:
        result['mp4_urls'] = mp4_matches
        result['found_urls'].extend(mp4_matches)

    # Pattern 5: Stream URLs in JS - broader patterns
    js_patterns = [
        r'src\s*:\s*["\']([^"\']+)["\']',
        r'url\s*:\s*["\']([^"\']+)["\']',
        r'file\s*:\s*["\']([^"\']+)["\']',
        r'["\']([^"\']*\.m3u8[^"\']*)["\']',
        r'["\']([^"\']*albaplayer[^"\']*)["\']',
        r'["\']([^"\']*gwoo\.online[^"\']*)["\']',
        r'["\']([^"\']*sport4all[^"\']*)["\']',
        r'["\']([^"\']*drix\.online[^"\']*)["\']',
    ]
    for pattern in js_patterns:
        matches = re.findall(pattern, html)
        for m in matches:
            if ('http' in m or '.m3u8' in m or '.mp4' in m or 'player' in m or 'stream' in m) and m not in result['found_urls']:
                result['found_urls'].append(m)

    # Pattern 6: Any http URLs that look like streams
    http_matches = re.findall(r'https?://[^\s"\'<>]+', html)
    for m in http_matches:
        if any(kw in m.lower() for kw in ['player', 'stream', 'live', 'm3u8', 'mp4', 'video', 'gwoo', 'drix', 'sport4all']):
            if m not in result['found_urls'] and not is_noise_url(m):
                result['found_urls'].append(m)

    # Deduplicate and find best URL
    seen = set()
    unique_urls = []
    for url in result['found_urls']:
        if url not in seen and len(url) > 10 and not is_noise_url(url):
            seen.add(url)
            unique_urls.append(url)
    result['found_urls'] = unique_urls

    # Determine best URL
    if result['found_urls']:
        for url in result['found_urls']:
            if '.m3u8' in url:
                result['best_url'] = url
                result['stream_type'] = 'hls'
                break
        else:
            for url in result['found_urls']:
                if '.mp4' in url:
                    result['best_url'] = url
                    result['stream_type'] = 'mp4'
                    break
            else:
                result['best_url'] = result['found_urls'][0]
                if 'iframe' in result:
                    result['stream_type'] = 'iframe'
                elif 'player' in result['best_url'].lower():
                    result['stream_type'] = 'player_page'

    return result if result.get('best_url') else None

def extract_ay_match_containers(html):
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

def parse_ay_match_container(content, class_name, source_name):
    if not content or len(content) < 20:
        return None

    info = {'source': source_name, 'source_type': 'AY_Match'}

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
    else:
        info['status'] = 'unknown'

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
        info['teams'] = f"{info['home_team']} vs {info['away_team']}"
    elif 'home_team' in info:
        info['teams'] = info['home_team']

    # Time - look for HH:MM format
    time_match = re.search(r'(\d{1,2}:\d{2})', content)
    if time_match:
        info['time'] = time_match.group(1)

    # Score - ONLY match patterns like "0 - 0", "2-1", "1 - 2" with small numbers
    # Avoid matching dates like "2026-06" or "3-2026"
    score_match = re.search(r'(\d{1,2})\s*[-]\s*(\d{1,2})', content)
    if score_match:
        num1 = int(score_match.group(1))
        num2 = int(score_match.group(2))
        # Only accept if both numbers are reasonable scores (0-20)
        if num1 <= 20 and num2 <= 20:
            info['score'] = f"{num1} - {num2}"

    # Channel
    channel_keywords = ['beIN', 'بي إن', 'bein', 'Abu Dhabi', 'أبو ظبي', 'SSC', 'KSA', 'ON Sport', 'STC', 'دبي', 'Dubai', 'Saudi', 'السعودية']
    content_lower = content.lower()
    for kw in channel_keywords:
        if kw.lower() in content_lower:
            info['channel'] = kw
            break

    # Links - extract and filter noise
    all_links = re.findall(r'href=["\']([^"\']*)["\']', content)
    if all_links:
        absolute_links = []
        stream_links = []

        for link in all_links:
            if link.startswith('http'):
                abs_link = link
            elif link.startswith('/'):
                base = ARABIC_SOURCES.get(source_name, '')
                abs_link = urljoin(base, link)
            else:
                abs_link = link

            if not is_noise_url(abs_link):
                absolute_links.append(abs_link)
                if is_stream_url(abs_link):
                    stream_links.append(abs_link)

        if absolute_links:
            info['links'] = absolute_links
        if stream_links:
            info['stream_links'] = stream_links
            info['stream_page_url'] = stream_links[0]

    # League
    league_match = re.search(r'<div[^>]*class=["\'][^"\']*(?:league|tournament|championship|دوري)[^"\']*["\'][^>]*>(.*?)</div>', content, re.DOTALL | re.IGNORECASE)
    if league_match:
        league_text = re.sub(r'<[^>]+>', '', league_match.group(1)).strip()
        if league_text:
            info['league'] = league_text

    return info if 'home_team' in info else None

def extract_matches_from_html(html, source_name):
    if not html:
        return []
    matches = []
    containers = extract_ay_match_containers(html)
    for class_name, content in containers:
        match_info = parse_ay_match_container(content, class_name, source_name)
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
                        'timestamp': event.get('strTimestamp'),
                        'channel': event.get('strChannel', 'Unknown'),
                        'source': 'thesportsdb',
                    })
        except Exception as e:
            logging.error(f"TheSportsDB error: {e}")
    return matches

def enrich_match_with_stream(match):
    """Automatically extract deep stream URL for a match"""
    if not match.get('stream_page_url'):
        return match

    deep = extract_deep_stream(match['stream_page_url'], referer=ARABIC_SOURCES.get(match.get('source', '')))
    if deep and deep.get('best_url'):
        match['stream_url'] = deep['best_url']
        match['stream_type'] = deep.get('stream_type', 'unknown')
        match['stream_iframe'] = deep.get('iframe_url')
        match['stream_m3u8'] = deep.get('m3u8_urls', [])
        match['stream_mp4'] = deep.get('mp4_urls', [])
        # Only include non-noise URLs
        match['stream_all_urls'] = [u for u in deep.get('found_urls', []) if not is_noise_url(u)]

    return match

# ========== ROUTES ==========

@app.route('/')
def index():
    return jsonify({
        "status": "online",
        "message": "Arabic Sports API - Clean Stream Extraction",
        "endpoints": {
            "/matches/today": "Today's matches with auto stream URLs",
            "/matches/live": "Live matches with auto stream URLs",
            "/streams": "All matches with stream URLs (auto deep extraction)",
            "/channels/bein": "Bein Sports matches",
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

@app.route('/test')
def test():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})

@app.route('/health')
def health():
    return jsonify({"status": "healthy", "cache_entries": len(cache), "timestamp": datetime.now(timezone.utc).isoformat()})

@app.route('/matches/today')
def get_today_matches():
    """Get today's matches - auto deep stream extraction for live/upcoming"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_matches = []

    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            matches = extract_matches_from_html(html, name)
            # Auto-enrich live and upcoming matches with stream URLs
            for m in matches:
                if m.get('status') in ['live', 'upcoming', 'not_started']:
                    m = enrich_match_with_stream(m)
            all_matches.extend(matches)

    if not all_matches:
        db_matches = get_thesportsdb_matches()
        for m in db_matches:
            if m.get('date') == today:
                all_matches.append(m)
    if not all_matches:
        all_matches = get_thesportsdb_matches()[:20]

    # Sort by status: live first, then upcoming, then not_started, then finished
    status_order = {'live': 0, 'upcoming': 1, 'not_started': 2, 'finished': 3, 'unknown': 4}
    all_matches.sort(key=lambda x: status_order.get(x.get('status', 'unknown'), 4))

    return jsonify({
        "date": today,
        "total_matches": len(all_matches),
        "live_count": sum(1 for m in all_matches if m.get('status') == 'live'),
        "upcoming_count": sum(1 for m in all_matches if m.get('status') in ['upcoming', 'not_started']),
        "matches": all_matches
    })

@app.route('/matches/live')
def get_live_matches():
    """Get live matches with auto stream extraction"""
    all_matches = []

    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=False)
        if html:
            matches = extract_matches_from_html(html, name)
            for m in matches:
                if m.get('status') == 'live':
                    m = enrich_match_with_stream(m)
                    all_matches.append(m)

    return jsonify({
        "count": len(all_matches),
        "matches": all_matches
    })

@app.route('/streams')
def get_all_streams():
    """Get all matches with stream URLs - auto deep extraction"""
    all_matches = []

    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            matches = extract_matches_from_html(html, name)
            for m in matches:
                if m.get('stream_page_url'):
                    m = enrich_match_with_stream(m)
                    if m.get('stream_url'):
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
        "total_streams": len(all_matches),
        "grouped_by_status": grouped,
        "all_matches": all_matches
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
                    if m.get('stream_page_url'):
                        m = enrich_match_with_stream(m)
                    all_matches.append(m)

    return jsonify({"channel": "Bein Sports", "count": len(all_matches), "matches": all_matches})

@app.route('/stream/deep/<path:url>')
def get_deep_stream(url):
    """Manual deep extraction for a specific URL"""
    decoded_url = unquote(url)
    if not decoded_url.startswith('http'):
        return jsonify({"error": "Invalid URL"}), 400

    stream_info = extract_deep_stream(decoded_url)

    if not stream_info:
        return jsonify({"error": "Could not extract stream", "url": decoded_url}), 500

    return jsonify(stream_info)

@app.route('/scrape/<source_name>')
def scrape_source(source_name):
    """Debug: Detailed scrape output"""
    url = ARABIC_SOURCES.get(source_name)
    if not url:
        return jsonify({"error": f"Unknown source: {source_name}"}), 404

    html = fetch_url(url, use_cache=False)
    if not html:
        return jsonify({"error": "Failed to fetch"}), 500

    matches = extract_matches_from_html(html, source_name)

    return jsonify({
        "source": source_name,
        "matches_found": len(matches),
        "matches": matches[:10],
    })

@app.route('/scrape/all')
def scrape_all_sources():
    """Quick scrape of all sources"""
    results = {}
    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            matches = extract_matches_from_html(html, name)
            stream_count = sum(1 for m in matches if m.get('stream_page_url'))
            results[name] = {
                "status": "ok",
                "matches_found": len(matches),
                "stream_pages_found": stream_count,
            }
        else:
            results[name] = {"status": "failed"}
    return jsonify(results)

@app.route('/sources')
def get_sources():
    return jsonify({"arabic_sources": ARABIC_SOURCES, "backup_sources": ["thesportsdb"]})

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
        "error": "Use /streams for auto-extracted stream URLs",
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
