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

def extract_matches_from_html_v2(html, source_name):
    """Extract matches using specific AY_Match container patterns"""
    if not html:
        return []

    matches = []

    # Pattern 1: Extract from AY_Match containers directly
    match_containers = re.findall(
        r'<[^>]*class=["\']([^"\']*AY_Match[^"\']*)["\'][^>]*>(.*?)</(?:div|li|article)>',
        html, re.DOTALL | re.IGNORECASE
    )

    for class_name, content in match_containers:
        match_info = parse_ay_match_container(content, class_name, source_name)
        if match_info:
            matches.append(match_info)

    # Pattern 2: If no AY_Match found, try alternative patterns
    if not matches:
        alt_patterns = [
            r'<div[^>]*data-match[^>]*>(.*?)</div>',
            r'<(?:div|span|td)[^>]*class=["\'][^"\']*(?:team|teamname|name|فريق)[^"\']*["\'][^>]*>(.*?)</(?:div|span|td)>',
        ]
        for pattern in alt_patterns:
            containers = re.findall(pattern, html, re.DOTALL | re.IGNORECASE)
            for content in containers:
                match_info = parse_generic_match(content, source_name)
                if match_info:
                    matches.append(match_info)

    # Pattern 3: Extract from text using structured patterns
    if not matches:
        matches = extract_from_text_patterns(html, source_name)

    return matches

def parse_ay_match_container(content, class_name, source_name):
    """Parse an AY_Match container"""
    if not content or len(content) < 10:
        return None

    info = {
        'source': source_name,
        'source_type': 'AY_Match',
        'status_class': class_name,
    }

    # Determine match status from class name
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

    # Extract team names from alt attributes
    alt_teams = re.findall(r'alt=["\']([^"\']{2,30})["\']', content)
    if len(alt_teams) >= 2:
        info['home_team'] = alt_teams[0]
        info['away_team'] = alt_teams[1]
        info['teams'] = f"{alt_teams[0]} vs {alt_teams[1]}"

    # Extract team names from title attributes
    if 'teams' not in info:
        title_teams = re.findall(r'title=["\']([^"\']{2,30})["\']', content)
        if len(title_teams) >= 2:
            info['home_team'] = title_teams[0]
            info['away_team'] = title_teams[1]
            info['teams'] = f"{title_teams[0]} vs {title_teams[1]}"

    # Extract team names from specific class elements
    if 'teams' not in info:
        team_elements = re.findall(
            r'<(?:span|div|td|a)[^>]*class=["\'][^"\']*(?:team|teamname|name|فريق)[^"\']*["\'][^>]*>(.*?)</(?:span|div|td|a)>',
            content, re.DOTALL | re.IGNORECASE
        )
        if len(team_elements) >= 2:
            teams = []
            for t in team_elements:
                clean = re.sub(r'<[^>]+>', '', t).strip()
                if clean and len(clean) > 1:
                    teams.append(clean)
            if len(teams) >= 2:
                info['home_team'] = teams[0]
                info['away_team'] = teams[1]
                info['teams'] = f"{teams[0]} vs {teams[1]}"

    # Extract time
    time_match = re.search(r'(\d{1,2}:\d{2})', content)
    if time_match:
        info['time'] = time_match.group(1)

    # Extract score
    score_match = re.search(r'(\d+\s*[-]\s*\d+)', content)
    if score_match:
        info['score'] = score_match.group(1)

    # Extract channel info
    channel_keywords = ['beIN', 'بي إن', 'bein', 'Abu Dhabi', 'أبو ظبي', 'SSC', 'KSA', 'ON Sport', 'STC', 'دبي', 'Dubai']
    content_lower = content.lower()
    for kw in channel_keywords:
        if kw.lower() in content_lower:
            info['channel'] = kw
            break

    # Extract links
    links = re.findall(r'href=["\']([^"\']*)["\']', content)
    if links:
        info['links'] = links

    # Only return if we found meaningful data
    if 'teams' in info or 'home_team' in info:
        return info

    return None

def parse_generic_match(content, source_name):
    """Parse generic match container"""
    if not content:
        return None

    info = {
        'source': source_name,
        'source_type': 'generic',
    }

    text = re.sub(r'<[^>]+>', ' ', content)
    text = re.sub(r'\s+', ' ', text).strip()

    if len(text) < 5:
        return None

    time_match = re.search(r'(\d{1,2}:\d{2})', text)
    if time_match:
        info['time'] = time_match.group(1)

    score_match = re.search(r'(\d+\s*[-]\s*\d+)', text)
    if score_match:
        info['score'] = score_match.group(1)

    info['raw_text'] = text[:100]

    return info if len(text) > 10 else None

def extract_from_text_patterns(html, source_name):
    """Extract matches from text using known patterns"""
    matches = []

    clean_html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    clean_html = re.sub(r'<style[^>]*>.*?</style>', '', clean_html, flags=re.DOTALL | re.IGNORECASE)

    text = re.sub(r'<[^>]+>', ' ', clean_html)
    text = re.sub(r'\s+', ' ', text).strip()

    lines = text.split('.')

    for line in lines:
        line = line.strip()
        if len(line) < 15 or len(line) > 200:
            continue

        time_match = re.search(r'(\d{1,2}:\d{2})', line)
        if not time_match:
            continue

        has_score = re.search(r'\d+\s*[-]\s*\d+', line)
        has_status = any(kw in line for kw in ['لم تبدأ', 'جارية', 'بعد قليل', 'مباشر', 'انتهت', 'live', 'finished'])

        if has_score or has_status:
            clean_line = line
            noise_words = ['مباريات', 'الأمس', 'اليوم', 'الغد', 'مباراة', 'بث مباشر', 'يلا شوت', 'koora', 'live']
            for word in noise_words:
                clean_line = clean_line.replace(word, '')

            matches.append({
                'source': source_name,
                'source_type': 'text_pattern',
                'time': time_match.group(1),
                'raw': line[:150],
                'cleaned': clean_line[:150],
            })

    return matches

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

    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            matches = extract_matches_from_html_v2(html, name)
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
            matches = extract_matches_from_html_v2(html, name)
            for m in matches:
                if m.get('status') == 'live':
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
            matches = extract_matches_from_html_v2(html, name)
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

    matches = extract_matches_from_html_v2(html, source_name)

    raw_containers = re.findall(
        r'<[^>]*class=["\']([^"\']*AY_Match[^"\']*)["\'][^>]*>(.*?)</(?:div|li|article)>',
        html, re.DOTALL | re.IGNORECASE
    )

    return jsonify({
        "source": source_name,
        "url": url,
        "title": re.search(r'<title>(.*?)</title>', html, re.IGNORECASE).group(1) if re.search(r'<title>(.*?)</title>', html, re.IGNORECASE) else "No title",
        "content_length": len(html),
        "matches_found": len(matches),
        "matches": matches[:20],
        "raw_container_count": len(raw_containers),
        "sample_raw_containers": [{
            "class": c[0],
            "content_preview": c[1][:200]
        } for c in raw_containers[:5]]
    })

@app.route('/scrape/all')
def scrape_all_sources():
    """Quick scrape of all sources"""
    results = {}
    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            matches = extract_matches_from_html_v2(html, name)
            results[name] = {
                "status": "ok",
                "content_length": len(html),
                "matches_found": len(matches),
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
