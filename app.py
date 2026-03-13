# -*- coding: utf-8 -*-
"""
Overlord Sync Server
Serveur API REST pour synchroniser les données de l'addon Overlord entre tous les joueurs.
Déployé sur Railway.
"""

import os
import sqlite3
import json
from datetime import datetime
from flask import Flask, request, jsonify, g
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DATABASE = os.environ.get('DATABASE_PATH', 'overlord.db')


def get_db():
    """Connexion à la base de données SQLite."""
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    """Ferme la connexion à la base de données."""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def init_db():
    """Initialise la base de données avec les tables nécessaires."""
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


@app.route('/', methods=['GET'])
def index():
    return jsonify({
        'name': 'Overlord Sync Server',
        'version': '1.0.0',
        'status': 'online',
        'endpoints': ['/state', '/leaderboard', '/capture', '/kill', '/reset']
    })


@app.route('/state', methods=['GET'])
def get_state():
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
    reset_row = db.execute("SELECT value FROM metadata WHERE key = 'last_reset_timestamp'").fetchone()
    last_reset = int(reset_row['value']) if reset_row else 0
    return jsonify({
        'zones': zones_dict,
        'lastResetTimestamp': last_reset,
        'serverTime': int(datetime.utcnow().timestamp())
    })


@app.route('/state', methods=['POST'])
def post_state():
    data = request.get_json()
    if not data or 'zones' not in data:
        return jsonify({'error': 'Missing zones data'}), 400
    db = get_db()
    updated_zones = []
    for zone_id, zone_data in data['zones'].items():
        incoming_ts = zone_data.get('updatedAt', 0)
        existing = db.execute('SELECT updated_at FROM zones WHERE zone_id = ?', (zone_id,)).fetchone()
        if existing is None or incoming_ts > existing['updated_at']:
            db.execute('''
                INSERT OR REPLACE INTO zones 
                (zone_id, owner, status, kills_current, hold_time_elapsed, captured_time, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (zone_id, zone_data.get('owner'), zone_data.get('status', 'idle'),
                  zone_data.get('killsCurrent', 0), zone_data.get('holdTimeElapsed', 0),
                  zone_data.get('capturedTime', 0), incoming_ts))
            updated_zones.append(zone_id)
    if 'lastResetTimestamp' in data:
        db.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_reset_timestamp', ?)",
                   (str(data['lastResetTimestamp']),))
    db.commit()
    return jsonify({'success': True, 'updatedZones': updated_zones, 'serverTime': int(datetime.utcnow().timestamp())})


@app.route('/leaderboard', methods=['GET'])
def get_leaderboard():
    db = get_db()
    kills_rows = db.execute('SELECT * FROM leaderboard_kills ORDER BY kills DESC LIMIT 100').fetchall()
    kills = {row['player_name']: {'kills': row['kills'], 'class': row['class'], 'faction': row['faction']} for row in kills_rows}
    captures_rows = db.execute('SELECT * FROM leaderboard_captures ORDER BY updated_at DESC LIMIT 100').fetchall()
    captures = {row['player_name']: {'captures': json.loads(row['captures']), 'faction': row['faction']} for row in captures_rows}
    player_info = {row['player_name']: {'class': row['class'], 'faction': row['faction']} for row in kills_rows}
    return jsonify({'kills': kills, 'captures': captures, 'playerInfo': player_info})


@app.route('/leaderboard', methods=['POST'])
def post_leaderboard():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Missing data'}), 400
    db = get_db()
    now = int(datetime.utcnow().timestamp())
    if 'kills' in data:
        for player_name, player_data in data['kills'].items():
            incoming_kills = player_data.get('kills', 0)
            existing = db.execute('SELECT kills FROM leaderboard_kills WHERE player_name = ?', (player_name,)).fetchone()
            if existing is None or incoming_kills > existing['kills']:
                db.execute('INSERT OR REPLACE INTO leaderboard_kills (player_name, kills, class, faction, updated_at) VALUES (?, ?, ?, ?, ?)',
                           (player_name, incoming_kills, player_data.get('class'), player_data.get('faction'), now))
    if 'captures' in data:
        for player_name, player_data in data['captures'].items():
            captures_list = player_data.get('captures', [])
            existing = db.execute('SELECT captures FROM leaderboard_captures WHERE player_name = ?', (player_name,)).fetchone()
            merged = list(set(json.loads(existing['captures'])) | set(captures_list)) if existing else captures_list
            db.execute('INSERT OR REPLACE INTO leaderboard_captures (player_name, captures, faction, updated_at) VALUES (?, ?, ?, ?)',
                       (player_name, json.dumps(merged), player_data.get('faction'), now))
    db.commit()
    return jsonify({'success': True, 'serverTime': now})


@app.route('/capture', methods=['POST'])
def post_capture():
    data = request.get_json()
    if not data or 'zoneId' not in data or 'playerName' not in data:
        return jsonify({'error': 'Missing zoneId or playerName'}), 400
    db = get_db()
    now = int(datetime.utcnow().timestamp())
    zone_id, player_name, faction = data['zoneId'], data['playerName'], data.get('faction')
    db.execute('INSERT OR REPLACE INTO zones (zone_id, owner, status, kills_current, hold_time_elapsed, captured_time, updated_at) VALUES (?, ?, ?, 0, 0, ?, ?)',
               (zone_id, faction, 'captured', now, now))
    existing = db.execute('SELECT captures FROM leaderboard_captures WHERE player_name = ?', (player_name,)).fetchone()
    captures = list(set(json.loads(existing['captures'])) | {zone_id}) if existing else [zone_id]
    db.execute('INSERT OR REPLACE INTO leaderboard_captures (player_name, captures, faction, updated_at) VALUES (?, ?, ?, ?)',
               (player_name, json.dumps(captures), faction, now))
    db.commit()
    return jsonify({'success': True, 'zoneId': zone_id, 'playerName': player_name, 'serverTime': now})


@app.route('/kill', methods=['POST'])
def post_kill():
    data = request.get_json()
    if not data or 'playerName' not in data:
        return jsonify({'error': 'Missing playerName'}), 400
    db = get_db()
    now = int(datetime.utcnow().timestamp())
    player_name, zone_id = data['playerName'], data.get('zoneId')
    existing = db.execute('SELECT kills FROM leaderboard_kills WHERE player_name = ?', (player_name,)).fetchone()
    new_kills = (existing['kills'] + 1) if existing else 1
    db.execute('INSERT OR REPLACE INTO leaderboard_kills (player_name, kills, class, faction, updated_at) VALUES (?, ?, ?, ?, ?)',
               (player_name, new_kills, data.get('class'), data.get('faction'), now))
    if zone_id:
        zone = db.execute('SELECT kills_current FROM zones WHERE zone_id = ?', (zone_id,)).fetchone()
        if zone:
            db.execute('UPDATE zones SET kills_current = ?, updated_at = ? WHERE zone_id = ?', (zone['kills_current'] + 1, now, zone_id))
    db.commit()
    return jsonify({'success': True, 'playerName': player_name, 'totalKills': new_kills, 'serverTime': now})


@app.route('/reset', methods=['POST'])
def reset_campaign():
    data = request.get_json() or {}
    secret = os.environ.get('RESET_SECRET')
    if secret and data.get('secret') != secret:
        return jsonify({'error': 'Invalid secret'}), 403
    db = get_db()
    now = int(datetime.utcnow().timestamp())
    db.execute('DELETE FROM zones')
    db.execute('DELETE FROM leaderboard_kills')
    db.execute('DELETE FROM leaderboard_captures')
    db.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_reset_timestamp', ?)", (str(now),))
    db.commit()
    return jsonify({'success': True, 'resetTimestamp': now, 'serverTime': now})


@app.route('/stats', methods=['GET'])
def get_stats():
    db = get_db()
    zones_count = db.execute('SELECT COUNT(*) as count FROM zones').fetchone()['count']
    alliance_zones = db.execute("SELECT COUNT(*) as count FROM zones WHERE owner = 'Alliance'").fetchone()['count']
    horde_zones = db.execute("SELECT COUNT(*) as count FROM zones WHERE owner = 'Horde'").fetchone()['count']
    players_count = db.execute('SELECT COUNT(*) as count FROM leaderboard_kills').fetchone()['count']
    reset_row = db.execute("SELECT value FROM metadata WHERE key = 'last_reset_timestamp'").fetchone()
    last_reset = int(reset_row['value']) if reset_row else 0
    return jsonify({'zonesTracked': zones_count, 'allianceZones': alliance_zones, 'hordeZones': horde_zones,
                    'playersTracked': players_count, 'lastResetTimestamp': last_reset, 'serverTime': int(datetime.utcnow().timestamp())})


# Initialiser la DB au chargement du module (pour gunicorn)
with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
