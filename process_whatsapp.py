"""
Procesa archivos de la carpeta WhatsApp Chat - Avales CMF:
- Imágenes JPG/PNG/WEBP → analiza directo con visión, sube a MinIO.
- PDFs → renderiza cada página como JPG y analiza cada una por separado.
- HEIC → sube tal cual sin análisis.

Para cada imagen/página, la visión clasifica en:
  - "dni"           → 1 persona detectada
  - "planilla_aval" → N personas detectadas (cada fila de la tabla)
  - "otro"          → 0 personas

Por cada persona detectada:
  - Match por DNI. Si existe → vincula la foto y ENRIQUECE los campos vacíos
    (matrícula, tomo, folio, jurisdicción, género).
  - Si no existe → crea la persona con lo detectado.
  - Se registra en avales_2026.fotos_personas (many-to-many).

Idempotencia: source_file_sha256 evita reprocesar el mismo archivo fuente.

Uso:
    python process_whatsapp.py                    # procesa todo
    python process_whatsapp.py --limit 5         # solo los primeros N
    python process_whatsapp.py --no-analyze      # sube sin OCR
    python process_whatsapp.py --dry-run         # lista sin hacer nada
    python process_whatsapp.py --force           # reprocesa aunque ya se hizo
    python process_whatsapp.py --only <texto>    # solo archivos cuyo nombre contenga texto
"""
import os
import io
import sys
import time
import json
import argparse
import hashlib
from pathlib import Path

# UTF-8 en consola Windows
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).parent / "app"))

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from minio import Minio
from minio.error import S3Error
import pypdfium2 as pdfium

from vision import analyze_image  # nuevo API multi-persona

load_dotenv()

SCHEMA = "avales_2026"
BUCKET = "avales-eleccion-2026"
FOLDER = Path(r"c:\Users\octav\Downloads\Consejo de la magistratura\WhatsApp Chat - Avales CMF")

engine = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)
minio = Minio(
    os.environ["MINIO_ENDPOINT"],
    access_key=os.environ["MINIO_ACCESS_KEY"],
    secret_key=os.environ["MINIO_SECRET_KEY"],
    secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
PDF_EXTS = {".pdf"}
PASSTHROUGH_EXTS = {".heic"}
SKIP_EXTS = {".doc", ".docx", ".txt", ".opus", ".mp3", ".mp4"}

# Campos de personas que se pueden enriquecer (solo si actualmente están vacíos)
ENRIQUECIBLES = ("nombre_apellido", "genero", "matricula", "tomo", "folio", "jurisdiccion")


# ─── Helpers DB / MinIO ───

def exec_sql(sql, **params):
    with engine.begin() as c:
        return c.execute(text(sql), params)

def q_one(sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).mappings().first()

def q_one_write(sql, **params):
    with engine.begin() as c:
        return c.execute(text(sql), params).mappings().first()


def upload_to_minio(data: bytes, ext: str, content_type: str) -> tuple[str, str]:
    sha = hashlib.sha256(data).hexdigest()
    object_key = f"originales/{sha[:2]}/{sha}{ext}"
    try:
        minio.stat_object(BUCKET, object_key)
    except S3Error:
        minio.put_object(BUCKET, object_key, io.BytesIO(data), length=len(data),
                         content_type=content_type)
    return object_key, sha


def pdf_pages_as_jpg(pdf_bytes: bytes, dpi: int = 200, jpeg_quality: int = 85):
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            pil = page.render(scale=dpi / 72).to_pil()
            if pil.mode != "RGB":
                pil = pil.convert("RGB")
            buf = io.BytesIO()
            pil.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
            yield i + 1, buf.getvalue()
    finally:
        pdf.close()


def source_already_processed(source_sha: str) -> bool:
    r = q_one(f"SELECT 1 AS x FROM {SCHEMA}.fotos WHERE source_file_sha256 = :s LIMIT 1", s=source_sha)
    return r is not None


# ─── Match & Enrich ───

def _find_or_create_persona(det: dict) -> tuple[int, bool, list[str]]:
    """
    Recibe un dict con datos detectados (dni, nombre_apellido, genero, tomo, folio,
    matricula, jurisdiccion). Devuelve (persona_id, creada, campos_enriquecidos).
    """
    dni = det.get("dni")
    if not dni:
        raise ValueError("_find_or_create_persona requiere dni")

    existing = q_one(f"""
        SELECT id, nombre_apellido, genero, matricula, tomo, folio, jurisdiccion
        FROM {SCHEMA}.personas WHERE dni = :d
    """, d=dni)

    if existing:
        # Enriquecer campos vacíos
        updates = {}
        for k in ENRIQUECIBLES:
            actual = existing[k]
            nuevo = det.get(k)
            if nuevo and not actual:
                updates[k] = nuevo
        if updates:
            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            params = {**updates, "id": existing["id"]}
            exec_sql(f"UPDATE {SCHEMA}.personas SET {set_clause} WHERE id = :id", **params)
        return existing["id"], False, list(updates.keys())

    # Crear persona nueva
    new_p = q_one_write(f"""
        INSERT INTO {SCHEMA}.personas
          (nombre_apellido, dni, genero, matricula, tomo, folio, jurisdiccion, observaciones)
        VALUES (:n, :d, :g, :m, :t, :f, :j, :obs)
        RETURNING id
    """,
        n=det.get("nombre_apellido") or f"(pendiente) DNI {dni}",
        d=dni,
        g=det.get("genero"),
        m=det.get("matricula"),
        t=det.get("tomo"),
        f=det.get("folio"),
        j=det.get("jurisdiccion"),
        obs="Creada automáticamente desde visión",
    )
    return new_p["id"], True, list(ENRIQUECIBLES)  # todos los campos "creados"


def _link_foto_persona(foto_id: int, persona_id: int, det: dict, creada: bool, campos: list[str]):
    exec_sql(f"""
        INSERT INTO {SCHEMA}.fotos_personas
          (foto_id, persona_id, dni_detectado, nombre_detectado, persona_creada, campos_enriquecidos)
        VALUES (:f, :p, :d, :n, :c, :ce)
        ON CONFLICT (foto_id, persona_id) DO NOTHING
    """,
        f=foto_id, p=persona_id,
        d=det.get("dni"), n=det.get("nombre_apellido"),
        c=creada, ce=",".join(campos) if campos else None,
    )


# ─── Persistencia principal ───

def persist_and_analyze(*, data: bytes, filename: str, content_type: str, ext: str,
                        analyze: bool, source_file_sha256: str) -> dict:
    """Sube la foto, la analiza y linkea todas las personas detectadas."""
    object_key, sha = upload_to_minio(data, ext, content_type)

    existing_foto = q_one(f"SELECT id, tipo FROM {SCHEMA}.fotos WHERE minio_object_key=:k", k=object_key)
    if existing_foto:
        return {"status": "duplicado_contenido"}

    ocr = None
    tipo = None
    personas_det = []
    if analyze:
        ocr = analyze_image(data, content_type or "")
        tipo = ocr.get("tipo")
        personas_det = [p for p in ocr.get("personas", []) if p.get("dni")]  # necesitamos DNI para linkear

    # Determinar match_status agregado
    if not analyze:
        match_status = "pendiente"
    elif not personas_det:
        match_status = "sin_match"
    else:
        match_status = "matched"

    foto_row = q_one_write(f"""
        INSERT INTO {SCHEMA}.fotos
          (filename_original, minio_bucket, minio_object_key, content_type, size_bytes, sha256,
           source_file_sha256, tipo, match_status, raw_ocr, processed_at)
        VALUES
          (:fn, :bk, :ok, :ct, :sz, :sh, :src, :tp, :ms, CAST(:raw AS JSONB),
           CASE WHEN :raw IS NULL THEN NULL ELSE now() END)
        RETURNING id
    """,
        fn=filename, bk=BUCKET, ok=object_key, ct=content_type, sz=len(data), sh=sha,
        src=source_file_sha256, tp=tipo, ms=match_status,
        raw=json.dumps(ocr, ensure_ascii=False) if ocr else None,
    )
    foto_id = foto_row["id"]

    creadas = 0
    enriquecidas = 0
    linked = []
    for det in personas_det:
        try:
            pid, creada, campos = _find_or_create_persona(det)
            _link_foto_persona(foto_id, pid, det, creada, campos)
            if creada: creadas += 1
            elif campos: enriquecidas += 1
            linked.append({"id": pid, "creada": creada, "campos": campos, "det": det})
        except Exception as e:
            print(f"    error vinculando persona {det.get('dni')}: {e}")

    return {
        "status": "ok",
        "foto_id": foto_id,
        "tipo": tipo,
        "match_status": match_status,
        "personas": linked,
        "creadas": creadas,
        "enriquecidas": enriquecidas,
        "provider": ocr.get("_provider") if ocr else None,
    }


# ─── Loop por archivo ───

def _report(tag: str, r: dict):
    if r["status"] == "duplicado_contenido":
        print(f"  {tag} → duplicado (mismo contenido ya subido)")
        return
    tipo = r.get("tipo") or "n/a"
    n = len(r.get("personas", []))
    header = f"  {tag} → [{tipo}] {r['match_status']}"
    if n == 0:
        print(header)
    else:
        print(f"{header} · {n} persona(s):")
        for p in r["personas"]:
            det = p["det"]
            flags = []
            if p["creada"]: flags.append("NUEVA")
            elif p["campos"]: flags.append(f"enriquecida[{','.join(p['campos'])}]")
            print(f"      #{p['id']} DNI {det.get('dni')} {det.get('nombre_apellido') or ''} "
                  f"{' '.join(flags)}")


def process_file(path: Path, *, analyze: bool, dry_run: bool, force: bool = False) -> dict:
    ext = path.suffix.lower()
    tag = f"[{path.name}]"

    if ext in SKIP_EXTS:
        print(f"{tag} SKIP ({ext})")
        return {"skipped": 1}

    if dry_run:
        print(f"{tag} DRY")
        return {"dry": 1}

    data = path.read_bytes()
    source_sha = hashlib.sha256(data).hexdigest()

    if not force and source_already_processed(source_sha):
        print(f"  {tag} → ya procesado antes (skip)")
        return {"ya_procesado": 1}

    stats = {"archivos": 1, "matched": 0, "sin_match": 0, "creadas": 0, "enriquecidas": 0,
             "duplicadas": 0, "paginas": 0}

    def _acc(r):
        if r["status"] == "duplicado_contenido":
            stats["duplicadas"] += 1
            return
        stats[r["match_status"]] = stats.get(r["match_status"], 0) + 1
        stats["creadas"] += r.get("creadas", 0)
        stats["enriquecidas"] += r.get("enriquecidas", 0)

    if ext in IMAGE_EXTS:
        ct = f"image/{'jpeg' if ext in ('.jpg', '.jpeg') else ext[1:]}"
        r = persist_and_analyze(data=data, filename=path.name, content_type=ct, ext=ext,
                                analyze=analyze, source_file_sha256=source_sha)
        _report(tag, r); _acc(r)
        return stats

    if ext in PASSTHROUGH_EXTS:
        r = persist_and_analyze(data=data, filename=path.name, content_type="image/heic",
                                ext=ext, analyze=False, source_file_sha256=source_sha)
        _report(tag + " (heic)", r); _acc(r)
        return stats

    if ext in PDF_EXTS:
        upload_to_minio(data, ext, "application/pdf")  # guardar PDF original
        try:
            for page_num, jpg_bytes in pdf_pages_as_jpg(data):
                fname = f"{path.stem}__p{page_num}.jpg"
                r = persist_and_analyze(
                    data=jpg_bytes, filename=fname, content_type="image/jpeg",
                    ext=".jpg", analyze=analyze, source_file_sha256=source_sha,
                )
                _report(f"{tag} p{page_num}", r); _acc(r)
                stats["paginas"] += 1
        except Exception as e:
            print(f"{tag} ERROR PDF: {e}")
        return stats

    print(f"{tag} SKIP (ext desconocido: {ext})")
    return {"skipped": 1}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-analyze", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    files = sorted(FOLDER.iterdir())
    if args.only:
        files = [f for f in files if args.only.lower() in f.name.lower()]
    if args.limit:
        files = files[: args.limit]

    print(f"Archivos: {len(files)}  Analyze={not args.no_analyze}  Force={args.force}")
    print("-" * 72)

    totals: dict = {}
    t0 = time.time()
    for i, f in enumerate(files, 1):
        if not f.is_file(): continue
        print(f"[{i}/{len(files)}] {f.name}")
        try:
            r = process_file(f, analyze=not args.no_analyze, dry_run=args.dry_run, force=args.force)
            for k, v in r.items():
                totals[k] = totals.get(k, 0) + v
        except KeyboardInterrupt:
            print("\nInterrumpido por usuario.")
            break
        except Exception as e:
            print(f"  ERROR: {e}")
            totals["errores"] = totals.get("errores", 0) + 1

    print("-" * 72)
    print(f"Terminado en {time.time()-t0:.1f}s")
    for k, v in sorted(totals.items()):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
