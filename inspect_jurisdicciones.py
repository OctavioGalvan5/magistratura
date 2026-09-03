import os, sys
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
load_dotenv()
e = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)

print("=== JURISDICCIONES: total personas vs personas con foto ===\n")
print(f"{'jurisdiccion':45s} {'total':>6s} {'c/foto':>7s} {'s/foto':>7s}")
print("-" * 68)
with e.connect() as c:
    for r in c.execute(text("""
        SELECT COALESCE(p.jurisdiccion, '(NULL)') AS jur,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE EXISTS (
                 SELECT 1 FROM avales_2026.fotos_personas fp WHERE fp.persona_id = p.id
               )) AS con_foto,
               COUNT(*) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM avales_2026.fotos_personas fp WHERE fp.persona_id = p.id
               )) AS sin_foto
        FROM avales_2026.personas p
        GROUP BY 1
        ORDER BY total DESC
    """)):
        print(f"{r[0][:45]:45s} {r[1]:>6d} {r[2]:>7d} {r[3]:>7d}")

print()
with e.connect() as c:
    total_juris = c.execute(text(
        "SELECT COUNT(DISTINCT jurisdiccion) FROM avales_2026.personas WHERE jurisdiccion IS NOT NULL"
    )).scalar()
    sin_jur = c.execute(text(
        "SELECT COUNT(*) FROM avales_2026.personas WHERE jurisdiccion IS NULL"
    )).scalar()
    total_p = c.execute(text("SELECT COUNT(*) FROM avales_2026.personas")).scalar()
    print(f"Total jurisdicciones distintas: {total_juris}")
    print(f"Personas sin jurisdiccion (NULL): {sin_jur}")
    print(f"Total personas: {total_p}")
