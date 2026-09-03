"""
Reset limpio para reprocesar todo con el nuevo modelo multi-persona:
- Borra TODAS las fotos (y sus vínculos por cascade).
- Borra las personas auto-creadas (observaciones LIKE 'Creada auto%').
- Deja intactos los 304 avales del Excel original.
- Deja intactos los objetos en MinIO (para no re-subir; la deduplicación por SHA los reusa).

Uso:
    python reset_batch.py                 # muestra qué haría
    python reset_batch.py --yes           # ejecuta
"""
import os
import argparse
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)
SCHEMA = "avales_2026"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="ejecutar (sin esto, dry-run)")
    args = ap.parse_args()

    with engine.connect() as c:
        fotos = c.execute(text(f"SELECT COUNT(*) FROM {SCHEMA}.fotos")).scalar()
        links = c.execute(text(f"SELECT COUNT(*) FROM {SCHEMA}.fotos_personas")).scalar()
        auto = c.execute(text(
            f"SELECT COUNT(*) FROM {SCHEMA}.personas WHERE observaciones ILIKE 'Creada auto%%'"
        )).scalar()
        total = c.execute(text(f"SELECT COUNT(*) FROM {SCHEMA}.personas")).scalar()

    print(f"Estado actual:")
    print(f"  fotos              : {fotos}")
    print(f"  fotos_personas     : {links}")
    print(f"  personas totales   : {total}")
    print(f"  personas auto-creadas (se borrarán): {auto}")
    print(f"  personas del Excel (se conservan)  : {total - auto}")
    print()

    if not args.yes:
        print("Dry-run. Corré con --yes para ejecutar.")
        return

    with engine.begin() as c:
        c.execute(text(f"DELETE FROM {SCHEMA}.fotos_personas"))
        c.execute(text(f"DELETE FROM {SCHEMA}.fotos"))
        c.execute(text(f"DELETE FROM {SCHEMA}.personas WHERE observaciones ILIKE 'Creada auto%%'"))
    print("Reset OK. Ahora podés correr: python process_whatsapp.py")


if __name__ == "__main__":
    main()
