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
    python process_whatsapp.py                                  # procesa todo (carpeta WhatsApp default, DB julia)
    python process_whatsapp.py --db nacion                      # trabaja sobre la DB 'nacion'
    python process_whatsapp.py --db nacion --folder "COLEGIO DE CORDOBA" --jurisdiccion Cordoba
    python process_whatsapp.py --folder "Avales" --por-subcarpeta  # cada subcarpeta = jurisdiccion
    python process_whatsapp.py --folder "path/a/otra/carpeta"   # procesa otra carpeta
    python process_whatsapp.py --jurisdiccion "Cordoba"         # todas las nuevas personas → Cordoba
    python process_whatsapp.py --model sonnet                   # opus | sonnet (default) | haiku
    python process_whatsapp.py --limit 5                        # solo los primeros N
    python process_whatsapp.py --no-analyze                     # sube sin OCR
    python process_whatsapp.py --dry-run                        # lista sin hacer nada
    python process_whatsapp.py --force                          # reprocesa aunque ya se hizo
    python process_whatsapp.py --only <texto>                   # solo archivos cuyo nombre contenga texto
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

from vision import analyze_image, analyze_pages, set_model as set_vision_model
from db_config import resolve_db_url, DB_CHOICES

load_dotenv()

SCHEMA = "avales_2026"
BUCKET = "avales-eleccion-2026"
FOLDER_DEFAULT = Path(r"c:\Users\octav\Downloads\Consejo de la magistratura\WhatsApp Chat - Avales CMF")

# Se sobreescribe desde CLI. Si tiene valor, las personas detectadas sin jurisdicción
# reciben esta como default. NO pisa la que ya trae una persona existente en la DB
# ni pisa la que la visión sí detectó desde una planilla.
DEFAULT_JURISDICCION: str | None = None

# engine se inicializa en main() según el flag --db para no crear conexión al importar.
engine = None  # se inicializa en main() con la URL segun --db

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

def _find_or_create_persona(det: dict, default_jur: str | None = None) -> tuple[int, bool, list[str]]:
    """
    Recibe un dict con datos detectados por visión (dni, nombre_apellido, genero,
    tomo, folio, matricula, jurisdiccion). Devuelve (persona_id, creada, campos_enriquecidos).

    Match: prioridad DNI. Si no hay DNI pero hay tomo+folio (típico de credenciales
    de matrícula que no muestran DNI), busca por tomo+folio como fallback.

    default_jur: nombre-de-carpeta a usar SOLO como fallback cuando la visión NO detectó
    jurisdicción (típicamente en DNIs/credenciales que no muestran esa info). NO pisa
    una jurisdicción ya cargada en la persona.

    Prioridad para jurisdiccion en enrichment de persona existente:
      1. det.jurisdiccion (real, viene de una planilla) → OVERRIDE aunque exista otra
         (así el DNI procesado primero con fallback se corrige cuando llega la planilla)
      2. det sin jurisdicción, existing vacia, default_jur presente → fill con default_jur
      3. det sin jurisdicción, existing con valor → no toca
    """
    dni = det.get("dni")
    tomo = det.get("tomo")
    folio = det.get("folio")

    if not dni and not (tomo and folio):
        raise ValueError("_find_or_create_persona necesita dni o tomo+folio")

    if dni:
        existing = q_one(f"""
            SELECT id, nombre_apellido, dni, genero, matricula, tomo, folio, jurisdiccion
            FROM {SCHEMA}.personas WHERE dni = :d
        """, d=dni)
    else:
        # Fallback por matrícula (tomo + folio). Puede ser ambiguo entre jurisdicciones,
        # así que si el det trae jurisdicción la usamos como desempate.
        jur_para_match = det.get("jurisdiccion") or default_jur
        if jur_para_match:
            existing = q_one(f"""
                SELECT id, nombre_apellido, dni, genero, matricula, tomo, folio, jurisdiccion
                FROM {SCHEMA}.personas
                WHERE tomo = :t AND folio = :f AND jurisdiccion = :j
                LIMIT 1
            """, t=tomo, f=folio, j=jur_para_match)
        else:
            existing = q_one(f"""
                SELECT id, nombre_apellido, dni, genero, matricula, tomo, folio, jurisdiccion
                FROM {SCHEMA}.personas
                WHERE tomo = :t AND folio = :f
                LIMIT 1
            """, t=tomo, f=folio)

    if existing:
        updates = {}
        for k in ENRIQUECIBLES:
            actual = existing[k]
            nuevo = det.get(k)
            if k == "jurisdiccion":
                # 1) det trae jurisdiccion real (de una planilla): override si difiere
                if nuevo and nuevo != actual:
                    updates[k] = nuevo
                # 2) det NO trae, existing vacia, hay default_jur (folder fallback)
                elif not nuevo and not actual and default_jur:
                    updates[k] = default_jur
                continue
            # Otros campos: solo fill si esta vacio (comportamiento clásico)
            if nuevo and not actual:
                updates[k] = nuevo
        if updates:
            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            params = {**updates, "id": existing["id"]}
            exec_sql(f"UPDATE {SCHEMA}.personas SET {set_clause} WHERE id = :id", **params)
        return existing["id"], False, list(updates.keys())

    # Crear persona nueva. Prioridad jurisdiccion: det → default_jur → None.
    jur_a_insertar = det.get("jurisdiccion") or default_jur
    fallback_nombre = (
        det.get("nombre_apellido")
        or (f"(pendiente) DNI {dni}" if dni else f"(pendiente) T{tomo} F{folio}")
    )
    new_p = q_one_write(f"""
        INSERT INTO {SCHEMA}.personas
          (nombre_apellido, dni, genero, matricula, tomo, folio, jurisdiccion, observaciones)
        VALUES (:n, :d, :g, :m, :t, :f, :j, :obs)
        RETURNING id
    """,
        n=fallback_nombre,
        d=dni,
        g=det.get("genero"),
        m=det.get("matricula"),
        t=det.get("tomo"),
        f=det.get("folio"),
        j=jur_a_insertar,
        obs="Creada automáticamente desde visión",
    )
    return new_p["id"], True, list(ENRIQUECIBLES)


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
        # Necesitamos DNI o (tomo+folio) para poder matchear/crear una persona.
        # Las credenciales muchas veces no muestran DNI pero sí muestran tomo+folio.
        personas_det = [
            p for p in ocr.get("personas", [])
            if p.get("dni") or (p.get("tomo") and p.get("folio"))
        ]
        # NO inyectamos DEFAULT_JURISDICCION en el det. Se pasa aparte a
        # _find_or_create_persona para que la jurisdiccion detectada por vision
        # tenga prioridad sobre el fallback de nombre-de-carpeta.

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
            pid, creada, campos = _find_or_create_persona(det, default_jur=DEFAULT_JURISDICCION)
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


def process_file(path: Path, *, analyze: bool, dry_run: bool, force: bool = False,
                 root_folder: Path | None = None) -> dict:
    """
    root_folder: si se pasa, el filename_original que se guarda en la DB incluye
    el path relativo desde ese root (ej: 'COLEGIO DE BELL VILLE/aval.pdf'). Sirve
    para saber de que subcarpeta vino una foto que quedo sin match.
    """
    ext = path.suffix.lower()
    # Nombre a guardar en la DB: relativo al root, o solo el basename si no hay root.
    rel_name = str(path.relative_to(root_folder)).replace("\\", "/") if root_folder else path.name
    rel_stem = rel_name.rsplit(".", 1)[0] if "." in rel_name else rel_name
    tag = f"[{rel_name}]"

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
        r = persist_and_analyze(data=data, filename=rel_name, content_type=ct, ext=ext,
                                analyze=analyze, source_file_sha256=source_sha)
        _report(tag, r); _acc(r)
        return stats

    if ext in PASSTHROUGH_EXTS:
        r = persist_and_analyze(data=data, filename=rel_name, content_type="image/heic",
                                ext=ext, analyze=False, source_file_sha256=source_sha)
        _report(tag + " (heic)", r); _acc(r)
        return stats

    if ext in PDF_EXTS:
        upload_to_minio(data, ext, "application/pdf")  # guardar PDF original
        try:
            pages = list(pdf_pages_as_jpg(data))  # [(page_num, jpg), ...]
            stats["paginas"] = len(pages)

            if not analyze:
                # Sin análisis: subir cada pagina como foto pendiente sin link
                for page_num, jpg in pages:
                    fname = f"{rel_stem}__p{page_num}.jpg"
                    r = persist_and_analyze(
                        data=jpg, filename=fname, content_type="image/jpeg",
                        ext=".jpg", analyze=False, source_file_sha256=source_sha,
                    )
                    _report(f"{tag} p{page_num}", r); _acc(r)
                return stats

            # ── Análisis multi-pagina: 1 sola llamada a la vision con TODAS las paginas ──
            analysis = analyze_pages([jpg for _, jpg in pages])
            documentos = analysis.get("documentos", [])
            covered_pages: set[int] = set()

            for doc in documentos:
                tipo = doc.get("tipo") or "otro"
                doc_pages = doc.get("paginas", [])
                covered_pages.update(doc_pages)

                personas_det = [
                    p for p in doc.get("personas", [])
                    if p.get("dni") or (p.get("tomo") and p.get("folio"))
                ]
                # NO inyectamos DEFAULT_JURISDICCION en el det. Se pasa a
                # _find_or_create_persona como default_jur para que la jurisdiccion
                # detectada por vision (real) tenga prioridad sobre el fallback.

                # Resolver/crear personas del documento (una sola vez)
                persona_records: list = []
                for det in personas_det:
                    try:
                        pid, creada, campos = _find_or_create_persona(
                            det, default_jur=DEFAULT_JURISDICCION
                        )
                        persona_records.append((pid, det, creada, campos))
                        if creada:  stats["creadas"] += 1
                        elif campos: stats["enriquecidas"] += 1
                    except Exception as e:
                        print(f"    error persona {det.get('dni') or det.get('tomo')}: {e}")

                match_status = "matched" if persona_records else "sin_match"
                if persona_records: stats["matched"] += len(doc_pages)
                else:               stats["sin_match"] += len(doc_pages)

                # Subir cada pagina del documento y linkearla a TODAS las personas del doc
                for page_num in doc_pages:
                    jpg = next((j for pn, j in pages if pn == page_num), None)
                    if jpg is None: continue
                    fname = f"{rel_stem}__p{page_num}.jpg"
                    obj_key, page_sha = upload_to_minio(jpg, ".jpg", "image/jpeg")

                    existing = q_one(f"SELECT id FROM {SCHEMA}.fotos WHERE minio_object_key=:k", k=obj_key)
                    if existing:
                        foto_id = existing["id"]
                        exec_sql(f"""
                            UPDATE {SCHEMA}.fotos SET tipo=:tp, match_status=:ms,
                              raw_ocr = CAST(:raw AS JSONB), processed_at = now(),
                              source_file_sha256 = COALESCE(source_file_sha256, :src)
                            WHERE id = :id
                        """, id=foto_id, tp=tipo, ms=match_status,
                             raw=json.dumps(doc, ensure_ascii=False), src=source_sha)
                    else:
                        foto = q_one_write(f"""
                            INSERT INTO {SCHEMA}.fotos
                              (filename_original, minio_bucket, minio_object_key, content_type,
                               size_bytes, sha256, source_file_sha256, tipo, match_status,
                               raw_ocr, processed_at)
                            VALUES
                              (:fn, :bk, :ok, :ct, :sz, :sh, :src, :tp, :ms,
                               CAST(:raw AS JSONB), now())
                            RETURNING id
                        """, fn=fname, bk=BUCKET, ok=obj_key, ct="image/jpeg",
                             sz=len(jpg), sh=page_sha, src=source_sha, tp=tipo, ms=match_status,
                             raw=json.dumps(doc, ensure_ascii=False))
                        foto_id = foto["id"]

                    for pid, det, creada, campos in persona_records:
                        _link_foto_persona(foto_id, pid, det, creada, campos)

                # Reportar
                header = f"  {tag} p{doc_pages} → [{tipo}] {match_status}"
                if persona_records:
                    names = ", ".join(det.get("nombre_apellido") or f"DNI {det.get('dni') or '?'}"
                                       for _, det, _, _ in persona_records)
                    print(f"{header} · {len(persona_records)} persona(s): {names}")
                else:
                    print(header)

            # Paginas que la vision no clasifico como documento: subirlas sin analisis
            for page_num, jpg in pages:
                if page_num in covered_pages: continue
                fname = f"{rel_stem}__p{page_num}.jpg"
                r = persist_and_analyze(
                    data=jpg, filename=fname, content_type="image/jpeg",
                    ext=".jpg", analyze=False, source_file_sha256=source_sha,
                )
                _report(f"{tag} p{page_num} (no clasificada)", r); _acc(r)

        except Exception as e:
            print(f"{tag} ERROR PDF: {e}")
        return stats

    print(f"{tag} SKIP (ext desconocido: {ext})")
    return {"skipped": 1}


def main():
    global DEFAULT_JURISDICCION, engine
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-analyze", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--folder", help="path a la carpeta a procesar (default: WhatsApp Chat)")
    ap.add_argument("--jurisdiccion",
                    help="jurisdiccion default para personas detectadas sin jurisdiccion en la planilla. "
                         "No pisa la existente en la DB ni la que la vision detecte explicitamente.")
    ap.add_argument("--db", default="julia", choices=DB_CHOICES,
                    help="Workspace / database a usar (default: julia)")
    ap.add_argument("--model", help="modelo Anthropic de vision: opus | sonnet (default) | haiku "
                                    "(o id completo tipo claude-opus-4-7)")
    ap.add_argument("--por-subcarpeta", action="store_true",
                    help="Procesa cada subcarpeta como batch separado con el nombre de la "
                         "subcarpeta como jurisdiccion default. Los archivos en la raiz se "
                         "procesan al final con la jurisdiccion global (--jurisdiccion).")
    args = ap.parse_args()

    if args.model:
        set_vision_model(args.model)

    engine = create_engine(resolve_db_url(args.db), pool_pre_ping=True)

    folder = Path(args.folder) if args.folder else FOLDER_DEFAULT
    if not folder.exists() or not folder.is_dir():
        print(f"ERROR: la carpeta '{folder}' no existe o no es un directorio.")
        return

    global_jur = args.jurisdiccion.strip() if args.jurisdiccion else None
    if global_jur:
        DEFAULT_JURISDICCION = global_jur

    import vision as _v
    print(f"DB: {args.db!r}  Modelo: {_v.ANTHROPIC_MODEL!r}  Carpeta: {folder}")
    if global_jur:
        print(f"Jurisdiccion default global: {global_jur!r}")
    print(f"Modo: {'por-subcarpeta' if args.por_subcarpeta else 'plano'}")
    print("=" * 72)

    totals: dict = {}
    por_carpeta: dict = {}  # {carpeta: totals}
    t0 = time.time()

    def _procesar_lista(files: list[Path], jur_batch: str | None, label: str):
        """Procesa una lista de archivos con la jurisdiccion dada. Acumula totales."""
        global DEFAULT_JURISDICCION
        DEFAULT_JURISDICCION = jur_batch or global_jur
        batch_totals: dict = {}
        if args.only:
            files = [f for f in files if args.only.lower() in f.name.lower()]
        if args.limit:
            files = files[: args.limit]
        print(f"\n### {label} ({len(files)} archivos) — jurisdiccion default: "
              f"{DEFAULT_JURISDICCION!r} ###")
        for i, f in enumerate(files, 1):
            if not f.is_file(): continue
            rel = str(f.relative_to(folder)).replace("\\", "/")
            print(f"[{i}/{len(files)}] {rel}")
            try:
                r = process_file(f, analyze=not args.no_analyze, dry_run=args.dry_run,
                                 force=args.force, root_folder=folder)
                for k, v in r.items():
                    totals[k] = totals.get(k, 0) + v
                    batch_totals[k] = batch_totals.get(k, 0) + v
            except KeyboardInterrupt:
                print("\nInterrumpido por usuario.")
                raise
            except Exception as e:
                print(f"  ERROR: {e}")
                totals["errores"] = totals.get("errores", 0) + 1
        por_carpeta[label] = batch_totals

    try:
        if args.por_subcarpeta:
            # Batch por cada subcarpeta con nombre-carpeta como jurisdiccion.
            subfolders = sorted(p for p in folder.iterdir() if p.is_dir())
            for sub in subfolders:
                jur = sub.name
                sub_files = sorted(p for p in sub.rglob("*") if p.is_file())
                _procesar_lista(sub_files, jur_batch=jur, label=sub.name)
            # Archivos sueltos en la raiz (si los hay).
            root_files = sorted(p for p in folder.iterdir() if p.is_file())
            if root_files:
                _procesar_lista(root_files, jur_batch=global_jur,
                                label="(raiz de la carpeta)")
        else:
            # Modo plano: todo recursivo con jurisdiccion global.
            all_files = sorted(p for p in folder.rglob("*") if p.is_file())
            _procesar_lista(all_files, jur_batch=global_jur, label=str(folder.name or "root"))
    except KeyboardInterrupt:
        pass

    print("\n" + "=" * 72)
    print(f"Terminado en {time.time()-t0:.1f}s")
    print("\nTotales globales:")
    for k, v in sorted(totals.items()):
        print(f"  {k}: {v}")
    if len(por_carpeta) > 1:
        print("\nPor carpeta:")
        for cp, st in por_carpeta.items():
            resumen = ", ".join(f"{k}={v}" for k, v in sorted(st.items()) if v)
            print(f"  [{cp}] {resumen or '(nada)'}")


if __name__ == "__main__":
    main()
