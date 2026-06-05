from flask import Flask, jsonify
import requests
import re
import json
from datetime import datetime, timezone, timedelta
import time
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ========== CONFIGURATION ==========
ARABIC_SOURCES = {
    "shoofelmatch": "https://www.shoofelmatch.com/",
    "koora_online": "https://koora-online.tv/",
    "xkoora": "https://www.xkoora.net/",
    "livekoora": "https://livekoora.info/",
}

# Known stream domain patterns
STREAM_DOMAINS = [
    'drix.online', 'top.drix.online', 'live.drix.online',
    'yalla-shoots.com', 'yalla-shoot.com', 'yallashoot.com',
    'koora-live.tv', 'koora-live.com', 'koora.tv',
    'beinmatch', 'bein-match', 'beinsports',
    'livestream', 'stream', 'player', 'embed',
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en;q=0.9",
    "Referer": "https://www.google.com/",
}

cache = {}
CACHE_TTL = 300

# ========== UTILITIES ==========

def fetch_url(url, headers=None, timeout=20, use_cache=True):
    now = time.time()
    cache_key = url

    if use_cache and cache_key in cache and now - cache[cache_key]['time'] < CACHE_TTL:
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
    """Check if a URL is likely a stream URL"""
    url_lower = url.lower()
    for domain in STREAM_DOMAINS:
        if domain in url_lower:
            return True
    # Also check for common stream patterns
    if any(ext in url_lower for ext in ['.m3u8', '.mp4', '.ts', 'stream', 'live', 'play', 'embed']):
        return True
    return False

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

def extract_matches_from_html(html, source_name):
    """Extract matches from AY_Match containers"""
    if not html:
        return []

    matches = []
    containers = extract_ay_match_containers(html)

    for class_name, content in containers:
        match_info = parse_ay_match_container(content, class_name, source_name)
        if match_info:
            matches.append(match_info)

    return matches

def parse_ay_match_container(content, class_name, source_name):
    """Parse an AY_Match container"""
    if not content or len(content) < 20:
        return None

    info = {
        'source': source_name,
        'source_type': 'AY_Match',
        'status_class': class_name,
    }

    # Determine match status
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

    # Extract team names from alt attributes in TM1 and TM2
    home_team_match = re.search(
        r'<div[^>]*class=["\'][^"\']*MT_Team\s+TM1[^"\']*["\'][^>]*>.*?<img[^>]*alt=["\']([^"\']+)["\']',
        content, re.DOTALL | re.IGNORECASE
    )
    away_team_match = re.search(
        r'<div[^>]*class=["\'][^"\']*MT_Team\s+TM2[^"\']*["\'][^>]*>.*?<img[^>]*alt=["\']([^"\']+)["\']',
        content, re.DOTALL | re.IGNORECASE
    )

    if home_team_match:
        info['home_team'] = home_team_match.group(1).strip()

    if away_team_match:
        info['away_team'] = away_team_match.group(1).strip()

    # Fallback: find all img alt attributes
    if 'home_team' not in info or 'away_team' not in info:
        all_alts = re.findall(r'<img[^>]*alt=["\']([^"\']+)["\']', content)
        if len(all_alts) >= 2:
            info['home_team'] = all_alts[0].strip()
            info['away_team'] = all_alts[1].strip()
        elif len(all_alts) == 1:
            info['home_team'] = all_alts[0].strip()

    # Build teams string
    if 'home_team' in info and 'away_team' in info:
        info['teams'] = f"{info['home_team']} vs {info['away_team']}"
    elif 'home_team' in info:
        info['teams'] = info['home_team']

    # Extract time
    time_match = re.search(r'(\d{1,2}:\d{2})', content)
    if time_match:
        info['time'] = time_match.group(1)

    # Extract score
    score_match = re.search(r'(\d+\s*[-]\s*\d+)', content)
    if score_match:
        info['score'] = score_match.group(1)

    # Extract channel info
    channel_keywords = ['beIN', 'بي إن', 'bein', 'Abu Dhabi', 'أبو ظبي', 'SSC', 'KSA', 'ON Sport', 'STC', 'دبي', 'Dubai', 'Saudi', 'السعودية']
    content_lower = content.lower()
    for kw in channel_keywords:
        if kw.lower() in content_lower:
            info['channel'] = kw
            break

    # Extract ALL links and categorize them
    all_links = re.findall(r'href=["\']([^"\']*)["\']', content)
    if all_links:
        absolute_links = []
        stream_links = []
        match_page_links = []

        for link in all_links:
            # Make absolute
            if link.startswith('http'):
                abs_link = link
            elif link.startswith('/'):
                if source_name == 'shoofelmatch':
                    abs_link = f"https://www.shoofelmatch.com{link}"
                elif source_name == 'koora_online':
                    abs_link = f"https://koora-online.tv{link}"
                elif source_name == 'xkoora':
                    abs_link = f"https://www.xkoora.net{link}"
                elif source_name == 'livekoora':
                    abs_link = f"https://livekoora.info{link}"
                else:
                    abs_link = link
            else:
                abs_link = link

            absolute_links.append(abs_link)

            # Categorize
            if is_stream_url(abs_link):
                stream_links.append(abs_link)
            else:
                match_page_links.append(abs_link)

        info['all_links'] = absolute_links
        if stream_links:
            info['stream_links'] = stream_links
            info['stream_url'] = stream_links[0]  # Primary stream URL
        if match_page_links:
            info['match_links'] = match_page_links

    # Extract league info
    league_match = re.search(r'<div[^>]*class=["\'][^"\']*(?:league|tournament|championship|دوري)[^"\']*["\'][^>]*>(.*?)</div>', content, re.DOTALL | re.IGNORECASE)
    if league_match:
        league_text = re.sub(r'<[^>]+>', '', league_match.group(1)).strip()
        if league_text:
            info['league'] = league_text

    if 'home_team' in info:
        return info

    return None

def get_thesportsdb_matches():
    """Fallback: Get matches from TheSportsDB"""
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

# ========== ROUTES ==========

@app.route('/')
def index():
    return jsonify({
        "status": "online",
        "message": "Arabic Sports API with stream links",
        "sources": list(ARABIC_SOURCES.keys()) + ["thesportsdb"],
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

@app.route('/test')
def test():
    return jsonify({
        "status": "ok",
        "message": "API running",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "cache_entries": len(cache),
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

@app.route('/matches/today')
def get_today_matches():
    """Get today's matches with stream links"""
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

    return jsonify({
        "date": today,
        "matches": all_matches,
        "count": len(all_matches)
    })

@app.route('/matches/live')
def get_live_matches():
    """Get live matches with stream links"""
    all_matches = []

    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=False)
        if html:
            matches = extract_matches_from_html(html, name)
            for m in matches:
                if m.get('status') == 'live':
                    m['is_live'] = True
                    all_matches.append(m)

    return jsonify({
        "matches": all_matches,
        "count": len(all_matches)
    })

@app.route('/streams')
def get_all_streams():
    """Get all matches that have stream URLs"""
    all_matches = []

    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            matches = extract_matches_from_html(html, name)
            for m in matches:
                if m.get('stream_url'):
                    all_matches.append(m)

    return jsonify({
        "matches_with_streams": all_matches,
        "count": len(all_matches)
    })

@app.route('/stream/<path:url>')
def get_stream_details(url):
    """Get details about a specific stream URL"""
    # Decode URL if encoded
    from urllib.parse import unquote
    decoded_url = unquote(url)

    return jsonify({
        "stream_url": decoded_url,
        "is_stream": is_stream_url(decoded_url),
        "note": "This is a direct stream link. You may need to set proper Referer headers to access it."
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

    return jsonify({
        "channel": "Bein Sports",
        "matches": all_matches,
        "count": len(all_matches)
    })

@app.route('/scrape/<source_name>')
def scrape_source(source_name):
    """Debug: Detailed scrape output"""
    url = ARABIC_SOURCES.get(source_name)
    if not url:
        return jsonify({"error": f"Unknown source: {source_name}"}), 404

    html = fetch_url(url, use_cache=False)
    if not html:
        return jsonify({"error": "Failed to fetch source"}), 500

    matches = extract_matches_from_html(html, source_name)
    containers = extract_ay_match_containers(html)

    # Count streams
    stream_count = sum(1 for m in matches if m.get('stream_url'))

    return jsonify({
        "source": source_name,
        "url": url,
        "title": re.search(r'<title>(.*?)</title>', html, re.IGNORECASE).group(1) if re.search(r'<title>(.*?)</title>', html, re.IGNORECASE) else "No title",
        "content_length": len(html),
        "matches_found": len(matches),
        "matches_with_streams": stream_count,
        "matches": matches[:10],
        "raw_container_count": len(containers),
    })

@app.route('/scrape/all')
def scrape_all_sources():
    """Quick scrape of all sources"""
    results = {}
    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            matches = extract_matches_from_html(html, name)
            stream_count = sum(1 for m in matches if m.get('stream_url'))
            results[name] = {
                "status": "ok",
                "content_length": len(html),
                "matches_found": len(matches),
                "streams_found": stream_count,
                "title": re.search(r'<title>(.*?)</title>', html, re.IGNORECASE).group(1) if re.search(r'<title>(.*?)</title>', html, re.IGNORECASE) else "No title"
            }
        else:
            results[name] = {"status": "failed", "error": "Could not fetch"}

    return jsonify(results)

@app.route('/sources')
def get_sources():
    return jsonify({
        "arabic_sources": ARABIC_SOURCES,
        "backup_sources": ["thesportsdb"],
        "total": len(ARABIC_SOURCES) + 1
    })

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
def get_stream(channel_id):
    return jsonify({
        "error": "Stream URLs unavailable",
        "message": "The old API is offline. Use /matches/today or /streams for match listings with stream links.",
        "channel_id": channel_id
    }), 503

@app.route('/events')
def get_events():
    return get_today_matches()

@app.route('/event/<int:event_id>')
def get_event(event_id):
    return jsonify({
        "id": event_id,
        "message": "Use /matches/today for current match listings",
        "source": "thesportsdb"
    })

@app.route('/verify/<int:channel_id>')
def verify_stream(channel_id):
    return jsonify({
        "channel_id": channel_id,
        "status": "unavailable",
        "message": "Old API is offline. Streams cannot be verified."
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3000)
