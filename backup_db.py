"""
Backup del schema avales_2026:
1) Clona los datos a un schema avales_2026_backup_<timestamp> en la misma DB.
2) Exporta a CSV locales en ./backups/YYYYMMDD_HHMMSS/*.csv (incluye JSON de raw_ocr).

Restauración:
- SQL:  INSERT INTO avales_2026.<tabla> SELECT * FROM avales_2026_backup_<ts>.<tabla>;
- CSV:  reimportar con pandas/psql \\copy.
"""
import os
import csv
import json
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)
SCHEMA = "avales_2026"
TABLES = ["personas", "fotos", "fotos_personas"]

ts = datetime.now().strftime("%Y%m%d_%H%M%S")
BACKUP_SCHEMA = f"{SCHEMA}_backup_{ts}"
CSV_DIR = Path(__file__).parent / "backups" / ts
CSV_DIR.mkdir(parents=True, exist_ok=True)

print(f"Backup timestamp: {ts}")
print(f"Schema DB:  {BACKUP_SCHEMA}")
print(f"CSV local:  {CSV_DIR}")
print("-" * 60)

# 1) Backup a schema clonado
with engine.begin() as c:
    c.execute(text(f"CREATE SCHEMA {BACKUP_SCHEMA}"))
    for t in TABLES:
        c.execute(text(
            f"CREATE TABLE {BACKUP_SCHEMA}.{t} AS SELECT * FROM {SCHEMA}.{t}"
        ))
        n = c.execute(text(f"SELECT COUNT(*) FROM {BACKUP_SCHEMA}.{t}")).scalar()
        print(f"  [DB]  {BACKUP_SCHEMA}.{t}: {n} filas clonadas")

# 2) Backup a CSV locales
print()
with engine.connect() as c:
    for t in TABLES:
        rows = list(c.execute(text(f"SELECT * FROM {SCHEMA}.{t} ORDER BY 1")).mappings())
        path = CSV_DIR / f"{t}.csv"
        if not rows:
            path.write_text("", encoding="utf-8")
            print(f"  [CSV] {path.name}: 0 filas")
            continue
        cols = list(rows[0].keys())
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in rows:
                w.writerow([
                    json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v
                    for v in r.values()
                ])
        print(f"  [CSV] {path.name}: {len(rows)} filas ({path.stat().st_size} bytes)")

print()
print("Backup completo.")
print()
print("Para restaurar desde el schema clonado (si algo sale mal):")
for t in TABLES:
    print(f"  TRUNCATE {SCHEMA}.{t} CASCADE;")
for t in TABLES:
    print(f"  INSERT INTO {SCHEMA}.{t} SELECT * FROM {BACKUP_SCHEMA}.{t};")
print()
print("Para borrar el backup cuando ya no lo necesites:")
print(f"  DROP SCHEMA {BACKUP_SCHEMA} CASCADE;")
