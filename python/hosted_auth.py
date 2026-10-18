"""Small persistent admission throttle for the invitation-only beta."""

import hashlib
import hmac
import time


def allow_login_attempt(config, peer, *, connect=None, clock=time.time):
    if connect is None:
        import psycopg
        connect = psycopg.connect
    key = hmac.new(config.session_secret.encode(), str(peer).encode(), hashlib.sha256).hexdigest()
    window = int(clock() // 300)
    with connect(config.database_url, sslmode="require", connect_timeout=10) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS hax_login_attempts (
            bucket TEXT PRIMARY KEY, window_id BIGINT NOT NULL, attempts INTEGER NOT NULL)""")
        row = db.execute("""INSERT INTO hax_login_attempts VALUES (%s,%s,1)
            ON CONFLICT(bucket) DO UPDATE SET
            attempts=CASE WHEN hax_login_attempts.window_id=EXCLUDED.window_id
                          THEN hax_login_attempts.attempts+1 ELSE 1 END,
            window_id=EXCLUDED.window_id RETURNING attempts""", (key, window)).fetchone()
        return row[0] <= 10
