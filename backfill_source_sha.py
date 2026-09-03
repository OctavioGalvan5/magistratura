"""
Backfill de source_file_sha256 para las fotos ya subidas antes de este cambio.
Recorre los archivos de la carpeta WhatsApp y para cada uno computa su SHA;
si en fotos hay filas con filename_original que empieza con el stem del archivo,
las marca con source_file_sha256 = <sha del archivo original>.
"""
import os
import sys
import hashlib
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

load_dotenv()
engine = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)
FOLDER = Path(r"c:\Users\octav\Downloads\Consejo de la magistratura\WhatsApp Chat - Avales CMF")

# Asegurar columna
with engine.begin() as c:
    c.execute(text("ALTER TABLE avales_2026.fotos ADD COLUMN IF NOT EXISTS source_file_sha256 TEXT"))
    c.execute(text("CREATE INDEX IF NOT EXISTS ix_fotos_source_sha ON avales_2026.fotos (source_file_sha256)"))

updated = 0
with engine.begin() as c:
    for p in sorted(FOLDER.iterdir()):
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except Exception:
            continue
        sha = hashlib.sha256(data).hexdigest()
        # Para imagen: filename_original == p.name
        # Para PDF: fotos.filename_original empieza con p.stem + "__p"
        rows = c.execute(text("""
            UPDATE avales_2026.fotos
            SET source_file_sha256 = :sha
            WHERE source_file_sha256 IS NULL
              AND (filename_original = :fn OR filename_original LIKE :prefix)
            RETURNING id, filename_original
        """), {"sha": sha, "fn": p.name, "prefix": f"{p.stem}__p%"}).all()
        if rows:
            print(f"{p.name} → sha {sha[:12]}… → {len(rows)} filas backfilled")
            updated += len(rows)

print(f"\nTotal fotos actualizadas: {updated}")
