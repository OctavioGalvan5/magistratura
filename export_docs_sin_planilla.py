"""
Exporta a Excel las personas que tienen DNI/credencial/otro pero NO tienen planilla.

Uso:
    python export_docs_sin_planilla.py --db nacion
"""
import sys, argparse
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
import pandas as pd
from db_config import resolve_db_url, DB_CHOICES

load_dotenv()
SCHEMA = "avales_2026"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="julia", choices=DB_CHOICES)
    args = ap.parse_args()

    engine = create_engine(resolve_db_url(args.db), pool_pre_ping=True)

    sql = f"""
    WITH tipos AS (
      SELECT p.id AS persona_id,
             BOOL_OR(f.tipo = 'planilla_aval')                        AS has_planilla,
             BOOL_OR(f.tipo = 'dni')                                  AS has_dni,
             BOOL_OR(f.tipo = 'credencial')                           AS has_credencial,
             BOOL_OR((f.tipo IS NULL) OR (LOWER(f.tipo) = 'otro'))    AS has_otro,
             COUNT(*) FILTER (WHERE f.tipo = 'dni')                   AS n_dni,
             COUNT(*) FILTER (WHERE f.tipo = 'credencial')            AS n_credencial,
             COUNT(*) FILTER (WHERE (f.tipo IS NULL) OR (LOWER(f.tipo) = 'otro')) AS n_otro
      FROM {SCHEMA}.personas p
      JOIN {SCHEMA}.fotos_personas fp ON fp.persona_id = p.id
      JOIN {SCHEMA}.fotos f           ON f.id = fp.foto_id
      GROUP BY p.id
    )
    SELECT p.jurisdiccion,
           p.nombre_apellido,
           p.dni,
           p.matricula,
           p.tomo,
           p.folio,
           t.has_dni,
           t.has_credencial,
           t.has_otro,
           t.n_dni,
           t.n_credencial,
           t.n_otro,
           p.observaciones
    FROM {SCHEMA}.personas p
    JOIN tipos t ON t.persona_id = p.id
    WHERE t.has_planilla = FALSE
      AND (t.has_dni OR t.has_credencial OR t.has_otro)
    ORDER BY p.jurisdiccion NULLS LAST, p.nombre_apellido
    """

    with engine.connect() as c:
        df = pd.read_sql(text(sql), c)

    fecha = datetime.now().strftime("%Y%m%d")
    out = Path(__file__).parent / f"personas_doc_sin_planilla_{args.db}_{fecha}.xlsx"

    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="doc_sin_planilla", index=False)
        resumen = (df.groupby("jurisdiccion", dropna=False)
                     .size().reset_index(name="personas")
                     .sort_values("personas", ascending=False))
        resumen.to_excel(xw, sheet_name="por_jurisdiccion", index=False)

    print(f"DB: {args.db}")
    print(f"Personas con doc pero sin planilla: {len(df)}")
    print(f"Jurisdicciones afectadas: {df['jurisdiccion'].nunique(dropna=False)}")
    print(f"Excel: {out}")


if __name__ == "__main__":
    main()
