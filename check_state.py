import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)

with engine.connect() as c:
    print("=== FOTOS SUBIDAS ===")
    for r in c.execute(text("""
        SELECT f.id, f.filename_original, f.match_status, f.dni_detectado, f.nombre_detectado,
               p.nombre_apellido AS persona
        FROM avales_2026.fotos f
        LEFT JOIN avales_2026.personas p ON p.id = f.persona_id
        ORDER BY f.uploaded_at
    """)).mappings():
        print(f"  {r['id']:3d} {r['filename_original'][:60]:60s} | {r['match_status']:10s} | DNI {r['dni_detectado'] or '---':10s} | {r['persona'] or '---'}")

    print()
    print("=== PERSONAS CREADAS AUTOMATICAMENTE ===")
    for r in c.execute(text("""
        SELECT id, nombre_apellido, dni, genero, jurisdiccion, observaciones
        FROM avales_2026.personas
        WHERE observaciones ILIKE '%Creada auto%'
        ORDER BY id
    """)).mappings():
        print(f"  #{r['id']} | DNI {r['dni']} | {r['nombre_apellido']} ({r['genero'] or '?'}) | jur={r['jurisdiccion'] or '-'}")

    total = c.execute(text("SELECT COUNT(*) FROM avales_2026.personas")).scalar()
    fotos = c.execute(text("SELECT COUNT(*) FROM avales_2026.fotos")).scalar()
    print(f"\nTotal personas: {total} | Total fotos: {fotos}")
