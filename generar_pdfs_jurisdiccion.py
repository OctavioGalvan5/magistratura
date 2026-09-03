"""
Genera 3 PDFs por jurisdicción:

  1) avales.<jur>.completo.YYYYMMDD.pdf
     Personas con aval completo (planilla + DNI). Incluye TODAS sus fotos.

  2) avales.<jur>.todos.YYYYMMDD.pdf
     Todas las personas con al menos 1 foto vinculada. Incluye TODAS sus fotos.

  3) avales.<jur>.documentacion.YYYYMMDD.pdf
     Personas con al menos 1 DNI u "otro" adjunto. Incluye SOLO fotos DNI+otros
     (sin planillas). Sirve para archivar la documentación de identidad.

Estructura de cada PDF:
  Página 1: portada con título + tabla resumen de personas incluidas.
  Después, por cada persona:
    - Página con datos de la persona (nombre, DNI, matrícula, jurisdicción).
    - Una página por cada foto vinculada, escalada a A4.

Los PDFs se suben a MinIO en entregables/YYYYMMDD/ con URLs presignadas de 24h.
Se genera un CSV con el índice de todos los PDFs subidos.

Uso:
    python generar_pdfs_jurisdiccion.py                    # genera las 3 variantes de todas
    python generar_pdfs_jurisdiccion.py --tipo docs        # solo la variante docs
    python generar_pdfs_jurisdiccion.py --tipo completo    # solo la variante completo
    python generar_pdfs_jurisdiccion.py --jur "Salta"      # solo esa jurisdicción
    python generar_pdfs_jurisdiccion.py --dry-run          # muestra qué haría
"""
import os
import io
import sys
import re
import csv
import argparse
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from minio import Minio
from PIL import Image
from fpdf import FPDF

load_dotenv()
engine = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)
minio = Minio(
    os.environ["MINIO_ENDPOINT"],
    access_key=os.environ["MINIO_ACCESS_KEY"],
    secret_key=os.environ["MINIO_SECRET_KEY"],
    secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
)
BUCKET = "avales-eleccion-2026"
SCHEMA = "avales_2026"

PAGE_W, PAGE_H = 210, 297  # A4 en mm
MARGIN = 12
MAX_IMG_PX = 1600

TIPOS_VALIDOS = ("completo", "todos", "docs", "entrega")


# ─── Utils ─────────────────────────────────────────────────────

def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "sin_jurisdiccion").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    return s or "sin_jurisdiccion"


def safe(s):
    """Codifica a Latin-1 para la fuente Helvetica built-in."""
    if s is None:
        return ""
    return str(s).encode("latin-1", "replace").decode("latin-1")


def q(sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).mappings().all()


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


# ─── Data ──────────────────────────────────────────────────────

def cargar_personas(jur_filter: str | None):
    """Devuelve {jurisdiccion: [ {persona, fotos, has_planilla, has_dni, has_otro, completo} ]}."""
    where = "WHERE p.jurisdiccion = :jur" if jur_filter else ""
    params = {"jur": jur_filter} if jur_filter else {}

    personas = q(f"""
        SELECT p.id, p.nombre_apellido, p.dni, p.matricula, p.tomo, p.folio,
               p.jurisdiccion, p.observaciones,
               (p.observaciones ILIKE 'Creada auto%%') AS auto_creada
        FROM {SCHEMA}.personas p
        {where}
        ORDER BY p.jurisdiccion NULLS LAST, p.nombre_apellido
    """, **params)

    resultado: dict = {}
    for p in personas:
        fotos = q(f"""
            SELECT f.id, f.tipo, f.match_status, f.minio_object_key, f.content_type,
                   f.filename_original
            FROM {SCHEMA}.fotos_personas fp
            JOIN {SCHEMA}.fotos f ON f.id = fp.foto_id
            WHERE fp.persona_id = :pid
            ORDER BY CASE f.tipo
                       WHEN 'planilla_aval' THEN 0
                       WHEN 'dni' THEN 1
                       ELSE 2
                     END, f.uploaded_at
        """, pid=p["id"])
        if not fotos:
            continue
        has_planilla = any(f["tipo"] == "planilla_aval" for f in fotos)
        has_dni = any(f["tipo"] == "dni" for f in fotos)
        has_otro = any((f["tipo"] or "").lower() in ("otro", "") for f in fotos)
        jur = p["jurisdiccion"] or "SIN JURISDICCION"
        resultado.setdefault(jur, []).append({
            "persona": dict(p),
            "fotos": [dict(f) for f in fotos],
            "has_planilla": has_planilla,
            "has_dni": has_dni,
            "has_otro": has_otro,
            "completo": has_planilla and has_dni,
        })
    return resultado


def filtrar_para_tipo(items: list, tipo: str) -> list:
    """Devuelve items adaptados a la variante:
       - completo:   solo personas con aval completo; todas sus fotos.
       - todos:      todas las personas con >=1 foto; todas sus fotos.
       - docs:       personas con >=1 DNI u 'otro'; SOLO fotos dni+otro.
       - instructivo:personas que aparecen en al menos 1 planilla de esa jurisdicción;
                     el PDF final agrupa primero PLANILLAS, luego DOCUMENTACION.
    """
    if tipo == "completo":
        return [x for x in items if x["completo"]]
    if tipo == "todos":
        return items
    if tipo == "docs":
        salida = []
        for x in items:
            docs = [f for f in x["fotos"] if (f["tipo"] or "").lower() != "planilla_aval"]
            if not docs:
                continue
            copia = dict(x)
            copia["fotos"] = docs
            salida.append(copia)
        return salida
    if tipo == "entrega":
        # Formato de entrega (avales.<jur>.pdf): personas que aparecen en al menos
        # una planilla de la jurisdicción. El PDF incluye primero todas las planillas
        # únicas y después solo los DNIs+otros de las personas que aportaron algo.
        return [x for x in items if x["has_planilla"]]
    raise ValueError(f"tipo invalido: {tipo}")


# ─── PDF ───────────────────────────────────────────────────────

TITULOS = {
    "completo":      "AVAL COMPLETO (planilla + DNI)",
    "todos":         "TODOS (planilla + DNI + otros)",
    "docs":          "DOCUMENTACION (solo DNI + otros)",
    "entrega":       "AVAL (planillas + documentacion) - formato de entrega",
}


def add_image_page(pdf: "FPDF", jpg: bytes, caption: str):
    """Agrega una página A4 con la imagen escalada y un caption arriba."""
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


def build_pdf_entrega(_jur: str, items: list) -> bytes:
    """
    Formato de entrega pedido por el instructivo (archivo final avales.<jur>.pdf):
      1) TODAS las planillas firmadas (una por página, deduplicadas)
      2) TODOS los DNIs + otros adjuntos de los firmantes que aportaron algo
    Sin portada, sin tabla, sin páginas separadoras, sin marcas de faltantes.
    """
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=MARGIN)

    # 1) Planillas únicas de todas las personas de esta jurisdicción
    planillas: dict = {}
    for it in items:
        for f in it["fotos"]:
            if f["tipo"] == "planilla_aval" and f["id"] not in planillas:
                planillas[f["id"]] = f

    for f in planillas.values():
        try:
            raw = download_minio(f["minio_object_key"])
            jpg = downscale_for_pdf(raw)
            if jpg:
                add_image_page(pdf, jpg, f"PLANILLA - {f.get('filename_original', '')}")
        except Exception as e:
            print(f"    ! error planilla id={f['id']}: {e}")

    # 2) DNIs + otros, agrupados por persona (solo las que aportaron algo)
    for it in items:
        p = it["persona"]
        docs = [f for f in it["fotos"] if f["tipo"] != "planilla_aval"]
        if not docs:
            continue
        for f in docs:
            try:
                raw = download_minio(f["minio_object_key"])
                jpg = downscale_for_pdf(raw)
                if jpg:
                    tipo_label = (f.get("tipo") or "sin_tipo").upper()
                    add_image_page(pdf, jpg,
                                   f"[{tipo_label}] {p.get('nombre_apellido')} - DNI {p.get('dni') or '-'}")
            except Exception as e:
                print(f"    ! error doc id={f['id']}: {e}")

    return bytes(pdf.output())


def build_pdf(jur: str, items: list, tipo: str) -> bytes:
    if tipo == "entrega":
        return build_pdf_entrega(jur, items)

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=MARGIN)

    # ── PORTADA ──
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, safe(f"AVALES - {jur}"), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, safe(f"Elecciones Consejo de la Magistratura 2026 - {datetime.now():%d/%m/%Y}"),
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, safe(f"Variante: {TITULOS[tipo]}  -  Personas: {len(items)}"),
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Tabla resumen
    pdf.set_font("Helvetica", "B", 9)
    cw = [8, 60, 22, 30, 18, 18, 18]
    headers = ["#", "Nombre", "DNI", "Matricula", "Planilla", "DNI adj.", "Otros"]
    for w, h in zip(cw, headers):
        pdf.cell(w, 6, safe(h), border=1, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 8)
    for idx, it in enumerate(items, 1):
        p = it["persona"]
        mat = (f"T{p['tomo']} F{p['folio']}" if p.get("tomo") and p.get("folio")
               else (p.get("matricula") or "-"))
        row = [
            str(idx),
            (p.get("nombre_apellido") or "")[:38],
            p.get("dni") or "-",
            mat[:18],
            "SI" if it["has_planilla"] else "NO",
            "SI" if it["has_dni"] else "NO",
            "SI" if it["has_otro"] else "NO",
        ]
        for w, val in zip(cw, row):
            pdf.cell(w, 5, safe(val), border=1)
        pdf.ln()

    # ── PERSONAS ──
    for idx, it in enumerate(items, 1):
        p = it["persona"]
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, safe(f"{idx}. {p.get('nombre_apellido')}"),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        mat = (f"T{p['tomo']} F{p['folio']}" if p.get("tomo") and p.get("folio")
               else (p.get("matricula") or "-"))
        info = f"DNI: {p.get('dni') or '-'}   |   Matricula: {mat}   |   Jurisdiccion: {p.get('jurisdiccion') or '-'}"
        pdf.cell(0, 6, safe(info), new_x="LMARGIN", new_y="NEXT")
        flags = (f"Planilla: {'SI' if it['has_planilla'] else 'NO'}   "
                 f"DNI adjunto: {'SI' if it['has_dni'] else 'NO'}")
        if p.get("auto_creada"):
            flags += "   (persona auto-creada por vision)"
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 5, safe(flags), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

        for f in it["fotos"]:
            try:
                raw = download_minio(f["minio_object_key"])
                jpg = downscale_for_pdf(raw)
                if not jpg:
                    continue
                img = Image.open(io.BytesIO(jpg))
                iw, ih = img.size
                pdf.add_page()
                pdf.set_font("Helvetica", "B", 10)
                tipo_label = (f.get("tipo") or "sin_tipo").upper()
                pdf.cell(0, 6, safe(f"[{tipo_label}] {p.get('nombre_apellido')} - {f.get('filename_original')}"),
                         new_x="LMARGIN", new_y="NEXT")
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
            except Exception as e:
                print(f"    ! error insertando foto id={f['id']}: {e}")
                continue

    return bytes(pdf.output())


def upload_pdf(data: bytes, object_key: str) -> str:
    minio.put_object(BUCKET, object_key, io.BytesIO(data), length=len(data),
                     content_type="application/pdf")
    return minio.presigned_get_object(BUCKET, object_key, expires=timedelta(hours=24))


# ─── Main ──────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jur", help="solo una jurisdiccion")
    ap.add_argument("--tipo", choices=TIPOS_VALIDOS,
                    help="solo una variante (default: las 3)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    fecha = datetime.now().strftime("%Y%m%d")
    prefix = f"entregables/{fecha}"
    tipos = [args.tipo] if args.tipo else list(TIPOS_VALIDOS)

    grupos = cargar_personas(args.jur)
    if not grupos:
        print("No hay personas con fotos vinculadas.")
        return

    print(f"Jurisdicciones a procesar: {len(grupos)}")
    print(f"Variantes: {', '.join(tipos)}")
    print(f"Prefix MinIO: {prefix}")
    print("=" * 72)

    resultados = []
    for jur, items in sorted(grupos.items()):
        s = slug(jur)
        print(f"\n[{jur}]  total: {len(items)}")

        for tipo in tipos:
            subset = filtrar_para_tipo(items, tipo)
            if not subset:
                print(f"  ({tipo}) 0 personas, se omite")
                continue
            fname = f"avales.{s}.pdf" if tipo == "entrega" else f"avales.{s}.{tipo}.pdf"
            key = f"{prefix}/{fname}"
            if args.dry_run:
                print(f"  ({tipo}) DRY -> {key}  [{len(subset)} personas]")
                continue
            print(f"  ({tipo}) generando con {len(subset)} personas...", end=" ", flush=True)
            pdf_bytes = build_pdf(jur, subset, tipo)
            url = upload_pdf(pdf_bytes, key)
            print(f"OK  {len(pdf_bytes)//1024} KB")
            print(f"    URL 24h: {url}")
            resultados.append({
                "jurisdiccion": jur, "variante": tipo, "personas": len(subset),
                "kb": len(pdf_bytes) // 1024, "minio_key": key, "url": url,
            })

    if resultados and not args.dry_run:
        print("\n" + "=" * 72)
        print(f"Total PDFs generados: {len(resultados)}")
        idx_path = Path(__file__).parent / f"entregables_{fecha}.csv"
        with idx_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["jurisdiccion", "variante", "personas", "kb", "minio_key", "url"])
            w.writeheader(); w.writerows(resultados)
        print(f"Indice CSV: {idx_path}")


if __name__ == "__main__":
    main()
