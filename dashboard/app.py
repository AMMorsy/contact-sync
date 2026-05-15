"""Contact Sync Dashboard — Flask app (Phase 2 — live data)."""
import json
import secrets
from datetime import timedelta
from pathlib import Path
from functools import wraps

from flask import (
    Flask, Blueprint, request, session, redirect, url_for,
    render_template, jsonify
)

from checks.health import get_overall_health

CONFIG_PATH = Path('/root/contact-sync-dashboard/config.json')
with CONFIG_PATH.open() as f:
    CONFIG = json.load(f)

URL_PREFIX = CONFIG.get('url_prefix', '/dashboard')

app = Flask(__name__)
app.secret_key = CONFIG['secret_key']
app.permanent_session_lifetime = timedelta(hours=CONFIG.get('session_hours', 24))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_PATH=URL_PREFIX,
    APPLICATION_ROOT=URL_PREFIX,
)

bp = Blueprint('dashboard', __name__, url_prefix=URL_PREFIX)


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('authed'):
            return redirect(url_for('dashboard.login'))
        return fn(*args, **kwargs)
    return wrapper


@bp.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        provided = request.form.get('password', '')
        if secrets.compare_digest(provided, CONFIG['password']):
            session.permanent = True
            session['authed']  = True
            return redirect(url_for('dashboard.home'))
        error = 'Incorrect password.'
    return render_template('login.html', error=error)


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('dashboard.login'))


@bp.route('/')
@require_login
def home():
    try:
        health = get_overall_health()
    except Exception as e:
        health = {
            'overall_status':  'red',
            'overall_label':   'Dashboard error',
            'overall_summary': f'Could not load system health: {e}',
            'components':      [],
            'alerts':          [{'severity': 'red', 'area': 'dashboard',
                                  'message': f'Error: {e}', 'action': 'Check /root/contact-sync-dashboard/logs/'}],
            'raw': {},
        }
    return render_template(
        'dashboard.html',
        health=health,
        refresh=CONFIG.get('refresh_seconds', 30),
    )


@bp.route('/api/status.json')
@require_login
def status_json():
    return jsonify(get_overall_health())


@bp.route('/healthz')
def healthz():
    return 'ok', 200


app.register_blueprint(bp)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8090, debug=False)
