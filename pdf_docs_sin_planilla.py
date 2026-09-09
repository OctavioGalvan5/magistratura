"""
PDF por jurisdiccion con las personas que tienen DNI/credencial pero NO planilla.

Estructura de cada PDF (dni_matricula.<jur>.pdf):
  1) Pagina 1: tabla con nombre, DNI, matricula (numerada #1, #2, ...)
  2) Despues, en el mismo orden, las imagenes de DNI/credencial de cada persona
     con un caption "#N - Nombre - DNI X" para poder ubicarlas rapido.

Excluye placeholders '(pendiente) ...' auto-creados por vision.

Uso:
    python pdf_docs_sin_planilla.py --db nacion
    python pdf_docs_sin_planilla.py --db nacion --upload   # sube a MinIO
    python pdf_docs_sin_planilla.py --db nacion --jur "Ciudad Autonoma"
"""
import os, io, re, sys, argparse, unicodedata
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from fpdf import FPDF
from PIL import Image
from minio import Minio
from db_config import resolve_db_url, DB_CHOICES

load_dotenv()
SCHEMA = "avales_2026"
BUCKET = "avales-eleccion-2026"
PAGE_W, PAGE_H = 210, 297
MARGIN = 12
MAX_IMG_PX = 1600

minio = Minio(
    os.environ["MINIO_ENDPOINT"],
    access_key=os.environ["MINIO_ACCESS_KEY"],
    secret_key=os.environ["MINIO_SECRET_KEY"],
    secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
)


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "sin_jurisdiccion").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    return s or "sin_jurisdiccion"


def safe(s):
    if s is None: return ""
    return str(s).encode("latin-1", "replace").decode("latin-1")


def matricula_str(row):
    if row.get("tomo") and row.get("folio"):
        return f"T{row['tomo']} F{row['folio']}"
    return row.get("matricula") or "-"


def download_minio(object_key: str) -> bytes:
    resp = minio.get_object(BUCKET, object_key)
    try:
        return resp.read()
    finally:
        resp.close(); resp.release_conn()


def downscale_for_pdf(image_bytes: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > MAX_IMG_PX:
            r = MAX_IMG_PX / long_edge
            img = img.resize((int(w * r), int(h * r)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception as e:
        print(f"    ! no pude procesar imagen: {e}")
        return b""


def add_image_page(pdf: "FPDF", jpg: bytes, caption: str):
    img = Image.open(io.BytesIO(jpg))
    iw, ih = img.size
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, safe(caption), new_x="LMARGIN", new_y="NEXT")
    avail_w = PAGE_W - 2 * MARGIN
    avail_h = PAGE_H - 2 * MARGIN - 8
    ratio = iw / ih
    if avail_w / avail_h > ratio:
        h_mm = avail_h; w_mm = h_mm * ratio
    else:
        w_mm = avail_w; h_mm = w_mm / ratio
    x = MARGIN + (avail_w - w_mm) / 2
    y = pdf.get_y() + 1
    pdf.image(io.BytesIO(jpg), x=x, y=y, w=w_mm, h=h_mm)


def build_pdf(jur: str, personas: list) -> bytes:
    """personas: list of dicts con 'persona' + 'docs' (list de fotos non-planilla)."""
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=MARGIN)

    # ── Pagina 1: tabla resumen ──
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, safe(jur), new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, safe(f"Personas con DNI/credencial sin planilla - {datetime.now():%d/%m/%Y}"),
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 5, safe(f"Total: {len(personas)} personas"),
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(4)

    cw = [10, 100, 30, 46]
    headers = ["#", "Nombre y Apellido", "DNI", "Matricula"]
    pdf.set_font("Helvetica", "B", 10)
    for w, h in zip(cw, headers):
        pdf.cell(w, 7, safe(h), border=1, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    for idx, it in enumerate(personas, 1):
        p = it["persona"]
        row_data = [
            str(idx),
            (p.get("nombre_apellido") or "")[:62],
            p.get("dni") or "-",
            matricula_str(p)[:28],
        ]
        for w, val in zip(cw, row_data):
            pdf.cell(w, 5.5, safe(val), border=1)
        pdf.ln()

    # ── Paginas siguientes: imagenes de DNI/credencial por persona ──
    for idx, it in enumerate(personas, 1):
        p = it["persona"]
        nombre = p.get("nombre_apellido") or "(sin nombre)"
        dni = p.get("dni") or "-"
        for f in it["docs"]:
            try:
                raw = download_minio(f["minio_object_key"])
                jpg = downscale_for_pdf(raw)
                if not jpg:
                    continue
                tipo_label = (f.get("tipo") or "").upper() or "DOC"
                caption = f"#{idx} - [{tipo_label}] {nombre} - DNI {dni}"
                add_image_page(pdf, jpg, caption)
            except Exception as e:
                print(f"    ! error foto id={f['id']} persona={nombre}: {e}")

    return bytes(pdf.output())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="julia", choices=DB_CHOICES)
    ap.add_argument("--jur", help="solo una jurisdiccion")
    ap.add_argument("--upload", action="store_true",
                    help="Subir los PDFs a MinIO en entregables/YYYYMMDD/<db>/")
    args = ap.parse_args()

    engine = create_engine(resolve_db_url(args.db), pool_pre_ping=True)

    where_jur = "AND p.jurisdiccion = :jur" if args.jur else ""
    params = {"jur": args.jur} if args.jur else {}

    sql_personas = f"""
    WITH tipos AS (
      SELECT p.id,
             BOOL_OR(f.tipo = 'planilla_aval') AS has_planilla,
             BOOL_OR(f.tipo IN ('dni','credencial') OR (f.tipo IS NULL)
                     OR (LOWER(f.tipo) = 'otro')) AS has_doc
      FROM {SCHEMA}.personas p
      JOIN {SCHEMA}.fotos_personas fp ON fp.persona_id = p.id
      JOIN {SCHEMA}.fotos f           ON f.id = fp.foto_id
      GROUP BY p.id
    )
    SELECT p.id, p.jurisdiccion, p.nombre_apellido, p.dni, p.matricula, p.tomo, p.folio
    FROM {SCHEMA}.personas p
    JOIN tipos t ON t.id = p.id
    WHERE t.has_planilla = FALSE AND t.has_doc = TRUE
      AND p.nombre_apellido NOT ILIKE '(pendiente)%'
      {where_jur}
    ORDER BY p.jurisdiccion NULLS LAST, p.nombre_apellido
    """
    sql_docs = f"""
    SELECT f.id, f.tipo, f.minio_object_key, f.filename_original
    FROM {SCHEMA}.fotos_personas fp
    JOIN {SCHEMA}.fotos f ON f.id = fp.foto_id
    WHERE fp.persona_id = :pid AND f.tipo <> 'planilla_aval'
    ORDER BY CASE f.tipo
               WHEN 'dni' THEN 0
               WHEN 'credencial' THEN 1
               ELSE 2
             END, f.uploaded_at
    """

    with engine.connect() as c:
        personas_raw = [dict(r) for r in c.execute(text(sql_personas), params).mappings()]
        if not personas_raw:
            print("No hay personas que cumplan el criterio.")
            return
        for p in personas_raw:
            p["_docs"] = [dict(f) for f in c.execute(text(sql_docs), {"pid": p["id"]}).mappings()]

    # Agrupar por jurisdiccion
    grupos: dict = {}
    for p in personas_raw:
        jur = p.get("jurisdiccion") or "SIN JURISDICCION"
        grupos.setdefault(jur, []).append({"persona": p, "docs": p["_docs"]})

    fecha = datetime.now().strftime("%Y%m%d")
    out_dir = Path(__file__).parent / f"dni_matricula_{args.db}_{fecha}"
    out_dir.mkdir(exist_ok=True)

    total_docs = sum(len(p["_docs"]) for p in personas_raw)
    print(f"DB: {args.db}   Total: {len(personas_raw)} personas   {total_docs} docs   {len(grupos)} jurisdicciones")
    print(f"Salida: {out_dir}")
    print("=" * 72)

    prefix = None
    if args.upload:
        prefix = f"entregables/{fecha}/{args.db}" if args.db != "julia" else f"entregables/{fecha}"

    for jur, items in sorted(grupos.items()):
        s = slug(jur)
        fname = f"dni_matricula.{s}.pdf"
        print(f"  {jur}: {len(items)} personas, generando...", end=" ", flush=True)
        pdf_bytes = build_pdf(jur, items)
        (out_dir / fname).write_bytes(pdf_bytes)
        info = f"OK {len(pdf_bytes)//1024} KB   {fname}"
        if args.upload:
            key = f"{prefix}/{fname}"
            minio.put_object(BUCKET, key, io.BytesIO(pdf_bytes), length=len(pdf_bytes),
                             content_type="application/pdf")
            info += "   [uploaded]"
        print(info)


if __name__ == "__main__":
    main()
