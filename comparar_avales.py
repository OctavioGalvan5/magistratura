import os, sys, re, unicodedata
from pathlib import Path
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
load_dotenv()

def slug(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()

folder = Path(r"c:\Users\octav\Downloads\Consejo de la magistratura\Avales")
pdfs = {p.stem.removeprefix("avales.") for p in folder.glob("avales.*.pdf")}

e = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)
db_juris = {}
with e.connect() as c:
    for jur, n in c.execute(text("""
        SELECT p.jurisdiccion, COUNT(*) AS n
        FROM avales_2026.personas p
        WHERE p.jurisdiccion IS NOT NULL
          AND EXISTS (SELECT 1 FROM avales_2026.fotos_personas fp WHERE fp.persona_id = p.id)
        GROUP BY 1
    """)):
        db_juris[slug(jur)] = (jur, n)

en_db = set(db_juris.keys())
faltan = en_db - pdfs
sobran = pdfs - en_db

print(f"PDFs en carpeta Avales/: {len(pdfs)}")
print(f"Jurisdicciones en DB con al menos 1 persona con foto: {len(en_db)}\n")

if faltan:
    print(">> FALTAN estos PDFs (jurisdicciones que tienen personas con fotos pero no hay archivo):")
    for s in sorted(faltan):
        nombre, n = db_juris[s]
        print(f"   - avales.{s}.pdf  ({nombre} - {n} personas)")
else:
    print("No falta ninguno.")

if sobran:
    print("\n>> Hay PDFs sin correspondencia en DB:")
    for s in sorted(sobran):
        print(f"   - avales.{s}.pdf")
