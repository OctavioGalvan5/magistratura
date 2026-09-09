"""
Normaliza jurisdicciones colapsando variantes de escritura a nombres canonicos
(estilo Excel: sin tildes, ASCII).

Uso:
    python normalizar_jurisdicciones.py                       # dry-run sobre julia
    python normalizar_jurisdicciones.py --yes                 # aplica sobre julia
    python normalizar_jurisdicciones.py --db nacion           # dry-run sobre nacion
    python normalizar_jurisdicciones.py --db nacion --yes     # aplica sobre nacion
"""
import sys, argparse
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from db_config import resolve_db_url, DB_CHOICES

load_dotenv()
SCHEMA = "avales_2026"

# Mapa: {variante_actual: canonico}. Todo lo que NO esté acá queda igual.
MAPA = {
    # ── Salta (Cámara Federal de Salta) ──
    "Cám. Fed. Salta":         "Salta",
    "Cámara F. de Salta":      "Salta",
    "Cam. Fed. Apel. Salta":   "Salta",
    "Cám. Fed. Apel. Salta":   "Salta",
    "CAF SALTA":               "Salta",
    "Cámara Apel. Salta":      "Salta",
    "Cám. de Apel. de Salta":  "Salta",
    "SA CTA":                  "Salta",
    "SALTA":                   "Salta",
    "COLEGIO DE SALTA":        "Salta",
    "Cam Fed Salta":           "Salta",
    "Cámara Federal Salta":    "Salta",
    # Salta-Jujuy → Salta (decisión del usuario)
    "Salta Jujuy":                    "Salta",
    "General T. de Salta Jujuy":      "Salta",
    "Ciudad T.A. Salta Jujuy":        "Salta",

    # ── Jujuy (jurisdicción de la Cámara Salta-Jujuy) ──
    "CF Salta-Jujuy":          "Jujuy",
    "Cámara F. de Salta-Jujuy": "Jujuy",
    "COLEGIO DE JUJUY":        "Jujuy",

    # ── Bahía Blanca ──
    "Bahía Blanca":            "Bahia Blanca",

    # ── Córdoba ──
    "Córdoba":                 "Cordoba",
    "Federal Cba":             "Cordoba",
    "CORDOBA":                 "Cordoba",
    "CÓRDOBA":                 "Cordoba",
    "Cba":                     "Cordoba",
    "CBA":                     "Cordoba",
    "Cordoba Capital":         "Cordoba",
    "Córdoba Capital":         "Cordoba",
    "CORDOBA CAPITAL":         "Cordoba",
    "CBA CAP.":                "Cordoba",
    "COM. FED. CBA":           "Cordoba",
    "COM. FED. CORDOBA":       "Cordoba",
    "CAM. FED. CORDOBA":       "Cordoba",
    "CA. FED. CBA":            "Cordoba",
    "CAM. FED. CÓRDOBA":       "Cordoba",
    "CAMARA FEDERAL CORDOBA":  "Cordoba",
    "Camara Federal Cba":      "Cordoba",
    "Camara Federal Córdoba":  "Cordoba",
    "C. FED. CBS":             "Cordoba",
    # "Federal" solo → Cordoba (decisión del usuario)
    "Federal":                 "Cordoba",
    "Federal Interior":        "Cordoba",
    "Córdoba - Federal":       "Cordoba",
    "CÓRDOBA / FEDERAL":       "Cordoba",
    "JUSTICIA FEDERAL DE CÓRDOBA": "Cordoba",
    # Villa Maria + Córdoba hibridos → Cordoba (decisión del usuario: "por Cordoba")
    "Villa Maria - Cordoba":   "Cordoba",
    "Villa Maria - Cba":       "Cordoba",
    "Villa Maria - Córdoba":   "Cordoba",
    "Villa María, Córdoba":    "Cordoba",
    "Villa Maria, Córdoba":    "Cordoba",
    "Villa María, CBA":        "Cordoba",

    # ── Córdoba Río IV ──
    "Córdoba Río Cuarto":      "Cordoba Rio IV",
    "Córdoba Río IV":          "Cordoba Rio IV",
    "Río Cuarto":              "Cordoba Rio IV",
    "Rio Cuarto":              "Cordoba Rio IV",
    "Córdoba - Río Cuarto":    "Cordoba Rio IV",
    "Córdoba - Río IV":        "Cordoba Rio IV",

    # ── Tucumán ──
    "Tucumán":                 "Tucuman",
    "Capital Tuc":             "Tucuman",
    "Capital - Tuc.":          "Tucuman",
    "Capital":                 "Tucuman",   # ambigüo, pero en este contexto probablemente Tucumán
    "Córdoba - Tuc":           "Tucuman",   # típico typo
    "TUCUMAN":                 "Tucuman",
    "TUC":                     "Tucuman",
    "COLEGIO DE TUCUMAN":      "Tucuman",
    "San Miguel de Tucumán":   "Tucuman",
    "Tucuma":                  "Tucuman",
    # San Miguel De Tucuman - Catamarca → Tucuman (decisión del usuario)
    "San Miguel De Tucuman - Catamarca": "Tucuman",

    # ── Rosario Sta Fe ──
    "Rosario":                 "Rosario Sta Fe",
    "Rosario de Sta Fe":       "Rosario Sta Fe",
    "ROSARIO":                 "Rosario Sta Fe",
    "COLEGIO DE ROSARIO":      "Rosario Sta Fe",
    "CAM. FED. ROSARIO":       "Rosario Sta Fe",
    "Rosario CF":              "Rosario Sta Fe",
    "Rosario - Sta Fe":        "Rosario Sta Fe",
    "Rosario - Santa Fe":      "Rosario Sta Fe",
    "Rosario, Santa Fe":       "Rosario Sta Fe",
    "Camara Federal de Rosario": "Rosario Sta Fe",

    # ── Villa Maria (canónico independiente) ──
    "Villa María":             "Villa Maria",
    "V. Maria":                "Villa Maria",
    "COLEGIO DE VILLA MARIA":  "Villa Maria",

    # ── Santa Fe ──
    "SANTA FE":                "Santa Fe",
    "SFe":                     "Santa Fe",
    "COLEGIO DE SANTA FE":     "Santa Fe",
    "1ra Circunscripción Santa Fe": "Santa Fe",

    # ── Neuquén ──
    "NEUQUÉN":                 "Neuquen",
    "COLEGIO DE NEUQUEN":      "Neuquen",

    # ── Mendoza ──
    "MENDOZA":                 "Mendoza",

    # ── Corrientes ──
    "COLEGIO DE CORRIENTES":   "Corrientes",

    # ── Catamarca ──
    "CATAMARCA":               "Catamarca",

    # ── San Juan ──
    "COLEGIO DE SAN JUAN":     "San Juan",

    # ── La Rioja ──
    "COLEGIO DE LA RIOJA":     "La Rioja",

    # ── Chaco ──
    "COLEGIO DE CHACO":        "Chaco",

    # ── Misiones ──
    "MISIONES":                "Misiones",
    "COLEGIO DE MISIONES":     "Misiones",
    "Posadas, Misiones":       "Misiones",
    "CAM FED APEL POSADAS":    "Misiones",

    # ── Entre Ríos ──
    "Entre Ríos":              "Entre Rios",
    "E. Rios":                 "Entre Rios",
    "COLEGIO DE ENTRE RIOS":   "Entre Rios",
    "CONCORDIA - ENTRE RIOS":  "Entre Rios",

    # ── Paraná ──
    "Paraná":                  "Parana",
    "PARANA":                  "Parana",

    # ── Bell Ville ──
    "COLEGIO DE BELL VILLE":         "Bell Ville",
    "Bell Ville - Federal":          "Bell Ville",
    "Bell Ville - Federal Córdoba":  "Bell Ville",

    # ── Marcos Juárez ──
    "COLEGIO DE MARCOS JUAREZ": "Marcos Juarez",

    # ── Santiago del Estero ──
    "COLEGIO DE SGO.DEL ESTERO": "Santiago del Estero",
    "Santiago":                  "Santiago del Estero",

    # ── Ciudad Autónoma ──
    "CABA":                        "Ciudad Autonoma",
    "Capital Federal":             "Ciudad Autonoma",
    "Buenos Aires":                "Ciudad Autonoma",
    "COLEGIO DE CIUDAD AUTONOMA":  "Ciudad Autonoma",

    # ── San Luis ──
    "COLEGIO DE SAN LUIS":     "San Luis",

    # ── Venado Tuerto ──
    "COLEGIO VENADO TUERTO":   "Venado Tuerto",
    "Venado Tuerto - Ros":     "Venado Tuerto",
    "V. Tuerto / Rosario":     "Venado Tuerto",
    "V. Tuerto / Ros":         "Venado Tuerto",
    "V. Tuerto - Rosario":     "Venado Tuerto",
    "V. Tto / Rosario":        "Venado Tuerto",
    "Ven. Tto / Rosario":      "Venado Tuerto",
    "Venado Tto / Ros":        "Venado Tuerto",
    "Venado Tuerto / Ros":     "Venado Tuerto",

    # ── Roque Sáenz Peña ──
    "Pcia. P.S. Peña":                "Roque Saenz Peña",
    "Pcia. R.S. Peña":                "Roque Saenz Peña",
    "II Cir. R.S. Peña":              "Roque Saenz Peña",
    "Pcia RS Pen":                    "Roque Saenz Peña",
    "Rcia RS Pen":                    "Roque Saenz Peña",
    "R.S. Peña":                      "Roque Saenz Peña",
    "R.P. Sáenz Peña":                "Roque Saenz Peña",
    "Pcia. Roque Saenz Peña, Chaco":  "Roque Saenz Peña",

    # ── Mar del Plata ──
    "Mar de Plata":            "Mar del Plata",

    # ── Bariloche (canónico nuevo) ──
    "S.C. de Bariloche":       "Bariloche",

    # ── General Roca (canónico nuevo) ──
    "General Roca - Cámara Federal de Apelaciones": "General Roca",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="julia", choices=DB_CHOICES, help="qué DB normalizar")
    ap.add_argument("--yes", action="store_true", help="aplica los UPDATE (sin esto es dry-run)")
    args = ap.parse_args()

    engine = create_engine(resolve_db_url(args.db), pool_pre_ping=True)

    print(f"DB: {args.db}")
    print(f"Cambios a aplicar: {len(MAPA)} variantes")
    print("-" * 60)

    total_afectadas = 0
    with engine.connect() as c:
        for variante, canonica in MAPA.items():
            n = c.execute(text(
                f"SELECT COUNT(*) FROM {SCHEMA}.personas WHERE jurisdiccion = :v"
            ), {"v": variante}).scalar()
            if n:
                print(f"  {variante!r:40s} → {canonica!r:20s} ({n} personas)")
                total_afectadas += n

    print("-" * 60)
    print(f"Total personas afectadas: {total_afectadas}")

    if not args.yes:
        print("\nDRY-RUN. Corré con --yes para aplicar.")
        return

    print("\nAplicando UPDATE...")
    with engine.begin() as c:
        for variante, canonica in MAPA.items():
            c.execute(text(
                f"UPDATE {SCHEMA}.personas SET jurisdiccion = :c WHERE jurisdiccion = :v"
            ), {"c": canonica, "v": variante})

    # Reporte post
    print("\n=== JURISDICCIONES DESPUES DE NORMALIZAR ===")
    with engine.connect() as c:
        for r in c.execute(text(f"""
            SELECT COALESCE(jurisdiccion, '(NULL)') AS jur, COUNT(*) AS n
            FROM {SCHEMA}.personas
            GROUP BY 1
            ORDER BY n DESC
        """)):
            print(f"  {r[0][:45]:45s} {r[1]:>5d}")
        total = c.execute(text(
            f"SELECT COUNT(DISTINCT jurisdiccion) FROM {SCHEMA}.personas WHERE jurisdiccion IS NOT NULL"
        )).scalar()
        print(f"\nTotal jurisdicciones distintas: {total}")


if __name__ == "__main__":
    main()
