"""
1) Crea/actualiza un trigger en avales_2026.fotos_personas para que cada vez que
   se vincule una foto tipo='dni' a una persona, automáticamente se marque
   personas.dni_recibido = TRUE (si estaba en NULL o FALSE).
2) Corre un UPDATE inicial (backfill) para las personas que ya tenían un DNI
   vinculado pero seguían con dni_recibido = FALSE / NULL.

El trigger es idempotente y no pisa dni_recibido si ya era TRUE.
"""
import os, sys
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)

SCHEMA = "avales_2026"

DDL = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.set_dni_recibido_on_link()
RETURNS TRIGGER AS $$
BEGIN
    -- Si se está linkeando una foto de tipo 'dni', marcamos dni_recibido = TRUE
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
"""

BACKFILL = f"""
UPDATE {SCHEMA}.personas p
SET dni_recibido = TRUE
WHERE (p.dni_recibido IS NULL OR p.dni_recibido = FALSE)
  AND EXISTS (
      SELECT 1
      FROM {SCHEMA}.fotos_personas fp
      JOIN {SCHEMA}.fotos f ON f.id = fp.foto_id
      WHERE fp.persona_id = p.id AND f.tipo = 'dni'
  );
"""

with engine.begin() as c:
    print("Creando/actualizando trigger…")
    c.execute(text(DDL))
    print("Corriendo backfill…")
    result = c.execute(text(BACKFILL))
    print(f"Personas actualizadas: {result.rowcount}")

with engine.connect() as c:
    total = c.execute(text(f"SELECT COUNT(*) FROM {SCHEMA}.personas")).scalar()
    con_dni = c.execute(text(f"SELECT COUNT(*) FROM {SCHEMA}.personas WHERE dni_recibido")).scalar()
    con_foto_dni = c.execute(text(f"""
        SELECT COUNT(DISTINCT p.id)
        FROM {SCHEMA}.personas p
        JOIN {SCHEMA}.fotos_personas fp ON fp.persona_id = p.id
        JOIN {SCHEMA}.fotos f ON f.id = fp.foto_id
        WHERE f.tipo = 'dni'
    """)).scalar()
    print()
    print(f"Total personas: {total}")
    print(f"Con dni_recibido = TRUE: {con_dni}")
    print(f"Con al menos 1 foto tipo 'dni' vinculada: {con_foto_dni}")
    if con_dni != con_foto_dni:
        print(f"[!] Diferencia de {abs(con_dni - con_foto_dni)} — probablemente son personas "
              f"marcadas como recibidas a mano pero sin foto de DNI todavía.")
    else:
        print("OK: coinciden.")
