import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)
with engine.connect() as c:
    total = c.execute(text("SELECT COUNT(*) FROM avales_2026.personas")).scalar()
    con_dni = c.execute(text("SELECT COUNT(*) FROM avales_2026.personas WHERE dni IS NOT NULL")).scalar()
    sin_dni = c.execute(text("SELECT COUNT(*) FROM avales_2026.personas WHERE dni IS NULL")).scalar()
    con_mat = c.execute(text("SELECT COUNT(*) FROM avales_2026.personas WHERE tomo IS NOT NULL")).scalar()
    print(f"Total: {total} | con DNI: {con_dni} | sin DNI: {sin_dni} | con tomo/folio: {con_mat}")
    print("\nPor jurisdiccion:")
    for j, n in c.execute(text(
        "SELECT jurisdiccion, COUNT(*) FROM avales_2026.personas GROUP BY jurisdiccion ORDER BY 2 DESC"
    )):
        print(f"  {j}: {n}")
