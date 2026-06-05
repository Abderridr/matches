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
    "koora99": "https://www.koora99.com/",
    "xkoora": "https://www.xkoora.net/",
    "yalla_shoot_new": "https://yalla-shoots.com/",
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

# ========== ADVANCED SCRAPER ==========

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

def extract_json_from_html(html):
    """Extract JSON data embedded in script tags or variables"""
    if not html:
        return []
    
    json_objects = []
    
    # Pattern 1: window.__INITIAL_STATE__ = {...}
    patterns = [
        r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
        r'window\.__DATA__\s*=\s*({.*?});',
        r'window\.__APP__\s*=\s*({.*?});',
        r'var\s+matches\s*=\s*({.*?});',
        r'var\s+data\s*=\s*({.*?});',
        r'const\s+matches\s*=\s*({.*?});',
        r'\"matches\":\s*(\[.*?\])',
        r'\"events\":\s*(\[.*?\])',
        r'\"games\":\s*(\[.*?\])',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, html, re.DOTALL)
        for match in matches:
            try:
                # Try to parse as JSON
                data = json.loads(match)
                json_objects.append(data)
            except json.JSONDecodeError:
                # Might need to fix quotes
                try:
                    fixed = match.replace("'", '"')
                    data = json.loads(fixed)
                    json_objects.append(data)
                except:
                    pass
    
    return json_objects

def extract_matches_from_html(html):
    """Extract match data from HTML structure"""
    if not html:
        return []
    
    matches = []
    
    # Remove scripts and styles for text extraction
    clean_html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    clean_html = re.sub(r'<style[^>]*>.*?</style>', '', clean_html, flags=re.DOTALL | re.IGNORECASE)
    
    # Pattern 1: Look for common match container structures
    # Many Arabic sites use divs with classes like: match-card, game-item, etc.
    
    # Extract all div/li/article elements that might contain matches
    container_patterns = [
        r'<(div|li|article|tr)[^>]*class=[\"\']([^\"\']*(?:match|game|مباراة|event|fixture|schedule|live|بث)[^\"\']*)[\"\'][^>]*>(.*?)</\1>',
        r'<(div|li|article|tr)[^>]*id=[\"\']([^\"\']*(?:match|game|مباراة|event)[^\"\']*)[\"\'][^>]*>(.*?)</\1>',
    ]
    
    for pattern in container_patterns:
        containers = re.findall(pattern, clean_html, re.DOTALL | re.IGNORECASE)
        for tag, class_name, content in containers:
            match_info = parse_match_container(content, class_name)
            if match_info:
                matches.append(match_info)
    
    # Pattern 2: Look for table rows with match data
    table_rows = re.findall(r'<tr[^>]*>(.*?)</tr>', clean_html, re.DOTALL | re.IGNORECASE)
    for row in table_rows:
        match_info = parse_match_container(row, "table-row")
        if match_info:
            matches.append(match_info)
    
    # Pattern 3: Extract text and look for structured match lines
    text = re.sub(r'<[^>]+>', ' ', clean_html)
    text = re.sub(r'\s+', ' ', text).strip()
    
    # Look for patterns like: "Team A vs Team B - HH:MM - Channel"
    # Arabic patterns: "الفريق الأول ضد الفريق الثاني - الساعة - القناة"
    
    # Pattern: Team names (Arabic or English) + time + channel
    match_patterns = [
        # English: Team A vs Team B HH:MM
        r'([A-Za-z\s]{3,25})\s+(?:vs|VS|Vs|v|V)\s+([A-Za-z\s]{3,25}).*?(\d{1,2}:\d{2})',
        # Arabic: Team A ضد Team B
        r'([\u0600-\u06FF\s]{2,20})\s+(?:ضد|vs|VS)\s+([\u0600-\u06FF\s]{2,20})',
        # Mixed: Any text with time
        r'(.{10,60}?)\s+(\d{1,2}:\d{2})\s*(?:-|\|)?\s*(.{0,30})',
    ]
    
    for pattern in match_patterns:
        found = re.findall(pattern, text)
        for groups in found:
            match_info = {
                'teams': ' vs '.join(groups[:2]) if len(groups) >= 2 else groups[0],
                'time': groups[-1] if ':' in str(groups[-1]) else None,
                'raw': str(groups),
                'source_type': 'text_pattern'
            }
            if len(str(groups)) > 15:  # Filter out noise
                matches.append(match_info)
    
    # Pattern 4: Look for channel links that might indicate matches
    channel_patterns = re.findall(
        r'<a[^>]*href=[\"\']([^\"\']*)[\"\'][^>]*>([^<<]*(?:beIN|بي\s*إن|bein|Abu\s*Dhabi|أبو\s*ظبي|SSC|KSA|ON\s*Sport)[^<<]*)</a>',
        html, re.IGNORECASE
    )
    
    for href, channel_name in channel_patterns:
        matches.append({
            'channel': channel_name.strip(),
            'channel_url': href,
            'source_type': 'channel_link'
        })
    
    return matches

def parse_match_container(content, class_name=""):
    """Parse a single match container (div/li/article)"""
    if not content or len(content) < 20:
        return None
    
    info = {
        'container_class': class_name,
        'source_type': 'html_container'
    }
    
    # Extract time
    time_match = re.search(r'(\d{1,2}:\d{2})', content)
    if time_match:
        info['time'] = time_match.group(1)
    
    # Extract team names from alt attributes, title attributes, or text
    team_patterns = [
        r'alt=[\"\']([^\"\']{2,30})[\"\'"]',
        r'title=[\"\']([^\"\']{2,30})[\"\'"]',
        r'>([^<<]{2,25}(?:vs|ضد|VS)[^<<]{2,25})<<',
    ]
    
    for pattern in team_patterns:
        team_match = re.search(pattern, content, re.IGNORECASE)
        if team_match:
            info['teams'] = team_match.group(1).strip()
            break
    
    # Extract channel
    channel_keywords = ['beIN', 'بي إن', 'bein', 'Abu Dhabi', 'أبو ظبي', 'SSC', 'KSA', 'ON Sport', 'Dubai', 'دبي']
    for kw in channel_keywords:
        if kw.lower() in content.lower():
            info['channel'] = kw
            break
    
    # Extract links
    links = re.findall(r'href=[\"\']([^\"\']*)[\"\'"]', content)
    if links:
        info['links'] = links
    
    # Only return if we found meaningful data
    if 'teams' in info or 'time' in info or 'channel' in info:
        return info
    
    return None

def get_thesportsdb_matches():
    """Fallback: Get matches from TheSportsDB"""
    matches = []
    league_ids = [4328, 4335, 4331, 4332, 4334, 4337]  # Major leagues + UCL
    
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
                        'has_bein': 'bein' in str(event.get('strChannel', '')).lower()
                    })
        except Exception as e:
            logging.error(f"TheSportsDB error: {e}")
    
    return matches

# ========== ROUTES ==========

@app.route('/')
def index():
    return jsonify({
        "status": "online",
        "message": "Arabic Sports API running",
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
    """Get today's matches from all sources"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_matches = []
    
    # Try Arabic sources
    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            # Try JSON extraction first (for JS-rendered sites)
            json_data = extract_json_from_html(html)
            for data in json_data:
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            item['source'] = name
                            item['source_type'] = 'json_embedded'
                            all_matches.append(item)
                elif isinstance(data, dict):
                    # Look for matches/events/games keys
                    for key in ['matches', 'events', 'games', 'fixtures']:
                        if key in data and isinstance(data[key], list):
                            for item in data[key]:
                                if isinstance(item, dict):
                                    item['source'] = name
                                    item['source_type'] = 'json_embedded'
                                    all_matches.append(item)
            
            # Try HTML extraction
            html_matches = extract_matches_from_html(html)
            for m in html_matches:
                m['source'] = name
                all_matches.append(m)
    
    # Fallback to TheSportsDB
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

@app.route('/matches/tomorrow')
def get_tomorrow_matches():
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    all_matches = []
    
    db_matches = get_thesportsdb_matches()
    for m in db_matches:
        if m.get('date') == tomorrow:
            all_matches.append(m)
    
    if not all_matches:
        all_matches = db_matches[:10]
    
    return jsonify({
        "date": tomorrow,
        "matches": all_matches,
        "count": len(all_matches)
    })

@app.route('/matches/live')
def get_live_matches():
    all_matches = []
    
    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=False)
        if html:
            live_keywords = ['مباشر', 'live', 'بث', 'جارية', 'now', ' LIVE ', 'بث مباشر']
            is_live = any(kw in html for kw in live_keywords)
            
            if is_live:
                matches = extract_matches_from_html(html)
                for m in matches:
                    m['source'] = name
                    m['is_live'] = True
                    all_matches.append(m)
    
    return jsonify({
        "matches": all_matches,
        "count": len(all_matches)
    })

@app.route('/channels/bein')
def get_bein_matches():
    all_matches = []
    
    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            matches = extract_matches_from_html(html)
            for m in matches:
                if m.get('channel') or 'bein' in str(m).lower() or 'بي إن' in str(m):
                    m['source'] = name
                    m['channel'] = m.get('channel', 'Bein Sports')
                    all_matches.append(m)
    
    # Also check TheSportsDB
    db_matches = get_thesportsdb_matches()
    for m in db_matches:
        if m.get('has_bein'):
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
    
    # Extract title
    title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
    title = title_match.group(1) if title_match else "No title"
    
    # Extract JSON data
    json_data = extract_json_from_html(html)
    
    # Extract matches via HTML
    html_matches = extract_matches_from_html(html)
    
    # Extract all links
    all_links = re.findall(r'href=[\"\']([^\"\']*)[\"\'"]', html)
    channel_links = [l for l in all_links if any(kw in l.lower() for kw in ['bein', 'abu', 'ssc', 'sport', 'live', 'channel'])]
    
    # Look for API endpoints
    api_patterns = re.findall(r'[\"\']([^\"\']*api[^\"\']*)[\"\'"]', html, re.IGNORECASE)
    
    # Extract text preview
    clean_html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    clean_html = re.sub(r'<style[^>]*>.*?</style>', '', clean_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', clean_html)
    text = re.sub(r'\s+', ' ', text).strip()
    
    return jsonify({
        "source": source_name,
        "url": url,
        "title": title,
        "content_length": len(html),
        "json_objects_found": len(json_data),
        "json_previews": [str(d)[:200] for d in json_data[:3]],
        "html_matches_found": len(html_matches),
        "html_matches": html_matches[:10],
        "channel_links": channel_links[:20],
        "api_endpoints_found": list(set(api_patterns))[:10],
        "text_preview": text[:800]
    })

@app.route('/scrape/all')
def scrape_all_sources():
    """Quick scrape of all sources"""
    results = {}
    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            json_data = extract_json_from_html(html)
            html_matches = extract_matches_from_html(html)
            results[name] = {
                "status": "ok",
                "content_length": len(html),
                "json_found": len(json_data),
                "matches_found": len(html_matches),
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
        "message": "The old API is offline. Use /matches/today or /matches/live for match listings.",
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
