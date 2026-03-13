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
    with app.app_context():
        db = get_db()
        db.executescript('''
            -- Table des zones (état actuel de chaque zone)
            CREATE TABLE IF NOT EXISTS zones (
                zone_id TEXT PRIMARY KEY,
                owner TEXT,
                status TEXT DEFAULT 'idle',
                kills_current INTEGER DEFAULT 0,
                hold_time_elapsed INTEGER DEFAULT 0,
                captured_time INTEGER DEFAULT 0,
                updated_at INTEGER DEFAULT 0
            );
            
            -- Table du leaderboard des kills
            CREATE TABLE IF NOT EXISTS leaderboard_kills (
                player_name TEXT PRIMARY KEY,
                kills INTEGER DEFAULT 0,
                class TEXT,
                faction TEXT,
                updated_at INTEGER DEFAULT 0
            );
            
            -- Table du leaderboard des captures
            CREATE TABLE IF NOT EXISTS leaderboard_captures (
                player_name TEXT PRIMARY KEY,
                captures TEXT DEFAULT '[]',
                faction TEXT,
                updated_at INTEGER DEFAULT 0
            );
            
            -- Table des métadonnées (reset timestamp, etc.)
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        ''')
        db.commit()


# ============================================================================
# ENDPOINTS API
# ============================================================================

@app.route('/', methods=['GET'])
def index():
    """Page d'accueil / health check."""
    return jsonify({
        'name': 'Overlord Sync Server',
        'version': '1.0.0',
        'status': 'online',
        'endpoints': ['/state', '/leaderboard', '/capture', '/kill', '/reset']
    })


@app.route('/state', methods=['GET'])
def get_state():
    """
    Récupère l'état complet des zones.
    Utilisé par le companion au login pour synchroniser les données.
    """
    db = get_db()
    
    # Récupérer toutes les zones
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
    
    # Récupérer le timestamp du dernier reset
    reset_row = db.execute("SELECT value FROM metadata WHERE key = 'last_reset_timestamp'").fetchone()
    last_reset = int(reset_row['value']) if reset_row else 0
    
    return jsonify({
        'zones': zones_dict,
        'lastResetTimestamp': last_reset,
        'serverTime': int(datetime.utcnow().timestamp())
    })


@app.route('/state', methods=['POST'])
def post_state():
    """
    Met à jour l'état des zones.
    Résolution de conflits : le timestamp le plus récent gagne.
    """
    data = request.get_json()
    if not data or 'zones' not in data:
        return jsonify({'error': 'Missing zones data'}), 400
    
    db = get_db()
    updated_zones = []
    
    for zone_id, zone_data in data['zones'].items():
        incoming_ts = zone_data.get('updatedAt', 0)
        
        # Vérifier si on a déjà cette zone
        existing = db.execute(
            'SELECT updated_at FROM zones WHERE zone_id = ?', 
            (zone_id,)
        ).fetchone()
        
        # Résolution de conflit : timestamp le plus récent gagne
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
                incoming_ts
            ))
            updated_zones.append(zone_id)
    
    # Mettre à jour le reset timestamp si fourni
    if 'lastResetTimestamp' in data:
        db.execute('''
            INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_reset_timestamp', ?)
        ''', (str(data['lastResetTimestamp']),))
    
    db.commit()
    
    return jsonify({
        'success': True,
        'updatedZones': updated_zones,
        'serverTime': int(datetime.utcnow().timestamp())
    })


@app.route('/leaderboard', methods=['GET'])
def get_leaderboard():
    """Récupère le leaderboard complet (kills + captures)."""
    db = get_db()
    
    # Kills
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
    
    # Captures
    captures_rows = db.execute(
        'SELECT * FROM leaderboard_captures ORDER BY updated_at DESC LIMIT 100'
    ).fetchall()
    captures = {}
    for row in captures_rows:
        captures[row['player_name']] = {
            'captures': json.loads(row['captures']),
            'faction': row['faction']
        }
    
    # Player info (fusion des deux)
    player_info = {}
    for row in kills_rows:
        player_info[row['player_name']] = {
            'class': row['class'],
            'faction': row['faction']
        }
    
    return jsonify({
        'kills': kills,
        'captures': captures,
        'playerInfo': player_info
    })


@app.route('/leaderboard', methods=['POST'])
def post_leaderboard():
    """
    Met à jour le leaderboard.
    Résolution de conflits : on garde le score le plus élevé pour les kills.
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Missing data'}), 400
    
    db = get_db()
    now = int(datetime.utcnow().timestamp())
    
    # Mise à jour des kills
    if 'kills' in data:
        for player_name, player_data in data['kills'].items():
            incoming_kills = player_data.get('kills', 0)
            
            existing = db.execute(
                'SELECT kills FROM leaderboard_kills WHERE player_name = ?',
                (player_name,)
            ).fetchone()
            
            # On garde le score le plus élevé
            if existing is None or incoming_kills > existing['kills']:
                db.execute('''
                    INSERT OR REPLACE INTO leaderboard_kills 
                    (player_name, kills, class, faction, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    player_name,
                    incoming_kills,
                    player_data.get('class'),
                    player_data.get('faction'),
                    now
                ))
    
    # Mise à jour des captures
    if 'captures' in data:
        for player_name, player_data in data['captures'].items():
            captures_list = player_data.get('captures', [])
            
            existing = db.execute(
                'SELECT captures FROM leaderboard_captures WHERE player_name = ?',
                (player_name,)
            ).fetchone()
            
            # Fusionner les captures (union des zones capturées)
            if existing:
                existing_captures = set(json.loads(existing['captures']))
                new_captures = set(captures_list)
                merged = list(existing_captures | new_captures)
            else:
                merged = captures_list
            
            db.execute('''
                INSERT OR REPLACE INTO leaderboard_captures 
                (player_name, captures, faction, updated_at)
                VALUES (?, ?, ?, ?)
            ''', (
                player_name,
                json.dumps(merged),
                player_data.get('faction'),
                now
            ))
    
    db.commit()
    
    return jsonify({
        'success': True,
        'serverTime': now
    })


@app.route('/capture', methods=['POST'])
def post_capture():
    """Enregistre une capture de zone."""
    data = request.get_json()
    if not data or 'zoneId' not in data or 'playerName' not in data:
        return jsonify({'error': 'Missing zoneId or playerName'}), 400
    
    db = get_db()
    now = int(datetime.utcnow().timestamp())
    zone_id = data['zoneId']
    player_name = data['playerName']
    faction = data.get('faction')
    
    # Mettre à jour la zone
    db.execute('''
        INSERT OR REPLACE INTO zones 
        (zone_id, owner, status, kills_current, hold_time_elapsed, captured_time, updated_at)
        VALUES (?, ?, 'captured', 0, 0, ?, ?)
    ''', (zone_id, faction, now, now))
    
    # Ajouter au leaderboard captures
    existing = db.execute(
        'SELECT captures FROM leaderboard_captures WHERE player_name = ?',
        (player_name,)
    ).fetchone()
    
    if existing:
        captures = set(json.loads(existing['captures']))
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
    
    return jsonify({
        'success': True,
        'zoneId': zone_id,
        'playerName': player_name,
        'serverTime': now
    })


@app.route('/kill', methods=['POST'])
def post_kill():
    """Enregistre un kill."""
    data = request.get_json()
    if not data or 'playerName' not in data:
        return jsonify({'error': 'Missing playerName'}), 400
    
    db = get_db()
    now = int(datetime.utcnow().timestamp())
    player_name = data['playerName']
    zone_id = data.get('zoneId')
    
    # Incrémenter les kills du joueur
    existing = db.execute(
        'SELECT kills FROM leaderboard_kills WHERE player_name = ?',
        (player_name,)
    ).fetchone()
    
    new_kills = (existing['kills'] + 1) if existing else 1
    
    db.execute('''
        INSERT OR REPLACE INTO leaderboard_kills 
        (player_name, kills, class, faction, updated_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (
        player_name,
        new_kills,
        data.get('class'),
        data.get('faction'),
        now
    ))
    
    # Incrémenter les kills de la zone si spécifiée
    if zone_id:
        zone = db.execute(
            'SELECT kills_current, updated_at FROM zones WHERE zone_id = ?',
            (zone_id,)
        ).fetchone()
        
        if zone:
            db.execute('''
                UPDATE zones SET kills_current = ?, updated_at = ? WHERE zone_id = ?
            ''', (zone['kills_current'] + 1, now, zone_id))
    
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
    Remet toutes les zones à zéro et archive le leaderboard.
    """
    data = request.get_json() or {}
    
    # Vérification optionnelle d'un secret pour éviter les resets accidentels
    secret = os.environ.get('RESET_SECRET')
    if secret and data.get('secret') != secret:
        return jsonify({'error': 'Invalid secret'}), 403
    
    db = get_db()
    now = int(datetime.utcnow().timestamp())
    
    # Supprimer toutes les zones
    db.execute('DELETE FROM zones')
    
    # Supprimer le leaderboard
    db.execute('DELETE FROM leaderboard_kills')
    db.execute('DELETE FROM leaderboard_captures')
    
    # Mettre à jour le timestamp de reset
    db.execute('''
        INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_reset_timestamp', ?)
    ''', (str(now),))
    
    db.commit()
    
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
        "SELECT COUNT(*) as count FROM zones WHERE owner = 'Alliance'"
    ).fetchone()['count']
    horde_zones = db.execute(
        "SELECT COUNT(*) as count FROM zones WHERE owner = 'Horde'"
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
        'serverTime': int(datetime.utcnow().timestamp())
    })


# ============================================================================
# INITIALISATION
# ============================================================================

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
