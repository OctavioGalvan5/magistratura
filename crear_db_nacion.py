"""
Crea la database 'nacion' en el mismo servidor Postgres y le arma:
- schema avales_2026
- tablas personas, fotos, fotos_personas
- índices, trigger updated_at, trigger auto dni_recibido
- constraint UNIQUE en dni

Es idempotente — se puede correr varias veces sin romper nada.
"""
import os, sys, re
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
from dotenv import load_dotenv
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

load_dotenv()

DB_NEW = "nacion"
SCHEMA = "avales_2026"

# Parseamos la connection string actual para reusar host/user/password.
def parse_url(url: str) -> dict:
    m = re.match(
        r"postgresql(?:\+\w+)?://(?P<user>[^:]+):(?P<pw>[^@]+)@(?P<host>[^:/]+)(?::(?P<port>\d+))?/(?P<db>[^?]+)",
        url,
    )
    if not m: raise ValueError(f"connection string no reconocida: {url!r}")
    return m.groupdict()

info = parse_url(os.environ["DB_CONNECTION_STRING"])
print(f"Servidor: {info['host']}:{info.get('port') or 5432}, usuario: {info['user']}")

# 1) Conexion en autocommit al DB original para hacer CREATE DATABASE
conn = psycopg2.connect(
    host=info["host"], port=info.get("port") or 5432,
    user=info["user"], password=info["pw"], dbname=info["db"],
)
conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
try:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NEW,))
        if cur.fetchone():
            print(f"DB '{DB_NEW}' ya existe. Se conserva; solo aplico DDL adentro.")
        else:
            print(f"Creando database '{DB_NEW}'...")
            cur.execute(f'CREATE DATABASE "{DB_NEW}"')
            print(f"  ✓ CREATE DATABASE {DB_NEW}")
finally:
    conn.close()

# 2) Conectamos a la DB nueva y aplicamos el DDL completo
conn2 = psycopg2.connect(
    host=info["host"], port=info.get("port") or 5432,
    user=info["user"], password=info["pw"], dbname=DB_NEW,
)
try:
    with conn2.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.personas (
            id               SERIAL PRIMARY KEY,
            numero_excel     INTEGER,
            nombre_apellido  TEXT NOT NULL,
            dni              TEXT,
            genero           CHAR(1),
            matricula        TEXT,
            tomo             INTEGER,
            folio            INTEGER,
            domicilio        TEXT,
            jurisdiccion     TEXT,
            dni_recibido     BOOLEAN DEFAULT FALSE,
            cotejado         TEXT,
            observaciones    TEXT,
            leyenda          TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_personas_dni       ON {SCHEMA}.personas (dni);
        CREATE INDEX IF NOT EXISTS ix_personas_matricula ON {SCHEMA}.personas (matricula);
        CREATE INDEX IF NOT EXISTS ix_personas_nombre    ON {SCHEMA}.personas (nombre_apellido);
        CREATE INDEX IF NOT EXISTS ix_personas_tomo_folio ON {SCHEMA}.personas (tomo, folio);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_personas_dni ON {SCHEMA}.personas (dni);
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.fotos (
            id                    SERIAL PRIMARY KEY,
            persona_id            INTEGER REFERENCES {SCHEMA}.personas(id) ON DELETE SET NULL,
            filename_original     TEXT NOT NULL,
            minio_bucket          TEXT NOT NULL,
            minio_object_key      TEXT NOT NULL UNIQUE,
            content_type          TEXT,
            size_bytes            BIGINT,
            sha256                TEXT,
            source_file_sha256    TEXT,
            tipo                  TEXT,
            dni_detectado         TEXT,
            matricula_detectada   TEXT,
            nombre_detectado      TEXT,
            match_status          TEXT NOT NULL DEFAULT 'pendiente',
            match_confidence      REAL,
            match_notas           TEXT,
            raw_ocr               JSONB,
            uploaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at          TIMESTAMPTZ,
            CONSTRAINT chk_match_status CHECK (match_status IN
                ('pendiente','procesando','matched','ambiguo','sin_match','error','manual'))
        );
        CREATE INDEX IF NOT EXISTS ix_fotos_persona    ON {SCHEMA}.fotos (persona_id);
        CREATE INDEX IF NOT EXISTS ix_fotos_status     ON {SCHEMA}.fotos (match_status);
        CREATE INDEX IF NOT EXISTS ix_fotos_dni_det    ON {SCHEMA}.fotos (dni_detectado);
        CREATE INDEX IF NOT EXISTS ix_fotos_matr_det   ON {SCHEMA}.fotos (matricula_detectada);
        CREATE INDEX IF NOT EXISTS ix_fotos_source_sha ON {SCHEMA}.fotos (source_file_sha256);
        CREATE INDEX IF NOT EXISTS ix_fotos_tipo       ON {SCHEMA}.fotos (tipo);
        """)

        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.fotos_personas (
            foto_id             INTEGER NOT NULL REFERENCES {SCHEMA}.fotos(id) ON DELETE CASCADE,
            persona_id          INTEGER NOT NULL REFERENCES {SCHEMA}.personas(id) ON DELETE CASCADE,
            dni_detectado       TEXT,
            nombre_detectado    TEXT,
            persona_creada      BOOLEAN NOT NULL DEFAULT FALSE,
            campos_enriquecidos TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (foto_id, persona_id)
        );
        CREATE INDEX IF NOT EXISTS ix_fp_persona ON {SCHEMA}.fotos_personas (persona_id);
        """)

        # trigger updated_at en personas
        cur.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.set_updated_at() RETURNS trigger AS $$
        BEGIN NEW.updated_at = now(); RETURN NEW; END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_personas_updated ON {SCHEMA}.personas;
        CREATE TRIGGER trg_personas_updated
        BEFORE UPDATE ON {SCHEMA}.personas
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at();
        """)

        # trigger auto dni_recibido cuando se vincula una foto tipo='dni'
        cur.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.set_dni_recibido_on_link()
        RETURNS TRIGGER AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM {SCHEMA}.fotos
                WHERE id = NEW.foto_id AND tipo = 'dni'
            ) THEN
                UPDATE {SCHEMA}.personas
                SET dni_recibido = TRUE
                WHERE id = NEW.persona_id
                  AND (dni_recibido IS NULL OR dni_recibido = FALSE);
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_fp_set_dni_recibido ON {SCHEMA}.fotos_personas;
        CREATE TRIGGER trg_fp_set_dni_recibido
        AFTER INSERT ON {SCHEMA}.fotos_personas
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_dni_recibido_on_link();
        """)

        conn2.commit()
        print(f"  ✓ Schema {SCHEMA} con tablas + triggers + índices en '{DB_NEW}'")

        # Reporte final
        cur.execute(f"""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = %s ORDER BY 1
        """, (SCHEMA,))
        print(f"\nTablas en {DB_NEW}.{SCHEMA}:")
        for (t,) in cur.fetchall():
            cur.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{t}")
            n = cur.fetchone()[0]
            print(f"  - {t}: {n} filas")
finally:
    conn2.close()

# 3) Construir la connection string nueva y sugerir al usuario que la agregue al .env
new_url = (
    f"postgresql+psycopg2://{info['user']}:{info['pw']}"
    f"@{info['host']}:{info.get('port') or 5432}/{DB_NEW}"
)
print("\n" + "=" * 72)
print("Agregá esta línea a tu .env (si no está):")
print(f"DB_NACION_CONNECTION_STRING={new_url}")
