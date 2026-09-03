import pandas as pd
import re

path = r"c:\Users\octav\Downloads\Consejo de la magistratura\AVALES_ELECCION_2026_control.xlsx"
df = pd.read_excel(path, sheet_name="Hoja1", header=1)
# drop rows without nombre
df = df.dropna(subset=["NOMBRE Y APELLIDO"]).reset_index(drop=True)
print("Filas con nombre:", len(df))
print("Columnas:", list(df.columns))

def norm_dni(v):
    if pd.isna(v): return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip().replace(" ", "").replace(".", "")
    return s or None

df["dni_norm"] = df["DNI"].apply(norm_dni)
print("\n-- DNI --")
print("Vacios:", df["dni_norm"].isna().sum())
dups = df[df.duplicated("dni_norm", keep=False) & df["dni_norm"].notna()].sort_values("dni_norm")
print("Duplicados:", len(dups))
if len(dups):
    print(dups[["NOMBRE Y APELLIDO", "dni_norm"]].to_string())
no_digit = df[df["dni_norm"].notna() & ~df["dni_norm"].astype(str).str.fullmatch(r"\d+").fillna(False)]
print("DNIs no-solo-digitos:", len(no_digit))
if len(no_digit):
    print(no_digit[["NOMBRE Y APELLIDO", "DNI", "dni_norm"]].to_string())

print("\n-- MATRICULA (muestreo de patrones) --")
mats = df["MATRICULA"].dropna().astype(str).str.strip()
print("Total con matricula:", len(mats), " / vacios:", df["MATRICULA"].isna().sum())
# Try parsing T{tomo} F{folio}
pat = re.compile(r"^\s*[TtTº°°]\s*[°º°]?\s*(\d+)\s*[FfFº°°]\s*[°º°]?\s*(\d+)\s*$")
def parse(m):
    if pd.isna(m): return (None, None, False)
    s = str(m).strip()
    r = pat.match(s)
    if r: return (int(r.group(1)), int(r.group(2)), True)
    return (None, None, False)

parsed = df["MATRICULA"].apply(parse)
df["tomo"] = parsed.apply(lambda x: x[0])
df["folio"] = parsed.apply(lambda x: x[1])
df["parsed_ok"] = parsed.apply(lambda x: x[2])
print("Parseadas OK:", df["parsed_ok"].sum(), " / con matricula:", df["MATRICULA"].notna().sum())
falla = df[df["MATRICULA"].notna() & ~df["parsed_ok"]]
if len(falla):
    print("\nMatriculas NO parseadas (muestra):")
    print(falla[["NOMBRE Y APELLIDO", "MATRICULA"]].head(30).to_string())
