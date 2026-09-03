import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from minio import Minio

load_dotenv()

engine = create_engine(os.environ["DB_CONNECTION_STRING"])

SCHEMA = "avales_2026"
BUCKET = "avales-eleccion-2026"

DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

CREATE TABLE IF NOT EXISTS {SCHEMA}.personas (
    id               SERIAL PRIMARY KEY,
    numero_excel     INTEGER,
    nombre_apellido  TEXT NOT NULL,
    dni              TEXT,
    genero           CHAR(1),
    matricula        TEXT,
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

CREATE TABLE IF NOT EXISTS {SCHEMA}.fotos (
    id                    SERIAL PRIMARY KEY,
    persona_id            INTEGER REFERENCES {SCHEMA}.personas(id) ON DELETE SET NULL,
    filename_original     TEXT NOT NULL,
    minio_bucket          TEXT NOT NULL,
    minio_object_key      TEXT NOT NULL UNIQUE,
    content_type          TEXT,
    size_bytes            BIGINT,
    sha256                TEXT,
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

CREATE INDEX IF NOT EXISTS ix_fotos_persona     ON {SCHEMA}.fotos (persona_id);
CREATE INDEX IF NOT EXISTS ix_fotos_status      ON {SCHEMA}.fotos (match_status);
CREATE INDEX IF NOT EXISTS ix_fotos_dni_det     ON {SCHEMA}.fotos (dni_detectado);
CREATE INDEX IF NOT EXISTS ix_fotos_matr_det    ON {SCHEMA}.fotos (matricula_detectada);

CREATE OR REPLACE FUNCTION {SCHEMA}.set_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_personas_updated ON {SCHEMA}.personas;
CREATE TRIGGER trg_personas_updated
BEFORE UPDATE ON {SCHEMA}.personas
FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at();
"""

print("== Aplicando DDL en PostgreSQL ==")
with engine.begin() as c:
    c.execute(text(DDL))

with engine.connect() as c:
    tables = c.execute(text(
        "select table_name from information_schema.tables where table_schema=:s order by 1"
    ), {"s": SCHEMA}).scalars().all()
    print(f"Tablas en {SCHEMA}: {tables}")
    for t in tables:
        cols = c.execute(text(
            "select column_name, data_type from information_schema.columns "
            "where table_schema=:s and table_name=:t order by ordinal_position"
        ), {"s": SCHEMA, "t": t}).all()
        print(f"\n  {SCHEMA}.{t}:")
        for name, dt in cols:
            print(f"    - {name}: {dt}")

print("\n== MinIO ==")
client = Minio(
    os.environ["MINIO_ENDPOINT"],
    access_key=os.environ["MINIO_ACCESS_KEY"],
    secret_key=os.environ["MINIO_SECRET_KEY"],
    secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
)
if client.bucket_exists(BUCKET):
    print(f"Bucket '{BUCKET}' ya existe, se conserva.")
else:
    client.make_bucket(BUCKET)
    print(f"Bucket '{BUCKET}' creado.")

print("\nOK.")
