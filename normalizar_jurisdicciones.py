"""
Normaliza jurisdicciones colapsando variantes de escritura a nombres canonicos
(estilo Excel: sin tildes, ASCII).

Uso:
    python normalizar_jurisdicciones.py            # dry-run
    python normalizar_jurisdicciones.py --yes      # aplica los UPDATE
"""
import os, sys, argparse
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)
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

    # ── Jujuy (jurisdicción de la Cámara Salta-Jujuy) ──
    "CF Salta-Jujuy":          "Jujuy",
    "Cámara F. de Salta-Jujuy": "Jujuy",

    # ── Bahía Blanca ──
    "Bahía Blanca":            "Bahia Blanca",

    # ── Córdoba ──
    "Córdoba":                 "Cordoba",
    "Federal Cba":             "Cordoba",

    # ── Córdoba Río IV ──
    "Córdoba Río Cuarto":      "Cordoba Rio IV",
    "Córdoba Río IV":          "Cordoba Rio IV",

    # ── Tucumán ──
    "Tucumán":                 "Tucuman",
    "Capital Tuc":             "Tucuman",
    "Capital - Tuc.":          "Tucuman",
    "Capital":                 "Tucuman",   # ambigüo, pero en este contexto probablemente Tucumán
    "Córdoba - Tuc":           "Tucuman",   # típico typo

    # ── Rosario Sta Fe ──
    "Rosario":                 "Rosario Sta Fe",
    "Rosario de Sta Fe":       "Rosario Sta Fe",

    # ── Mar del Plata ──
    "Mar de Plata":            "Mar del Plata",

    # ── Roque Sáenz Peña ──
    "Pcia. P.S. Peña":         "Roque Saenz Peña",
    "Pcia. R.S. Peña":         "Roque Saenz Peña",
    "II Cir. R.S. Peña":       "Roque Saenz Peña",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="aplica los UPDATE (sin esto es dry-run)")
    args = ap.parse_args()

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
