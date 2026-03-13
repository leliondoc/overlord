# -*- coding: utf-8 -*-
"""
Overlord Sync Server v1.2.0
Serveur API REST pour synchroniser les données de l'addon Overlord entre tous les joueurs.
Déployé sur Railway.
"""

import os
import sqlite3
import json
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify, g
from flask_cors import CORS

# ============================================================================
# CONFIGURATION & LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024  # 1 Mo max par requête

CORS(app, origins=["https://overlord-production-3ac6.up.railway.app"])

DATABASE = os.environ.get('DATABASE_PATH', 'overlord.db')
API_KEY = os.environ.get('API_KEY', 'OvLd-Ar4th1-2026-Sync')
RESET_SECRET = os.environ.get('RESET_SECRET', '')

# Limites de validation
MAX_ZONE_ID_LEN = 64
MAX_PLAYER_NAME_LEN = 64
MAX_ZONES_PER_REQUEST = 50
MAX_PLAYERS_PER_REQUEST = 200
VALID_FACTIONS = {'Alliance', 'Horde', None}


# ============================================================================
# UTILITAIRES
# ============================================================================

def utcnow_ts():
    """Timestamp UTC courant (compatible Python 3.12+)."""
    return int(datetime.now(timezone.utc).timestamp())


def check_api_key():
    """Vérifie la clé API dans le header X-API-Key."""
    return request.headers.get('X-API-Key', '') == API_KEY


def get_db():
    """Connexion SQLite avec WAL mode et timeout pour la concurrence."""
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA busy_timeout=5000')
    return db


@app.teardown_appcontext
def close_connection(exception):
    """Ferme la connexion à la base de données."""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def init_db():
    """Initialise la base de données avec les tables et index nécessaires."""
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS zones (
            zone_id TEXT PRIMARY KEY,
            owner TEXT,
            status TEXT DEFAULT 'idle',
            kills_current INTEGER DEFAULT 0,
            hold_time_elapsed INTEGER DEFAULT 0,
            captured_time INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_zones_owner ON zones(owner);

        CREATE TABLE IF NOT EXISTS leaderboard_kills (
            player_name TEXT PRIMARY KEY,
            kills INTEGER DEFAULT 0,
            class TEXT,
            faction TEXT,
            updated_at INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS leaderboard_captures (
            player_name TEXT PRIMARY KEY,
            captures TEXT DEFAULT '[]',
            faction TEXT,
            updated_at INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    ''')
    db.commit()
    log.info("Base de donnees initialisee : %s", DATABASE)


def safe_json_loads(raw, fallback=None):
    """json.loads protégé contre les données corrompues."""
    if fallback is None:
        fallback = []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return fallback


# ============================================================================
# GESTIONNAIRES D'ERREURS
# ============================================================================

@app.errorhandler(400)
def bad_request(e):
    return jsonify({'error': 'Bad request'}), 400


@app.errorhandler(403)
def forbidden(e):
    return jsonify({'error': 'Forbidden'}), 403


@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(413)
def payload_too_large(e):
    return jsonify({'error': 'Payload too large'}), 413


@app.errorhandler(500)
def internal_error(e):
    log.exception("Erreur interne")
    return jsonify({'error': 'Internal server error'}), 500


# ============================================================================
# ENDPOINTS API
# ============================================================================

@app.route('/', methods=['GET'])
def index():
    """Health check avec vérification de la base de données."""
    try:
        db = get_db()
        db.execute('SELECT 1').fetchone()
        db_ok = True
    except Exception:
        db_ok = False

    return jsonify({
        'name': 'Overlord Sync Server',
        'version': '1.2.0',
        'status': 'online' if db_ok else 'degraded',
        'database': 'ok' if db_ok else 'error',
        'endpoints': ['/state', '/leaderboard', '/capture', '/kill', '/reset', '/stats']
    })


@app.route('/state', methods=['GET'])
def get_state():
    """Récupère l'état complet des zones."""
    db = get_db()

    zones = db.execute('SELECT * FROM zones').fetchall()
    zones_dict = {}
    for zone in zones:
        zones_dict[zone['zone_id']] = {
            'owner': zone['owner'],
            'status': zone['status'],
            'killsCurrent': zone['kills_current'],
            'holdTimeElapsed': zone['hold_time_elapsed'],
            'capturedTime': zone['captured_time'],
            'updatedAt': zone['updated_at']
        }

    reset_row = db.execute(
        "SELECT value FROM metadata WHERE key = 'last_reset_timestamp'"
    ).fetchone()
    last_reset = int(reset_row['value']) if reset_row else 0

    return jsonify({
        'zones': zones_dict,
        'lastResetTimestamp': last_reset,
        'serverTime': utcnow_ts()
    })


@app.route('/state', methods=['POST'])
def post_state():
    """
    Met à jour l'état des zones.
    Résolution de conflits : le timestamp le plus récent gagne.
    """
    if not check_api_key():
        return jsonify({'error': 'Invalid API key'}), 403
    data = request.get_json()
    if not data or 'zones' not in data:
        return jsonify({'error': 'Missing zones data'}), 400

    zones_data = data['zones']
    if not isinstance(zones_data, dict) or len(zones_data) > MAX_ZONES_PER_REQUEST:
        return jsonify({'error': 'Invalid zones data'}), 400

    db = get_db()
    updated_zones = []

    for zone_id, zone_data in zones_data.items():
        if not isinstance(zone_data, dict):
            continue
        if len(zone_id) > MAX_ZONE_ID_LEN:
            continue

        incoming_ts = zone_data.get('updatedAt', 0)
        if not isinstance(incoming_ts, (int, float)):
            continue

        existing = db.execute(
            'SELECT updated_at FROM zones WHERE zone_id = ?',
            (zone_id,)
        ).fetchone()

        if existing is None or incoming_ts > existing['updated_at']:
            db.execute('''
                INSERT OR REPLACE INTO zones
                (zone_id, owner, status, kills_current, hold_time_elapsed, captured_time, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                zone_id,
                zone_data.get('owner'),
                zone_data.get('status', 'idle'),
                zone_data.get('killsCurrent', 0),
                zone_data.get('holdTimeElapsed', 0),
                zone_data.get('capturedTime', 0),
                int(incoming_ts)
            ))
            updated_zones.append(zone_id)

    if 'lastResetTimestamp' in data:
        db.execute('''
            INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_reset_timestamp', ?)
        ''', (str(data['lastResetTimestamp']),))

    db.commit()

    return jsonify({
        'success': True,
        'updatedZones': updated_zones,
        'serverTime': utcnow_ts()
    })


@app.route('/leaderboard', methods=['GET'])
def get_leaderboard():
    """Récupère le leaderboard complet (kills + captures)."""
    db = get_db()

    kills_rows = db.execute(
        'SELECT * FROM leaderboard_kills ORDER BY kills DESC LIMIT 100'
    ).fetchall()
    kills = {}
    for row in kills_rows:
        kills[row['player_name']] = {
            'kills': row['kills'],
            'class': row['class'],
            'faction': row['faction']
        }

    captures_rows = db.execute(
        'SELECT * FROM leaderboard_captures ORDER BY updated_at DESC LIMIT 100'
    ).fetchall()
    captures = {}
    for row in captures_rows:
        captures[row['player_name']] = {
            'captures': safe_json_loads(row['captures']),
            'faction': row['faction']
        }

    return jsonify({
        'kills': kills,
        'captures': captures
    })


@app.route('/leaderboard', methods=['POST'])
def post_leaderboard():
    """
    Met à jour le leaderboard.
    Accepte deux formats pour la rétrocompatibilité :
      - Format companion (brut Lua) : kills = {"name": 5}, captures = {"name": ["z1"]}
      - Format structuré : kills = {"name": {"kills": 5, "class": "...", "faction": "..."}}
    """
    if not check_api_key():
        return jsonify({'error': 'Invalid API key'}), 403
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Missing data'}), 400

    db = get_db()
    now = utcnow_ts()

    if 'kills' in data and isinstance(data['kills'], dict):
        for player_name, player_data in list(data['kills'].items())[:MAX_PLAYERS_PER_REQUEST]:
            if len(player_name) > MAX_PLAYER_NAME_LEN:
                continue

            # Format brut Lua : kills = {"name": 5}
            if isinstance(player_data, (int, float)):
                incoming_kills = int(player_data)
                player_class = None
                faction = None
            # Format structuré : kills = {"name": {"kills": 5, ...}}
            elif isinstance(player_data, dict):
                incoming_kills = player_data.get('kills', 0)
                player_class = player_data.get('class')
                faction = player_data.get('faction')
            else:
                continue

            existing = db.execute(
                'SELECT kills FROM leaderboard_kills WHERE player_name = ?',
                (player_name,)
            ).fetchone()

            if existing is None or incoming_kills > existing['kills']:
                db.execute('''
                    INSERT OR REPLACE INTO leaderboard_kills
                    (player_name, kills, class, faction, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (player_name, incoming_kills, player_class, faction, now))

    if 'captures' in data and isinstance(data['captures'], dict):
        for player_name, player_data in list(data['captures'].items())[:MAX_PLAYERS_PER_REQUEST]:
            if len(player_name) > MAX_PLAYER_NAME_LEN:
                continue

            # Format brut Lua : captures = {"name": ["z1", "z2"]} ou {"name": {}}
            if isinstance(player_data, list):
                captures_list = player_data
                faction = None
            elif isinstance(player_data, dict):
                # Dict vide ({}) = pas de captures
                captures_list = player_data.get('captures', [])
                if isinstance(captures_list, dict):
                    captures_list = []
                faction = player_data.get('faction')
            else:
                continue

            existing = db.execute(
                'SELECT captures FROM leaderboard_captures WHERE player_name = ?',
                (player_name,)
            ).fetchone()

            if existing:
                existing_captures = set(safe_json_loads(existing['captures']))
                merged = list(existing_captures | set(captures_list))
            else:
                merged = captures_list

            db.execute('''
                INSERT OR REPLACE INTO leaderboard_captures
                (player_name, captures, faction, updated_at)
                VALUES (?, ?, ?, ?)
            ''', (player_name, json.dumps(merged), faction, now))

    db.commit()
    return jsonify({'success': True, 'serverTime': now})


@app.route('/capture', methods=['POST'])
def post_capture():
    """Enregistre une capture de zone."""
    if not check_api_key():
        return jsonify({'error': 'Invalid API key'}), 403
    data = request.get_json()
    if not data or 'zoneId' not in data or 'playerName' not in data:
        return jsonify({'error': 'Missing zoneId or playerName'}), 400

    zone_id = str(data['zoneId'])[:MAX_ZONE_ID_LEN]
    player_name = str(data['playerName'])[:MAX_PLAYER_NAME_LEN]
    faction = data.get('faction')

    db = get_db()
    now = utcnow_ts()

    db.execute('''
        INSERT OR REPLACE INTO zones
        (zone_id, owner, status, kills_current, hold_time_elapsed, captured_time, updated_at)
        VALUES (?, ?, 'captured', 0, 0, ?, ?)
    ''', (zone_id, faction, now, now))

    existing = db.execute(
        'SELECT captures FROM leaderboard_captures WHERE player_name = ?',
        (player_name,)
    ).fetchone()

    if existing:
        captures = set(safe_json_loads(existing['captures']))
        captures.add(zone_id)
        captures = list(captures)
    else:
        captures = [zone_id]

    db.execute('''
        INSERT OR REPLACE INTO leaderboard_captures
        (player_name, captures, faction, updated_at)
        VALUES (?, ?, ?, ?)
    ''', (player_name, json.dumps(captures), faction, now))

    db.commit()
    log.info("Capture: %s -> %s (%s)", player_name, zone_id, faction)

    return jsonify({
        'success': True,
        'zoneId': zone_id,
        'playerName': player_name,
        'serverTime': now
    })


@app.route('/kill', methods=['POST'])
def post_kill():
    """Enregistre un kill."""
    if not check_api_key():
        return jsonify({'error': 'Invalid API key'}), 403
    data = request.get_json()
    if not data or 'playerName' not in data:
        return jsonify({'error': 'Missing playerName'}), 400

    player_name = str(data['playerName'])[:MAX_PLAYER_NAME_LEN]
    zone_id = data.get('zoneId')

    db = get_db()
    now = utcnow_ts()

    existing = db.execute(
        'SELECT kills FROM leaderboard_kills WHERE player_name = ?',
        (player_name,)
    ).fetchone()

    new_kills = (existing['kills'] + 1) if existing else 1

    db.execute('''
        INSERT OR REPLACE INTO leaderboard_kills
        (player_name, kills, class, faction, updated_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (player_name, new_kills, data.get('class'), data.get('faction'), now))

    if zone_id:
        zone_id = str(zone_id)[:MAX_ZONE_ID_LEN]
        zone = db.execute(
            'SELECT kills_current FROM zones WHERE zone_id = ?',
            (zone_id,)
        ).fetchone()
        if zone:
            db.execute(
                'UPDATE zones SET kills_current = ?, updated_at = ? WHERE zone_id = ?',
                (zone['kills_current'] + 1, now, zone_id)
            )

    db.commit()

    return jsonify({
        'success': True,
        'playerName': player_name,
        'totalKills': new_kills,
        'serverTime': now
    })


@app.route('/reset', methods=['POST'])
def reset_campaign():
    """
    Reset hebdomadaire de la campagne.
    Requiert la clé API ET le secret de reset.
    """
    if not check_api_key():
        return jsonify({'error': 'Invalid API key'}), 403

    data = request.get_json() or {}
    if not RESET_SECRET or data.get('secret') != RESET_SECRET:
        return jsonify({'error': 'Invalid or missing reset secret'}), 403

    db = get_db()
    now = utcnow_ts()

    db.execute('DELETE FROM zones')
    db.execute('DELETE FROM leaderboard_kills')
    db.execute('DELETE FROM leaderboard_captures')
    db.execute('''
        INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_reset_timestamp', ?)
    ''', (str(now),))

    db.commit()
    log.info("Reset de campagne effectue a %s", now)

    return jsonify({
        'success': True,
        'resetTimestamp': now,
        'serverTime': now
    })


@app.route('/stats', methods=['GET'])
def get_stats():
    """Statistiques du serveur (pour le companion)."""
    db = get_db()

    zones_count = db.execute('SELECT COUNT(*) as count FROM zones').fetchone()['count']
    alliance_zones = db.execute(
        "SELECT COUNT(*) as count FROM zones WHERE owner = 'Alliance' AND status = 'captured'"
    ).fetchone()['count']
    horde_zones = db.execute(
        "SELECT COUNT(*) as count FROM zones WHERE owner = 'Horde' AND status = 'captured'"
    ).fetchone()['count']
    players_count = db.execute(
        'SELECT COUNT(*) as count FROM leaderboard_kills'
    ).fetchone()['count']

    reset_row = db.execute(
        "SELECT value FROM metadata WHERE key = 'last_reset_timestamp'"
    ).fetchone()
    last_reset = int(reset_row['value']) if reset_row else 0

    return jsonify({
        'zonesTracked': zones_count,
        'allianceZones': alliance_zones,
        'hordeZones': horde_zones,
        'playersTracked': players_count,
        'lastResetTimestamp': last_reset,
        'serverTime': utcnow_ts()
    })


# ============================================================================
# INITIALISATION
# ============================================================================

with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
