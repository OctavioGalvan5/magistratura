"""Migración v2: tipo en fotos + tabla junction fotos_personas."""
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)
SCHEMA = "avales_2026"

DDL = f"""
ALTER TABLE {SCHEMA}.fotos ADD COLUMN IF NOT EXISTS source_file_sha256 TEXT;
CREATE INDEX IF NOT EXISTS ix_fotos_source_sha ON {SCHEMA}.fotos (source_file_sha256);

ALTER TABLE {SCHEMA}.fotos ADD COLUMN IF NOT EXISTS tipo TEXT;
CREATE INDEX IF NOT EXISTS ix_fotos_tipo ON {SCHEMA}.fotos (tipo);

CREATE TABLE IF NOT EXISTS {SCHEMA}.fotos_personas (
    foto_id           INTEGER NOT NULL REFERENCES {SCHEMA}.fotos(id) ON DELETE CASCADE,
    persona_id        INTEGER NOT NULL REFERENCES {SCHEMA}.personas(id) ON DELETE CASCADE,
    dni_detectado     TEXT,
    nombre_detectado  TEXT,
    persona_creada    BOOLEAN NOT NULL DEFAULT FALSE,
    campos_enriquecidos TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (foto_id, persona_id)
);
CREATE INDEX IF NOT EXISTS ix_fp_persona ON {SCHEMA}.fotos_personas (persona_id);
"""

with engine.begin() as c:
    c.execute(text(DDL))
    tables = c.execute(text(
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema='{SCHEMA}' ORDER BY 1"
    )).scalars().all()
print("Tablas:", tables)
print("OK — schema v2 aplicado.")
