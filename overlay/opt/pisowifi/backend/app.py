#!/usr/bin/env python3
"""
PotsWorks PisoWiFi - Main Backend (Flask)
Handles: captive portal, session management, vouchers, admin panel.
"""

from flask import Flask, request, redirect, jsonify, send_file, session as flask_session
import os, sys, subprocess, hashlib, secrets, time, json, shutil, logging
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(__file__))
from db import get_db, init_db, get_config, set_config

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [BACKEND] %(message)s")

# In-memory rate limiting: {ip: {count: int, locked_until: float}}
_login_attempts = {}

# ─── HELPERS ────────────────────────────────────────────────────────────────

def get_client_mac(ip):
    """
    Resolve a client IP to its MAC address.
    Primary: parse /proc/net/arp (no subprocess, instant).
    Fallback: arp -n command with 500ms timeout.
    """
    try:
        with open('/proc/net/arp', 'r') as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip:
                    mac = parts[3]
                    if mac and mac != '00:00:00:00:00:00':
                        return mac.upper()
    except Exception:
        pass
    try:
        result = subprocess.check_output(
            ['arp', '-n', ip], timeout=0.5, stderr=subprocess.DEVNULL
        ).decode()
        for line in result.splitlines():
            if ip in line:
                parts = line.split()
                if len(parts) >= 3:
                    mac = parts[2]
                    if mac and mac != '<incomplete>':
                        return mac.upper()
    except Exception:
        pass
    return None


def allow_mac(mac):
    """Allow internet access for a MAC address via iptables."""
    os.system(f'iptables -t nat -I PREROUTING 1 -m mac --mac-source {mac} -j ACCEPT')
    os.system(f'iptables -I FORWARD 1 -m mac --mac-source {mac} -j ACCEPT')


def block_mac(mac):
    """Block internet access for a MAC address via iptables."""
    os.system(f'iptables -t nat -D PREROUTING -m mac --mac-source {mac} -j ACCEPT 2>/dev/null')
    os.system(f'iptables -D FORWARD -m mac --mac-source {mac} -j ACCEPT 2>/dev/null')


# ─── ADMIN MIDDLEWARE ───────────────────────────────────────────────────────

@app.before_request
def require_admin_for_api():
    """Protect all /admin/api/* routes — return 401 if not logged in."""
    if request.path.startswith('/admin/api/'):
        if not flask_session.get('admin'):
            return jsonify({'error': 'Unauthorized'}), 401


# ─── CAPTIVE PORTAL ROUTES ──────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def portal():
    """Serve the captive portal HTML page."""
    portal_path = os.path.join(os.path.dirname(__file__), '..', 'portal', 'portal.html')
    return send_file(os.path.abspath(portal_path))


@app.route('/generate_204')
def generate_204():
    """Android captive portal detection endpoint."""
    return '', 204


@app.route('/hotspot-detect.html')
def hotspot_detect():
    """iOS captive portal detection endpoint."""
    return 'Success', 200


@app.route('/connect', methods=['POST'])
def connect():
    """Register a pending client. Check trial mode and orphan credits."""
    ip = request.remote_addr
    mac = get_client_mac(ip)
    if not mac:
        return jsonify({'error': 'Cannot detect device'}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        # Check trial mode limit
        activation_status = get_config('activation_status', 'trial')
        if activation_status == 'trial':
            cur.execute('SELECT COUNT(*) FROM sessions WHERE active=1 AND remaining_seconds>0')
            active_count = cur.fetchone()[0]
            if active_count >= 2:
                return jsonify({'status': 'full', 'message': 'Puno na ang koneksyon. Subukan muli mamaya.'})
        # Register as pending client
        cur.execute('DELETE FROM pending_clients WHERE mac=?', (mac,))
        cur.execute('INSERT INTO pending_clients (mac, ip, connected_at) VALUES (?,?,?)',
                    (mac, ip, datetime.now().isoformat()))
        # Check orphan credits
        orphan = int(get_config('orphan_credits', '0') or 0)
        orphan_time = float(get_config('orphan_credits_time', '0') or 0)
        if orphan > 0 and (time.time() - orphan_time) < 300:
            rate = int(get_config('rate_piso_per_minute', '5') or 5)
            seconds = orphan * rate * 60
            cur.execute('SELECT id, remaining_seconds FROM sessions WHERE mac=? AND active=1', (mac,))
            sess = cur.fetchone()
            if sess:
                cur.execute('UPDATE sessions SET remaining_seconds=? WHERE id=?',
                            (sess['remaining_seconds'] + seconds, sess['id']))
            else:
                cur.execute('INSERT INTO sessions (mac, remaining_seconds, active, created_at) VALUES (?,?,1,?)',
                            (mac, seconds, datetime.now().isoformat()))
            set_config('orphan_credits', '0')
            set_config('orphan_credits_time', '0')
            allow_mac(mac)
        conn.commit()
        # Check if already has active session
        cur.execute('SELECT remaining_seconds FROM sessions WHERE mac=? AND active=1', (mac,))
        row = cur.fetchone()
        if row and row['remaining_seconds'] > 0:
            allow_mac(mac)
            return jsonify({'status': 'ok', 'minutes': row['remaining_seconds'] // 60})
        return jsonify({'status': 'waiting', 'message': 'Insert coin to connect'})
    finally:
        conn.close()


@app.route('/api/check_credit')
def check_credit():
    """Poll for coin credit. Returns credited status and remaining seconds."""
    ip = request.remote_addr
    mac = get_client_mac(ip)
    if not mac:
        return jsonify({'credited': False, 'minutes_added': 0, 'remaining_seconds': 0})
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT remaining_seconds FROM sessions WHERE mac=? AND active=1', (mac,)
        ).fetchone()
        if row and row['remaining_seconds'] > 0:
            return jsonify({'credited': True, 'minutes_added': row['remaining_seconds'] // 60,
                            'remaining_seconds': row['remaining_seconds']})
        return jsonify({'credited': False, 'minutes_added': 0, 'remaining_seconds': 0})
    finally:
        conn.close()


@app.route('/api/session_status')
def session_status():
    """Return current session status for the requesting client."""
    ip = request.remote_addr
    mac = get_client_mac(ip)
    if not mac:
        return jsonify({'active': False, 'remaining_seconds': 0, 'mac': ''})
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT remaining_seconds FROM sessions WHERE mac=? AND active=1', (mac,)
        ).fetchone()
        if row and row['remaining_seconds'] > 0:
            return jsonify({'active': True, 'remaining_seconds': row['remaining_seconds'], 'mac': mac})
        return jsonify({'active': False, 'remaining_seconds': 0, 'mac': mac})
    finally:
        conn.close()


@app.route('/api/redeem_voucher', methods=['POST'])
def redeem_voucher():
    """Redeem a voucher code. Accepts JSON {code: str}."""
    data = request.get_json(silent=True) or {}
    code = data.get('code', '').strip().upper()
    if not code:
        return jsonify({'error': 'Code required'}), 400
    ip = request.remote_addr
    mac = get_client_mac(ip)
    if not mac:
        return jsonify({'error': 'Cannot detect device'}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id, minutes, bandwidth_tier FROM vouchers WHERE code=? AND used=0', (code,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Invalid or used voucher'}), 400
        minutes = row['minutes']
        seconds = minutes * 60
        cur.execute('UPDATE vouchers SET used=1, used_by=?, used_at=? WHERE id=?',
                    (mac, datetime.now().isoformat(), row['id']))
        cur.execute('SELECT id, remaining_seconds FROM sessions WHERE mac=? AND active=1', (mac,))
        sess = cur.fetchone()
        if sess:
            cur.execute('UPDATE sessions SET remaining_seconds=? WHERE id=?',
                        (sess['remaining_seconds'] + seconds, sess['id']))
        else:
            cur.execute('INSERT INTO sessions (mac, remaining_seconds, active, created_at, bandwidth_tier) VALUES (?,?,1,?,?)',
                        (mac, seconds, datetime.now().isoformat(), row['bandwidth_tier']))
        conn.commit()
        allow_mac(mac)
        return jsonify({'status': 'ok', 'minutes': minutes})
    finally:
        conn.close()


@app.route('/voucher', methods=['POST'])
def use_voucher_compat():
    """Backward-compatible voucher endpoint (form-encoded)."""
    code = request.form.get('code', '').strip().upper()
    if not code:
        return jsonify({'error': 'Code required'}), 400
    ip = request.remote_addr
    mac = get_client_mac(ip)
    if not mac:
        return jsonify({'error': 'Cannot detect device'}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id, minutes FROM vouchers WHERE code=? AND used=0', (code,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Invalid or used voucher'}), 400
        minutes = row['minutes']
        cur.execute('UPDATE vouchers SET used=1, used_by=?, used_at=? WHERE id=?',
                    (mac, datetime.now().isoformat(), row['id']))
        cur.execute('SELECT id, remaining_seconds FROM sessions WHERE mac=? AND active=1', (mac,))
        sess = cur.fetchone()
        if sess:
            cur.execute('UPDATE sessions SET remaining_seconds=? WHERE id=?',
                        (sess['remaining_seconds'] + minutes * 60, sess['id']))
        else:
            cur.execute('INSERT INTO sessions (mac, remaining_seconds, active, created_at) VALUES (?,?,1,?)',
                        (mac, minutes * 60, datetime.now().isoformat()))
        conn.commit()
        allow_mac(mac)
        return jsonify({'status': 'ok', 'minutes': minutes})
    finally:
        conn.close()


@app.route('/api/config/rate')
def config_rate():
    """Public endpoint: return coin rate config."""
    rate = int(get_config('rate_piso_per_minute', '5') or 5)
    return jsonify({'rate': rate, 'unit': 'minutes per piso'})


@app.route('/api/config/banner')
def config_banner():
    """Public endpoint: return custom banner config."""
    banner_path = get_config('banner_path', '')
    if banner_path and os.path.exists(banner_path):
        return jsonify({'has_banner': True, 'banner_url': '/portal/assets/banner/custom.png'})
    return jsonify({'has_banner': False, 'banner_url': None})


@app.route('/status')
def status():
    """Backward-compatible status endpoint."""
    ip = request.remote_addr
    mac = get_client_mac(ip)
    if not mac:
        return jsonify({'connected': False})
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT remaining_seconds FROM sessions WHERE mac=? AND active=1', (mac,)
        ).fetchone()
        if row:
            return jsonify({'connected': True, 'remaining_seconds': row['remaining_seconds'], 'mac': mac})
        return jsonify({'connected': False})
    finally:
        conn.close()


# ─── ADMIN ROUTES ───────────────────────────────────────────────────────────

@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    """Admin login page with rate limiting (3 attempts = 5-minute lockout)."""
    if request.method == 'GET':
        if flask_session.get('admin'):
            return redirect('/admin/dashboard')
        return send_file(os.path.join(os.path.dirname(__file__), '..', 'admin', 'index.html'))
    # POST: validate password
    ip = request.remote_addr
    now = time.time()
    attempt = _login_attempts.get(ip, {'count': 0, 'locked_until': 0})
    if now < attempt['locked_until']:
        return jsonify({'error': 'Too many attempts. Try again later.'}), 429
    pw = request.form.get('password', '') or (request.get_json(silent=True) or {}).get('password', '')
    stored_hash = get_config('admin_password_hash', '')
    if hashlib.sha256(pw.encode()).hexdigest() == stored_hash:
        flask_session['admin'] = True
        _login_attempts.pop(ip, None)
        return jsonify({'status': 'ok'})
    # Failed attempt
    attempt['count'] = attempt.get('count', 0) + 1
    if attempt['count'] >= 3:
        attempt['locked_until'] = now + 300
        logging.warning(f'Admin login locked for IP {ip}')
    _login_attempts[ip] = attempt
    logging.warning(f'Failed admin login from {ip} (attempt {attempt[chr(99)+chr(111)+chr(117)+chr(110)+chr(116)]})')
    return jsonify({'error': 'Wrong password'}), 401


@app.route('/admin/dashboard')
def admin_dashboard():
    """Admin dashboard — serve the admin SPA."""
    if not flask_session.get('admin'):
        return redirect('/admin')
    return send_file(os.path.join(os.path.dirname(__file__), '..', 'admin', 'index.html'))


@app.route('/admin/logout')
def admin_logout():
    """Clear admin session."""
    flask_session.pop('admin', None)
    return redirect('/admin')


@app.route('/admin/api/stats')
def admin_stats():
    """Return real-time dashboard stats."""
    conn = get_db()
    try:
        active_sessions = conn.execute(
            'SELECT COUNT(*) FROM sessions WHERE active=1 AND remaining_seconds>0'
        ).fetchone()[0]
        today = date.today().isoformat()
        total_revenue = conn.execute(
            'SELECT COALESCE(SUM(amount_piso),0) FROM transactions WHERE date(created_at)=?', (today,)
        ).fetchone()[0]
        coin_count = conn.execute(
            'SELECT COUNT(*) FROM transactions WHERE type=? AND date(created_at)=?', ('coin', today)
        ).fetchone()[0]
    finally:
        conn.close()
    # Uptime from /proc/uptime
    try:
        uptime_seconds = float(open('/proc/uptime').read().split()[0])
    except Exception:
        uptime_seconds = 0
    # WAN status
    try:
        wan_out = subprocess.check_output(['ip', 'route', 'show', 'default'], timeout=2, stderr=subprocess.DEVNULL).decode()
        wan_status = 'online' if 'default' in wan_out else 'offline'
    except Exception:
        wan_status = 'unknown'
    activation_status = get_config('activation_status', 'trial')
    return jsonify({
        'active_sessions': active_sessions,
        'total_revenue_today': float(total_revenue),
        'coin_count_today': coin_count,
        'uptime_seconds': uptime_seconds,
        'wan_status': wan_status,
        'activation_status': activation_status,
    })


@app.route('/admin/api/sessions')
def admin_sessions():
    """Return list of active sessions with MAC, IP, remaining_seconds, bandwidth_tier."""
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT s.mac, s.remaining_seconds, s.bandwidth_tier, p.ip FROM sessions s'
            ' LEFT JOIN pending_clients p ON s.mac=p.mac'
            ' WHERE s.active=1 AND s.remaining_seconds>0'
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route('/admin/api/kick', methods=['POST'])
def admin_kick():
    """Kick a user: block MAC and deactivate session."""
    data = request.get_json(silent=True) or {}
    mac = data.get('mac', '').strip().upper()
    if not mac:
        return jsonify({'error': 'MAC required'}), 400
    block_mac(mac)
    conn = get_db()
    try:
        conn.execute('UPDATE sessions SET active=0 WHERE mac=?', (mac,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'ok'})


@app.route('/admin/api/add_time', methods=['POST'])
def admin_add_time():
    """Add minutes to a user's session."""
    data = request.get_json(silent=True) or {}
    mac = data.get('mac', '').strip().upper()
    minutes = int(data.get('minutes', 0))
    if not mac or minutes <= 0:
        return jsonify({'error': 'MAC and positive minutes required'}), 400
    conn = get_db()
    try:
        conn.execute(
            'UPDATE sessions SET remaining_seconds=remaining_seconds+? WHERE mac=? AND active=1',
            (minutes * 60, mac)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'ok'})


@app.route('/admin/api/vouchers')
def admin_vouchers():
    """Return all vouchers."""
    conn = get_db()
    try:
        rows = conn.execute('SELECT * FROM vouchers ORDER BY id DESC').fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route('/admin/api/vouchers/generate', methods=['POST'])
def admin_gen_vouchers():
    """Generate batch vouchers. Accepts {count, duration, prefix, bandwidth_tier}."""
    data = request.get_json(silent=True) or {}
    count = max(1, min(int(data.get('count', 1)), 500))
    duration = int(data.get('duration', 60))
    prefix = data.get('prefix', '').strip().upper()
    bandwidth_tier = data.get('bandwidth_tier', None)
    conn = get_db()
    try:
        codes = []
        for _ in range(count):
            parts = [secrets.token_hex(2).upper() for _ in range(3)]
            code = '-'.join(parts)
            if prefix:
                code = prefix + '-' + code
            conn.execute(
                'INSERT OR IGNORE INTO vouchers (code, minutes, created_at, bandwidth_tier, prefix) VALUES (?,?,?,?,?)',
                (code, duration, datetime.now().isoformat(), bandwidth_tier, prefix or None)
            )
            codes.append(code)
        conn.commit()
        return jsonify({'codes': codes})
    finally:
        conn.close()


@app.route('/admin/api/vouchers/<int:vid>', methods=['DELETE'])
def admin_del_voucher(vid):
    """Delete a voucher by ID."""
    conn = get_db()
    try:
        conn.execute('DELETE FROM vouchers WHERE id=?', (vid,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'ok'})


@app.route('/admin/api/vouchers/export')
def admin_export_vouchers():
    """Export all unused voucher codes as plain text file."""
    conn = get_db()
    try:
        rows = conn.execute('SELECT code FROM vouchers WHERE used=0 ORDER BY id').fetchall()
        text = chr(10).join(r['code'] for r in rows)
    finally:
        conn.close()
    from io import BytesIO
    buf = BytesIO(text.encode('utf-8'))
    return send_file(buf, mimetype='text/plain', as_attachment=True, download_name='vouchers.txt')


@app.route('/admin/api/config', methods=['GET', 'POST'])
def admin_config():
    """GET: return all config (excluding password hash). POST: update config keys."""
    ALLOWED_KEYS = {
        'ssid', 'wifi_password', 'wifi_channel', 'wifi_band',
        'rate_piso_per_minute', 'coin_pulse_timeout_ms', 'coin_debounce_ms',
        'gpio_pin', 'default_upload_kbps', 'default_download_kbps',
        'qos_enabled', 'starlink_monitor_enabled', 'coin_sound_enabled'
    }
    conn = get_db()
    try:
        if request.method == 'GET':
            rows = conn.execute('SELECT key, value FROM config WHERE key != ?', ('admin_password_hash',)).fetchall()
            return jsonify({r['key']: r['value'] for r in rows})
        data = request.get_json(silent=True) or {}
        for key, value in data.items():
            if key in ALLOWED_KEYS:
                conn.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', (key, str(value)))
        conn.commit()
        return jsonify({'status': 'ok'})
    finally:
        conn.close()


@app.route('/admin/api/wifi', methods=['POST'])
def admin_wifi():
    """Update WiFi settings and restart hostapd."""
    data = request.get_json(silent=True) or {}
    ssid = data.get('ssid', '').strip()
    password = data.get('password', '').strip()
    channel = str(data.get('channel', '6')).strip()
    band = str(data.get('band', '2.4')).strip()
    if ssid:
        set_config('ssid', ssid)
    if password:
        set_config('wifi_password', password)
    if channel:
        set_config('wifi_channel', channel)
    if band:
        set_config('wifi_band', band)
    # Update hostapd.conf
    hostapd_conf = '/etc/hostapd/hostapd.conf'
    try:
        if os.path.exists(hostapd_conf):
            with open(hostapd_conf, 'r') as f:
                lines = f.readlines()
            new_lines = []
            for line in lines:
                if line.startswith('ssid=') and ssid:
                    new_lines.append(f'ssid={ssid}' + chr(10))
                elif line.startswith('wpa_passphrase=') and password:
                    new_lines.append(f'wpa_passphrase={password}' + chr(10))
                elif line.startswith('channel=') and channel:
                    new_lines.append(f'channel={channel}' + chr(10))
                else:
                    new_lines.append(line)
            with open(hostapd_conf, 'w') as f:
                f.writelines(new_lines)
        subprocess.run(['systemctl', 'restart', 'hostapd'], timeout=10, check=False)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'status': 'ok'})


@app.route('/admin/api/password', methods=['POST'])
def admin_password():
    """Change admin password. Validates current, requires >= 8 chars for new."""
    data = request.get_json(silent=True) or {}
    current = data.get('current', '')
    new_pw = data.get('new_password', '')
    stored_hash = get_config('admin_password_hash', '')
    if hashlib.sha256(current.encode()).hexdigest() != stored_hash:
        return jsonify({'error': 'Current password incorrect'}), 401
    if len(new_pw) < 8:
        return jsonify({'error': 'New password must be at least 8 characters'}), 400
    set_config('admin_password_hash', hashlib.sha256(new_pw.encode()).hexdigest())
    flask_session.pop('admin', None)
    return jsonify({'status': 'ok'})


@app.route('/admin/api/logs')
def admin_logs():
    """Return last 100 lines from coin, session, and system logs."""
    def tail_file(path, n=100):
        try:
            with open(path, 'r', errors='replace') as f:
                lines = f.readlines()
                return ''.join(lines[-n:])
        except Exception:
            return ''
    def tail_journalctl(n=100):
        try:
            result = subprocess.check_output(
                ['journalctl', '-n', str(n), '--no-pager'], timeout=5, stderr=subprocess.DEVNULL
            ).decode(errors='replace')
            return result
        except Exception:
            return ''
    return jsonify({
        'coin': tail_file('/var/log/pisowifi-coin.log'),
        'session': tail_file('/var/log/pisowifi-session.log'),
        'system': tail_journalctl(),
    })


@app.route('/admin/api/bandwidth', methods=['GET', 'POST'])
def admin_bandwidth():
    """GET: list bandwidth rules. POST: add/update a rule."""
    conn = get_db()
    try:
        if request.method == 'GET':
            rows = conn.execute('SELECT * FROM bandwidth_rules ORDER BY id').fetchall()
            return jsonify([dict(r) for r in rows])
        data = request.get_json(silent=True) or {}
        mac = data.get('mac', '').strip().upper()
        upload = int(data.get('upload_kbps', 1024))
        download = int(data.get('download_kbps', 5120))
        notes = data.get('notes', '')
        if not mac:
            return jsonify({'error': 'MAC required'}), 400
        conn.execute(
            'INSERT OR REPLACE INTO bandwidth_rules (mac, upload_kbps, download_kbps, created_at, notes) VALUES (?,?,?,?,?)',
            (mac, upload, download, datetime.now().isoformat(), notes)
        )
        conn.commit()
        return jsonify({'status': 'ok'})
    finally:
        conn.close()


@app.route('/admin/api/bandwidth/<mac>', methods=['DELETE'])
def admin_del_bandwidth(mac):
    """Delete bandwidth rule for a MAC address."""
    conn = get_db()
    try:
        conn.execute('DELETE FROM bandwidth_rules WHERE mac=?', (mac.upper(),))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'ok'})


@app.route('/admin/api/throughput')
def admin_throughput():
    """Return last 60 throughput log entries."""
    conn = get_db()
    try:
        rows = conn.execute('SELECT * FROM throughput_log ORDER BY id DESC LIMIT 60').fetchall()
        return jsonify([dict(r) for r in reversed(rows)])
    finally:
        conn.close()


@app.route('/admin/api/activation_status')
def admin_activation_status():
    """Return activation status, masked key, and hardware ID."""
    try:
        from license import get_activation_status
        return jsonify(get_activation_status())
    except Exception:
        status = get_config('activation_status', 'trial')
        return jsonify({'status': status, 'activated_at': None, 'masked_key': None, 'hardware_id': None})


@app.route('/admin/api/activate', methods=['POST'])
def admin_activate():
    """Validate and activate the system with a license key."""
    data = request.get_json(silent=True) or {}
    key = data.get('key', '').strip().upper()
    if not key:
        return jsonify({'error': 'License key required'}), 400
    try:
        from license import activate
        result = activate(key)
        if result['success']:
            return jsonify({'status': 'ok', 'message': result['message']})
        return jsonify({'error': result['message']}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/rates', methods=['GET', 'POST'])
def admin_rates():
    """GET: return coin rates. POST: save new rates array."""
    if request.method == 'GET':
        raw = get_config('coin_rates', '[]')
        try:
            rates = json.loads(raw)
        except Exception:
            rates = []
        return jsonify(rates)
    data = request.get_json(silent=True)
    if not isinstance(data, list):
        return jsonify({'error': 'Expected array of {piso, minutes}'}), 400
    for item in data:
        if not isinstance(item, dict) or 'piso' not in item or 'minutes' not in item:
            return jsonify({'error': 'Each item must have piso and minutes'}), 400
    set_config('coin_rates', json.dumps(data))
    return jsonify({'status': 'ok'})


# ── File Upload Routes ───────────────────────────────────────────────────────

BANNER_DIR = '/opt/pisowifi/portal/assets/banner'
SOUND_DIR  = '/opt/pisowifi/portal/assets/sounds'
MAX_BANNER_BYTES = 500 * 1024   # 500 KB
MAX_SOUND_BYTES  = 100 * 1024   # 100 KB


@app.route('/admin/api/upload/banner', methods=['POST'])
def admin_upload_banner():
    """Upload a custom banner image (JPG/PNG, max 500KB)."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png'):
        return jsonify({'error': 'JPG o PNG lamang ang tinatanggap'}), 400
    data = f.read()
    if len(data) > MAX_BANNER_BYTES:
        return jsonify({'error': 'Maximum file size ay 500KB'}), 400
    os.makedirs(BANNER_DIR, exist_ok=True)
    dest = os.path.join(BANNER_DIR, 'custom.png')
    with open(dest, 'wb') as out:
        out.write(data)
    set_config('banner_path', dest)
    return jsonify({'status': 'ok', 'path': dest})


@app.route('/admin/api/reset/banner', methods=['POST'])
def admin_reset_banner():
    """Remove custom banner and revert to default logo."""
    banner_path = get_config('banner_path', '')
    if banner_path and os.path.exists(banner_path):
        try:
            os.remove(banner_path)
        except Exception:
            pass
    set_config('banner_path', '')
    return jsonify({'status': 'ok'})


@app.route('/admin/api/upload/sound', methods=['POST'])
def admin_upload_sound():
    """Upload a custom coin sound (MP3/WAV, max 100KB)."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.mp3', '.wav'):
        return jsonify({'error': 'MP3 o WAV lamang ang tinatanggap'}), 400
    data = f.read()
    if len(data) > MAX_SOUND_BYTES:
        return jsonify({'error': 'Maximum file size ay 100KB'}), 400
    os.makedirs(SOUND_DIR, exist_ok=True)
    dest = os.path.join(SOUND_DIR, 'custom_coin' + ext)
    with open(dest, 'wb') as out:
        out.write(data)
    set_config('custom_sound_path', dest)
    return jsonify({'status': 'ok', 'path': dest})


@app.route('/admin/api/backup')
def admin_backup():
    """Download the SQLite database as a backup file."""
    from db import DB_PATH as db_path
    if not os.path.exists(db_path):
        return jsonify({'error': 'Database not found'}), 404
    filename = f'pisowifi_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
    return send_file(db_path, as_attachment=True, download_name=filename)


# ── VLAN Configuration ───────────────────────────────────────────────────────

@app.route('/admin/api/vlan', methods=['POST'])
def admin_vlan_set():
    """Create a VLAN interface on the WAN port (built-in ethernet eth0)."""
    data = request.get_json(silent=True) or {}
    vlan_id = data.get('vlan_id', 0)
    try:
        vlan_id = int(vlan_id)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid VLAN ID'}), 400
    if not (1 <= vlan_id <= 4094):
        return jsonify({'error': 'VLAN ID must be between 1 and 4094'}), 400

    # WAN is always built-in ethernet (eth0) — USB-to-LAN is secondary LAN
    wan_if = 'eth0'
    for iface in ['eth0', 'enp0s3']:
        if os.path.exists(f'/sys/class/net/{iface}'):
            wan_if = iface
            break

    vlan_if = f'{wan_if}.{vlan_id}'
    try:
        # Load 8021q VLAN kernel module
        subprocess.run(['modprobe', '8021q'], timeout=5, check=False)
        subprocess.run(['ip', 'link', 'add', 'link', wan_if, 'name', vlan_if,
                        'type', 'vlan', 'id', str(vlan_id)], timeout=5, check=False)
        subprocess.run(['ip', 'link', 'set', wan_if, 'up'], timeout=5, check=False)
        subprocess.run(['ip', 'link', 'set', vlan_if, 'up'], timeout=5, check=False)
        subprocess.run(['dhclient', vlan_if], timeout=15, check=False)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    set_config('vlan_id', str(vlan_id))
    set_config('qos_wan_interface', vlan_if)
    return jsonify({'status': 'ok', 'vlan_interface': vlan_if,
                    'message': f'VLAN {vlan_id} created on {wan_if} → {vlan_if}'})


@app.route('/admin/api/vlan', methods=['DELETE'])
def admin_vlan_delete():
    """Remove the VLAN interface."""
    vlan_id = get_config('vlan_id', '0')
    if vlan_id and vlan_id != '0':
        wan_if = 'eth0'
        for iface in ['usb0', 'eth1', 'eth0']:
            if os.path.exists(f'/sys/class/net/{iface}'):
                wan_if = iface
                break
        vlan_if = f'{wan_if}.{vlan_id}'
        subprocess.run(['ip', 'link', 'del', vlan_if], timeout=5, check=False)
    set_config('vlan_id', '0')
    return jsonify({'status': 'ok'})


# ── Gaming QoS Routes ────────────────────────────────────────────────────────

@app.route('/admin/api/qos', methods=['GET'])
def admin_qos_get():
    """Return current QoS status and configuration."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
        from gaming_qos import get_qos_status
        return jsonify(get_qos_status())
    except Exception as e:
        return jsonify({'enabled': False, 'error': str(e)})


@app.route('/admin/api/qos', methods=['POST'])
def admin_qos_set():
    """Enable/disable QoS and update configuration."""
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled', False)
    wan_mbps = data.get('wan_bandwidth_mbps', 50)
    ports = data.get('gaming_ports', None)

    try:
        scripts_path = os.path.join(os.path.dirname(__file__), '..', 'scripts')
        sys.path.insert(0, scripts_path)
        from gaming_qos import enable_gaming_qos, disable_gaming_qos, set_gaming_ports

        if ports is not None:
            set_gaming_ports(ports)
        set_config('wan_bandwidth_mbps', str(wan_mbps))

        if enabled:
            enable_gaming_qos(wan_bandwidth_mbps=int(wan_mbps))
        else:
            disable_gaming_qos()

        return jsonify({'status': 'ok', 'enabled': enabled})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/qos/ports', methods=['POST'])
def admin_qos_add_port():
    """Add a gaming port to the QoS filter."""
    data = request.get_json(silent=True) or {}
    port = data.get('port')
    if not port:
        return jsonify({'error': 'Port required'}), 400
    try:
        scripts_path = os.path.join(os.path.dirname(__file__), '..', 'scripts')
        sys.path.insert(0, scripts_path)
        from gaming_qos import get_gaming_ports, set_gaming_ports, add_gaming_port_filter
        ports = get_gaming_ports()
        if int(port) not in ports:
            ports.append(int(port))
            set_gaming_ports(ports)
            if get_config('qos_enabled', '0') == '1':
                add_gaming_port_filter(int(port))
        return jsonify({'status': 'ok', 'ports': ports})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/qos/ports/<int:port>', methods=['DELETE'])
def admin_qos_del_port(port):
    """Remove a gaming port from the QoS filter."""
    try:
        scripts_path = os.path.join(os.path.dirname(__file__), '..', 'scripts')
        sys.path.insert(0, scripts_path)
        from gaming_qos import remove_gaming_port_filter, get_gaming_ports
        remove_gaming_port_filter(port)
        return jsonify({'status': 'ok', 'ports': get_gaming_ports()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Starlink Monitor Routes ──────────────────────────────────────────────────

@app.route('/admin/api/starlink', methods=['GET'])
def admin_starlink_get():
    """Return Starlink monitor status and recent throughput."""
    enabled = get_config('starlink_monitor_enabled', '0') == '1'
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT timestamp, mbps, is_throttled FROM throughput_log '
            'ORDER BY id DESC LIMIT 60'
        ).fetchall()
        history = [dict(r) for r in reversed(rows)]
    finally:
        conn.close()

    # Calculate stats
    mbps_values = [r['mbps'] for r in history if r['mbps'] > 0]
    avg_mbps = round(sum(mbps_values) / len(mbps_values), 2) if mbps_values else 0
    throttle_events = sum(1 for r in history if r['is_throttled'])
    current_mbps = history[-1]['mbps'] if history else 0

    return jsonify({
        'enabled': enabled,
        'current_mbps': current_mbps,
        'avg_mbps_1h': avg_mbps,
        'throttle_events_1h': throttle_events,
        'history': history,
    })


@app.route('/admin/api/starlink', methods=['POST'])
def admin_starlink_set():
    """Enable or disable the Starlink monitor."""
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled', False)
    set_config('starlink_monitor_enabled', '1' if enabled else '0')

    # Start/stop the service
    action = 'start' if enabled else 'stop'
    try:
        subprocess.run(
            ['systemctl', action, 'pisowifi-starlink'],
            timeout=10, check=False
        )
    except Exception:
        pass

    return jsonify({'status': 'ok', 'enabled': enabled})


# ── Network Status Route ─────────────────────────────────────────────────────

@app.route('/admin/api/network')
def admin_network():
    """Return detected network setup and interface status."""
    detected_setup = get_config('detected_setup', 'unknown')
    detected_wan   = get_config('detected_wan_if', 'eth0')
    vlan_id        = get_config('vlan_id', '0')

    # Check interface states
    def iface_info(name):
        path = f'/sys/class/net/{name}'
        if not os.path.exists(path):
            return {'name': name, 'exists': False, 'carrier': False, 'ip': None}
        try:
            carrier = open(f'{path}/carrier').read().strip() == '1'
        except Exception:
            carrier = False
        # Get IP address
        ip = None
        try:
            out = subprocess.check_output(
                ['ip', '-4', 'addr', 'show', name],
                timeout=2, stderr=subprocess.DEVNULL
            ).decode()
            for line in out.splitlines():
                if 'inet ' in line:
                    ip = line.strip().split()[1].split('/')[0]
                    break
        except Exception:
            pass
        return {'name': name, 'exists': True, 'carrier': carrier, 'ip': ip}

    interfaces = {
        'eth0':  iface_info('eth0'),
        'wlan0': iface_info('wlan0'),
        'usb0':  iface_info('usb0'),
    }
    if vlan_id and vlan_id != '0':
        interfaces[f'eth0.{vlan_id}'] = iface_info(f'eth0.{vlan_id}')

    setup_descriptions = {
        'vlan':    f'VLAN Mode — Modem LAN1→eth0→eth0.{vlan_id} (WAN), wlan0 = Hotspot',
        'usb_ap':  'USB-to-LAN AP Mode — eth0 = WAN, usb0 = AP/LAN, wlan0 = Hotspot',
        'direct':  'Direct Mode — eth0 = WAN, wlan0 = Hotspot',
        'unknown': 'Hindi pa na-detect ang network setup',
    }

    return jsonify({
        'detected_setup':      detected_setup,
        'detected_wan':        detected_wan,
        'vlan_id':             vlan_id,
        'description':         setup_descriptions.get(detected_setup, detected_setup),
        'interfaces':          interfaces,
    })


@app.route('/admin/api/network/redetect', methods=['POST'])
def admin_network_redetect():
    """Re-run network detection script."""
    try:
        result = subprocess.run(
            ['/bin/bash', '/opt/pisowifi/scripts/detect_wan.sh'],
            timeout=30, capture_output=True, text=True
        )
        return jsonify({
            'status': 'ok',
            'output': result.stdout[-500:] if result.stdout else '',
            'detected_setup': get_config('detected_setup', 'unknown'),
            'detected_wan':   get_config('detected_wan_if', 'eth0'),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/admin/api/diagnostics')
def admin_diagnostics():
    """Return system diagnostics: CPU temp, RAM, disk, WAN, services."""
    # CPU temperature
    try:
        cpu_temp = int(open('/sys/class/thermal/thermal_zone0/temp').read().strip()) / 1000.0
    except Exception:
        cpu_temp = 0
    # RAM usage
    try:
        meminfo = {}
        for line in open('/proc/meminfo').readlines():
            parts = line.split()
            if len(parts) >= 2:
                meminfo[parts[0].rstrip(':')] = int(parts[1])
        total = meminfo.get('MemTotal', 1)
        available = meminfo.get('MemAvailable', total)
        ram_usage_pct = round((total - available) / total * 100, 1)
    except Exception:
        ram_usage_pct = 0
    # Disk usage
    try:
        du = shutil.disk_usage('/opt/pisowifi')
        disk_usage_pct = round(du.used / du.total * 100, 1)
    except Exception:
        try:
            du = shutil.disk_usage('.')
            disk_usage_pct = round(du.used / du.total * 100, 1)
        except Exception:
            disk_usage_pct = 0
    # Uptime
    try:
        uptime_seconds = float(open('/proc/uptime').read().split()[0])
    except Exception:
        uptime_seconds = 0
    # WAN status
    try:
        wan_out = subprocess.check_output(['ip', 'route', 'show', 'default'], timeout=2, stderr=subprocess.DEVNULL).decode()
        wan_status = 'online' if 'default' in wan_out else 'offline'
    except Exception:
        wan_status = 'unknown'
    # Service status
    services = []
    for svc in ['pisowifi-backend', 'pisowifi-coin', 'pisowifi-session']:
        try:
            out = subprocess.check_output(['systemctl', 'is-active', svc], timeout=3, stderr=subprocess.DEVNULL).decode().strip()
            services.append({'name': svc, 'status': out})
        except Exception:
            services.append({'name': svc, 'status': 'unknown'})
    return jsonify({
        'cpu_temp': cpu_temp,
        'ram_usage_pct': ram_usage_pct,
        'disk_usage_pct': disk_usage_pct,
        'uptime_seconds': uptime_seconds,
        'wan_status': wan_status,
        'services': services,
    })


# ─── MAIN ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=80, debug=False)
