"""Additive SQLite migrations; schema changes stay in one transaction."""

SCHEMA_VERSION = 3


def migrate_database(connection):
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('CREATE TABLE IF NOT EXISTS readings (id INTEGER PRIMARY KEY,seq INTEGER NOT NULL,temperature_c REAL NOT NULL,received_at REAL NOT NULL,humidity_pct REAL)')
        columns={row[1] for row in connection.execute('PRAGMA table_info(readings)')}
        for name,kind in (('humidity_pct','REAL'),('device_id',"TEXT DEFAULT 'cabinet-01'"),('boot_id','TEXT')):
            if name not in columns:
                connection.execute('ALTER TABLE readings ADD COLUMN '+name+' '+kind)
        connection.execute('CREATE INDEX IF NOT EXISTS readings_received_at ON readings(received_at)')
        connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS readings_identity ON readings(device_id,boot_id,seq) WHERE boot_id IS NOT NULL')
        connection.execute('CREATE TABLE IF NOT EXISTS device_sessions (device_id TEXT NOT NULL,boot_id TEXT NOT NULL,first_seen_at REAL NOT NULL,last_seen_at REAL NOT NULL,max_seq INTEGER NOT NULL,closed_at REAL,PRIMARY KEY(device_id,boot_id))')
        connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_current_session ON device_sessions(device_id) WHERE closed_at IS NULL')
        connection.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,event_type TEXT NOT NULL,device_id TEXT NOT NULL,rule_id TEXT,started_at REAL NOT NULL,detected_at REAL NOT NULL,ended_at REAL,end_reason TEXT,evidence_json TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS event_changes (change_id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL,transition TEXT NOT NULL,occurred_at REAL NOT NULL,event_json TEXT NOT NULL)')
        connection.execute('CREATE INDEX IF NOT EXISTS events_ended_at ON events(ended_at)')
        connection.execute('CREATE TABLE IF NOT EXISTS schema_meta (name TEXT PRIMARY KEY,value TEXT NOT NULL)')
        existing=connection.execute("SELECT value FROM schema_meta WHERE name='schema_version'").fetchone()
        if existing and int(existing[0])>SCHEMA_VERSION:
            raise ValueError('Database schema is newer than this application')
        connection.execute("INSERT INTO schema_meta(name,value) VALUES ('schema_version',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",(str(SCHEMA_VERSION),))
        connection.execute("INSERT INTO schema_meta(name,value) VALUES ('minimum_reader_version','0.3.0') ON CONFLICT(name) DO NOTHING")
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        raise
