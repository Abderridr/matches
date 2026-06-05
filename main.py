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
# Primary source: Arabic sports aggregator sites
ARABIC_SOURCES = {
    "shoofelmatch": "https://www.shoofelmatch.com/",
    "koora_online": "https://koora-online.tv/",
    "koora99": "https://www.koora99.com/",
    "xkoora": "https://www.xkoora.net/",
    "live_sports": "https://www.live-sports-tv.info/",
}

# Backup: TheSportsDB (free, no key)
THESPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en;q=0.9",
    "Referer": "https://www.google.com/",
}

cache = {}
CACHE_TTL = 300  # 5 minutes

# ========== UTILITIES ==========

def fetch_url(url, headers=None, timeout=15, use_cache=True):
    """Generic fetcher with caching"""
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

def parse_arabic_matches(html_content):
    """Parse match data from Arabic sports sites"""
    if not html_content:
        return []
    
    matches = []
    
    # Common patterns for Arabic match listings
    # Pattern 1: Match cards with teams and time
    # Look for patterns like: "Team A vs Team B" or "Team A ضد Team B"
    
    # Try to find match blocks
    # Many sites use similar structure: team names, time, score, channel
    
    # Pattern for team names (Arabic text around vs/ضد)
    team_patterns = [
        r'([^\n<>]{3,30})\s*(?:vs|ضد|VS|&)\s*([^\n<>]{3,30})',
        r'([^\n<>]{3,30})\s*-\s*([^\n<>]{3,30})',
    ]
    
    # Pattern for time (HH:MM format)
    time_pattern = r'(\d{1,2}:\d{2})'
    
    # Pattern for channels (Bein Sports, Abu Dhabi Sports, etc.)
    channel_patterns = [
        r'(?:beIN|بي إن|bein)\s*(?:SPORTS|سبورت)?\s*(?:HD|Premium)?\s*(\d*)',
        r'(?:Abu Dhabi|أبو ظبي)',
        r'(?:SSC|Saudi)',
        r'(?:ON Time|ON Sport)',
    ]
    
    # Extract all time occurrences
    times = re.findall(time_pattern, html_content)
    
    # Simple extraction: find lines with both teams and time
    lines = html_content.split('\n')
    for line in lines:
        line = line.strip()
        if len(line) > 20 and len(line) < 300:
            # Check if line contains match indicators
            has_teams = any(kw in line.lower() for kw in ['vs', 'ضد', 'vs.', 'VS'])
            has_time = re.search(time_pattern, line)
            
            if has_teams and has_time:
                # Try to extract clean match info
                match_info = {
                    'raw': line,
                    'time': has_time.group(1) if has_time else None,
                    'has_bein': 'bein' in line.lower() or 'بي إن' in line or 'beIN' in line,
                }
                matches.append(match_info)
    
    # If no structured matches found, try regex extraction
    if not matches:
        # Look for common match listing patterns
        match_blocks = re.findall(
            r'([^\n<>]{2,25}(?:vs|ضد)[^\n<>]{2,25}).*?(\d{1,2}:\d{2})',
            html_content,
            re.IGNORECASE
        )
        for block in match_blocks:
            matches.append({
                'teams': block[0].strip(),
                'time': block[1],
                'raw': f"{block[0]} - {block[1]}"
            })
    
    return matches

def get_thesportsdb_matches():
    """Fallback: Get matches from TheSportsDB"""
    matches = []
    # Major leagues: Premier League (4328), La Liga (4335), Bundesliga (4331), Serie A (4332), Ligue 1 (4334)
    league_ids = [4328, 4335, 4331, 4332, 4334]
    
    for lid in league_ids:
        try:
            data = fetch_url(f"{THESPORTSDB_BASE}/eventsnextleague.php?id={lid}", use_cache=True)
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
                        'source': 'thesportsdb'
                    })
        except Exception as e:
            logging.error(f"TheSportsDB error for league {lid}: {e}")
    
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
    """Get today's matches from Arabic sources + TheSportsDB"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_matches = []
    
    # Try Arabic sources first
    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            matches = parse_arabic_matches(html)
            for m in matches:
                m['source'] = name
                all_matches.append(m)
    
    # Fallback to TheSportsDB
    if not all_matches:
        db_matches = get_thesportsdb_matches()
        for m in db_matches:
            if m.get('date') == today:
                all_matches.append(m)
    
    if not all_matches:
        # Return TheSportsDB next matches as last resort
        all_matches = get_thesportsdb_matches()[:20]
    
    return jsonify({
        "date": today,
        "matches": all_matches,
        "count": len(all_matches)
    })

@app.route('/matches/tomorrow')
def get_tomorrow_matches():
    """Get tomorrow's matches"""
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    all_matches = []
    
    # Try TheSportsDB
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
    """Get currently live matches"""
    # Try Arabic sources for live indicators
    all_matches = []
    
    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=False)  # Don't cache live data
        if html:
            # Look for live indicators
            live_keywords = ['مباشر', 'live', 'بث', 'جارية', 'now', ' LIVE ']
            if any(kw in html for kw in live_keywords):
                matches = parse_arabic_matches(html)
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
    """Get matches broadcasting on Bein Sports"""
    all_matches = []
    
    for name, url in ARABIC_SOURCES.items():
        html = fetch_url(url, use_cache=True)
        if html:
            matches = parse_arabic_matches(html)
            for m in matches:
                if m.get('has_bein') or 'bein' in str(m).lower():
                    m['source'] = name
                    m['channel'] = 'Bein Sports'
                    all_matches.append(m)
    
    return jsonify({
        "channel": "Bein Sports",
        "matches": all_matches,
        "count": len(all_matches)
    })

@app.route('/scrape/<source_name>')
def scrape_source(source_name):
    """Debug: Raw scrape output from a specific source"""
    url = ARABIC_SOURCES.get(source_name)
    if not url:
        return jsonify({"error": f"Unknown source: {source_name}"}), 404
    
    html = fetch_url(url, use_cache=False)
    if not html:
        return jsonify({"error": "Failed to fetch source"}), 500
    
    # Extract basic info
    title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
    title = title_match.group(1) if title_match else "No title"
    
    # Extract all text content (simplified)
    text_content = re.sub(r'<[^>]+>', ' ', html)
    text_content = re.sub(r'\s+', ' ', text_content).strip()
    
    matches = parse_arabic_matches(html)
    
    return jsonify({
        "source": source_name,
        "url": url,
        "title": title,
        "content_length": len(html),
        "matches_found": len(matches),
        "matches": matches[:10],  # Limit output
        "text_preview": text_content[:500]
    })

@app.route('/sources')
def get_sources():
    """List all available sources"""
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

# ========== LEGACY ROUTES (for backward compatibility) ==========

@app.route('/categories')
def get_categories():
    """Legacy: Return leagues as categories"""
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
    """Legacy: Return channels for a category"""
    return jsonify([
        {"id": 1, "name": "Bein Sports HD 1", "category_id": cat_id},
        {"id": 2, "name": "Bein Sports HD 2", "category_id": cat_id},
        {"id": 3, "name": "Bein Sports HD 3", "category_id": cat_id},
        {"id": 4, "name": "Bein Sports HD 4", "category_id": cat_id},
        {"id": 5, "name": "Abu Dhabi Sports", "category_id": cat_id},
    ])

@app.route('/stream/<int:channel_id>')
def get_stream(channel_id):
    """Legacy: Stream URLs are no longer available from old API"""
    return jsonify({
        "error": "Stream URLs unavailable",
        "message": "The old API is offline. Use /matches/today or /matches/live for match listings.",
        "channel_id": channel_id
    }), 503

@app.route('/events')
def get_events():
    """Legacy: Redirect to today's matches"""
    return get_today_matches()

@app.route('/event/<int:event_id>')
def get_event(event_id):
    """Legacy: Event details"""
    return jsonify({
        "id": event_id,
        "message": "Use /matches/today for current match listings",
        "source": "thesportsdb"
    })

@app.route('/verify/<int:channel_id>')
def verify_stream(channel_id):
    """Legacy: Stream verification no longer works"""
    return jsonify({
        "channel_id": channel_id,
        "status": "unavailable",
        "message": "Old API is offline. Streams cannot be verified."
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3000)
