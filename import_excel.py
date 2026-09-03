import os
import re
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.environ["DB_CONNECTION_STRING"])
SCHEMA = "avales_2026"
XLSX = r"c:\Users\octav\Downloads\Consejo de la magistratura\AVALES_ELECCION_2026_control.xlsx"
DUP_CSV = r"c:\Users\octav\Downloads\Consejo de la magistratura\duplicados_para_revisar.csv"

# 1) Ajuste de schema: agregar tomo/folio + UNIQUE(dni)
DDL = f"""
ALTER TABLE {SCHEMA}.personas ADD COLUMN IF NOT EXISTS tomo  INTEGER;
ALTER TABLE {SCHEMA}.personas ADD COLUMN IF NOT EXISTS folio INTEGER;
CREATE INDEX IF NOT EXISTS ix_personas_tomo_folio ON {SCHEMA}.personas (tomo, folio);
CREATE UNIQUE INDEX IF NOT EXISTS uq_personas_dni ON {SCHEMA}.personas (dni);
"""
with engine.begin() as c:
    c.execute(text(DDL))
print("Schema ajustado (tomo, folio, UNIQUE dni).")

# 2) Normalizadores
def norm_str(v):
    if pd.isna(v): return None
    s = str(v).strip()
    return s or None

def norm_dni(v):
    if pd.isna(v): return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip().replace(" ", "").replace(".", "")
    return s or None

def norm_genero(v):
    s = norm_str(v)
    if not s: return None
    s = s.upper()[0]
    return s if s in ("M", "F", "X") else None

def norm_bool_si_no(v):
    s = norm_str(v)
    if not s: return None
    s = s.upper()
    if s in ("SI", "SÍ", "S", "TRUE", "1"): return True
    if s in ("NO", "N", "FALSE", "0"): return False
    return None

MAT_RE = re.compile(r"^\s*[TtTº°]\s*[º°]?\s*(\d+)\s*[FfFº°]\s*[º°]?\s*(\d+)\s*$")
def parse_matricula(v):
    """Return (matricula_original_str, tomo, folio). tomo/folio None si no parsea."""
    if pd.isna(v): return (None, None, None)
    s = str(v).strip()
    if not s: return (None, None, None)
    m = MAT_RE.match(s)
    if m: return (s, int(m.group(1)), int(m.group(2)))
    return (s, None, None)

# 3) Leer excel
df = pd.read_excel(XLSX, sheet_name="Hoja1", header=1)
df = df.dropna(subset=["NOMBRE Y APELLIDO"]).reset_index(drop=True)
print(f"Filas leidas: {len(df)}")

rows = []
for _, r in df.iterrows():
    mat, tomo, folio = parse_matricula(r["MATRICULA"])
    rows.append({
        "numero_excel":    int(r["Unnamed: 0"]) if pd.notna(r["Unnamed: 0"]) else None,
        "nombre_apellido": norm_str(r["NOMBRE Y APELLIDO"]),
        "dni":             norm_dni(r["DNI"]),
        "genero":          norm_genero(r["GENERO (M/F/X)"]),
        "matricula":       mat,
        "tomo":            tomo,
        "folio":           folio,
        "domicilio":       norm_str(r["Domicilio"]),
        "jurisdiccion":    norm_str(r["JURISDICCION"]),
        "dni_recibido":    norm_bool_si_no(r["DNI RECIBIDO (SI/NO)"]),
        "cotejado":        norm_str(r["COTEJADO (OK / VERIFICAR)"]),
        "observaciones":   norm_str(r["OBSERVACIONES"]),
        "leyenda":         norm_str(r["LEYENDA"]),
    })

# 4) Insert con ON CONFLICT DO NOTHING sobre dni
insert_sql = text(f"""
INSERT INTO {SCHEMA}.personas
  (numero_excel, nombre_apellido, dni, genero, matricula, tomo, folio,
   domicilio, jurisdiccion, dni_recibido, cotejado, observaciones, leyenda)
VALUES
  (:numero_excel, :nombre_apellido, :dni, :genero, :matricula, :tomo, :folio,
   :domicilio, :jurisdiccion, :dni_recibido, :cotejado, :observaciones, :leyenda)
ON CONFLICT (dni) DO NOTHING
RETURNING id, dni, nombre_apellido
""")

inserted = []
skipped = []
with engine.begin() as c:
    for row in rows:
        res = c.execute(insert_sql, row).first()
        if res is None:
            skipped.append(row)
        else:
            inserted.append(res)

print(f"\nInsertadas: {len(inserted)}")
print(f"Duplicados salteados (por DNI ya presente): {len(skipped)}")

if skipped:
    pd.DataFrame(skipped).to_csv(DUP_CSV, index=False, encoding="utf-8-sig")
    print(f"CSV con duplicados: {DUP_CSV}")

# 5) Resumen final
with engine.connect() as c:
    total = c.execute(text(f"SELECT COUNT(*) FROM {SCHEMA}.personas")).scalar()
    con_dni = c.execute(text(f"SELECT COUNT(*) FROM {SCHEMA}.personas WHERE dni IS NOT NULL")).scalar()
    sin_dni = c.execute(text(f"SELECT COUNT(*) FROM {SCHEMA}.personas WHERE dni IS NULL")).scalar()
    con_mat = c.execute(text(f"SELECT COUNT(*) FROM {SCHEMA}.personas WHERE tomo IS NOT NULL")).scalar()
    print(f"\n== Estado tabla {SCHEMA}.personas ==")
    print(f"  Total filas: {total}")
    print(f"  Con DNI:     {con_dni}")
    print(f"  Sin DNI:     {sin_dni}")
    print(f"  Con tomo/folio parseados: {con_mat}")
    print("\n  Por jurisdiccion:")
    rows = c.execute(text(
        f"SELECT jurisdiccion, COUNT(*) FROM {SCHEMA}.personas GROUP BY jurisdiccion ORDER BY 2 DESC"
    )).all()
    for j, n in rows:
        print(f"    {j}: {n}")
