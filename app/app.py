import os
import io
import re
import json
import hashlib
from datetime import timedelta

import pypdfium2 as pdfium
from PIL import Image

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    jsonify, abort,
)
from sqlalchemy import create_engine, text
from minio import Minio
from minio.error import S3Error

from vision import analyze_image

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

SCHEMA = "avales_2026"
BUCKET = "avales-eleccion-2026"

engine = create_engine(os.environ["DB_CONNECTION_STRING"], pool_pre_ping=True)
minio = Minio(
    os.environ["MINIO_ENDPOINT"],
    access_key=os.environ["MINIO_ACCESS_KEY"],
    secret_key=os.environ["MINIO_SECRET_KEY"],
    secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-only-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB por request

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".pdf"}

ENRIQUECIBLES = ("nombre_apellido", "genero", "matricula", "tomo", "folio", "jurisdiccion")


# ─────────── Helpers ───────────

def q(sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).mappings().all()

def q_one(sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).mappings().first()

def q_one_write(sql, **params):
    with engine.begin() as c:
        return c.execute(text(sql), params).mappings().first()

def exec_sql(sql, **params):
    with engine.begin() as c:
        return c.execute(text(sql), params)

def presigned(object_key, expires_min=60):
    return minio.presigned_get_object(BUCKET, object_key, expires=timedelta(minutes=expires_min))


# ─────────── Rutas: Personas ───────────

@app.route("/")
def index():
    return redirect(url_for("personas_list"))


def _personas_filtros_sql(args) -> tuple[str, dict]:
    """Devuelve (where_sql, params) a partir de los query params de la vista personas."""
    search = (args.get("q") or "").strip()
    jur    = (args.get("jur") or "").strip()
    dni_r  = args.get("dni_recibido", "")
    con_foto = args.get("con_foto", "")
    origen = args.get("origen", "")

    where = []
    params: dict = {}
    if search:
        where.append("(p.nombre_apellido ILIKE :s OR p.dni ILIKE :s OR p.matricula ILIKE :s)")
        params["s"] = f"%{search}%"
    if jur:
        where.append("p.jurisdiccion = :jur")
        params["jur"] = jur
    if dni_r == "si":
        where.append("p.dni_recibido = TRUE")
    elif dni_r == "no":
        where.append("(p.dni_recibido = FALSE OR p.dni_recibido IS NULL)")
    if con_foto == "si":
        where.append(f"EXISTS (SELECT 1 FROM {SCHEMA}.fotos_personas fp WHERE fp.persona_id = p.id)")
    elif con_foto == "no":
        where.append(f"NOT EXISTS (SELECT 1 FROM {SCHEMA}.fotos_personas fp WHERE fp.persona_id = p.id)")
    if origen == "auto":
        where.append("p.observaciones ILIKE 'Creada auto%'")
    elif origen == "excel":
        where.append("(p.observaciones IS NULL OR p.observaciones NOT ILIKE 'Creada auto%')")
    return (("WHERE " + " AND ".join(where)) if where else ""), params


@app.route("/personas")
def personas_list():
    search = (request.args.get("q") or "").strip()
    jur    = (request.args.get("jur") or "").strip()
    dni_r  = request.args.get("dni_recibido", "")
    con_foto = request.args.get("con_foto", "")
    origen = request.args.get("origen", "")

    where_sql, params = _personas_filtros_sql(request.args)

    personas = q(f"""
        SELECT p.*,
               (SELECT COUNT(*) FROM {SCHEMA}.fotos_personas fp WHERE fp.persona_id = p.id) AS n_fotos,
               (p.observaciones ILIKE 'Creada auto%') AS auto_creada
        FROM {SCHEMA}.personas p
        {where_sql}
        ORDER BY p.jurisdiccion NULLS LAST, p.nombre_apellido
        LIMIT 1500
    """, **params)

    jurisdicciones = [r["jurisdiccion"] for r in q(
        f"SELECT DISTINCT jurisdiccion FROM {SCHEMA}.personas WHERE jurisdiccion IS NOT NULL ORDER BY 1"
    )]

    stats = q_one(f"""
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE dni IS NULL) AS sin_dni,
          COUNT(*) FILTER (WHERE dni_recibido) AS dni_recibido,
          COUNT(*) FILTER (WHERE EXISTS (
              SELECT 1 FROM {SCHEMA}.fotos_personas fp WHERE fp.persona_id = p.id
          )) AS con_foto,
          COUNT(*) FILTER (WHERE observaciones ILIKE 'Creada auto%') AS auto_creadas
        FROM {SCHEMA}.personas p
    """)

    return render_template("personas.html",
        personas=personas, jurisdicciones=jurisdicciones, stats=stats,
        f_search=search, f_jur=jur, f_dni_r=dni_r, f_con_foto=con_foto, f_origen=origen,
    )


@app.route("/persona/nueva", methods=["GET", "POST"])
def persona_nueva():
    if request.method == "GET":
        return render_template("persona_nueva.html")

    f = request.form
    dni = (f.get("dni") or "").strip() or None
    nombre = (f.get("nombre_apellido") or "").strip()
    if not nombre:
        flash("El nombre es obligatorio.", "warning")
        return redirect(url_for("persona_nueva"))

    tomo = (f.get("tomo") or "").strip()
    folio = (f.get("folio") or "").strip()
    dni_recibido = None
    if f.get("dni_recibido") == "si":
        dni_recibido = True
    elif f.get("dni_recibido") == "no":
        dni_recibido = False

    try:
        with engine.begin() as c:
            new_p = c.execute(text(f"""
                INSERT INTO {SCHEMA}.personas
                  (nombre_apellido, dni, genero, matricula, tomo, folio,
                   domicilio, jurisdiccion, dni_recibido, cotejado, observaciones)
                VALUES (:n, :d, :g, :m, :t, :fo, :dom, :jur, :dnir, :cot, :obs)
                RETURNING id
            """), {
                "n": nombre, "d": dni,
                "g": (f.get("genero") or "").strip() or None,
                "m": (f.get("matricula") or "").strip() or None,
                "t": int(tomo) if tomo.isdigit() else None,
                "fo": int(folio) if folio.isdigit() else None,
                "dom": (f.get("domicilio") or "").strip() or None,
                "jur": (f.get("jurisdiccion") or "").strip() or None,
                "dnir": dni_recibido,
                "cot": (f.get("cotejado") or "").strip() or None,
                "obs": (f.get("observaciones") or "").strip() or None,
            }).mappings().first()
        flash(f"Persona creada (id={new_p['id']}).", "success")
        return redirect(url_for("persona_detail", pid=new_p["id"]))
    except Exception as e:
        flash(f"Error al crear: {e}", "danger")
        return redirect(url_for("persona_nueva"))


@app.route("/persona/<int:pid>/eliminar", methods=["POST"])
def persona_eliminar(pid):
    p = q_one(f"SELECT nombre_apellido FROM {SCHEMA}.personas WHERE id=:id", id=pid)
    if not p:
        abort(404)
    # Las fotos vinculadas via fotos_personas se borran en cascada (FK ON DELETE CASCADE).
    # Las fotos con persona_id directo pasan a NULL (FK ON DELETE SET NULL).
    exec_sql(f"DELETE FROM {SCHEMA}.personas WHERE id=:id", id=pid)
    flash(f"Persona '{p['nombre_apellido']}' eliminada.", "info")
    return redirect(url_for("personas_list"))


@app.route("/personas.xlsx")
def personas_excel():
    """Exporta el listado de personas (respetando filtros de la URL) a Excel."""
    import pandas as pd
    from io import BytesIO
    from flask import send_file

    where_sql, params = _personas_filtros_sql(request.args)

    rows = q(f"""
        SELECT p.numero_excel, p.nombre_apellido, p.dni, p.genero, p.matricula, p.tomo, p.folio,
               p.domicilio, p.jurisdiccion, p.dni_recibido, p.cotejado, p.observaciones,
               (p.observaciones ILIKE 'Creada auto%') AS auto_creada,
               (SELECT COUNT(*) FROM {SCHEMA}.fotos_personas fp WHERE fp.persona_id = p.id) AS n_fotos,
               (SELECT COUNT(*) FROM {SCHEMA}.fotos_personas fp
                JOIN {SCHEMA}.fotos f ON f.id = fp.foto_id
                WHERE fp.persona_id = p.id AND f.tipo = 'planilla_aval') AS n_planillas,
               (SELECT COUNT(*) FROM {SCHEMA}.fotos_personas fp
                JOIN {SCHEMA}.fotos f ON f.id = fp.foto_id
                WHERE fp.persona_id = p.id AND f.tipo = 'dni') AS n_dnis,
               (SELECT COUNT(*) FROM {SCHEMA}.fotos_personas fp
                JOIN {SCHEMA}.fotos f ON f.id = fp.foto_id
                WHERE fp.persona_id = p.id AND (f.tipo = 'otro' OR f.tipo IS NULL)) AS n_otros,
               p.created_at, p.updated_at
        FROM {SCHEMA}.personas p
        {where_sql}
        ORDER BY p.jurisdiccion NULLS LAST, p.nombre_apellido
    """, **params)

    df = pd.DataFrame([dict(r) for r in rows])
    # Excel no soporta datetimes con timezone → convertir a naive
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]) and getattr(df[col].dt, "tz", None) is not None:
            df[col] = df[col].dt.tz_localize(None)
    # nombres más amigables
    df = df.rename(columns={
        "numero_excel":    "N° Excel",
        "nombre_apellido": "Nombre y Apellido",
        "dni":             "DNI",
        "genero":          "Género",
        "matricula":       "Matrícula",
        "tomo":            "Tomo",
        "folio":           "Folio",
        "domicilio":       "Domicilio",
        "jurisdiccion":    "Jurisdicción",
        "dni_recibido":    "DNI recibido",
        "cotejado":        "Cotejado",
        "observaciones":   "Observaciones",
        "auto_creada":     "Auto-creada",
        "n_fotos":         "Fotos",
        "n_planillas":     "Planillas",
        "n_dnis":          "DNIs adj.",
        "n_otros":         "Otros adj.",
        "created_at":      "Creado",
        "updated_at":      "Actualizado",
    })

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Personas")
        # ancho automático
        ws = w.sheets["Personas"]
        for i, col in enumerate(df.columns, start=1):
            m = max([len(str(col))] + [len(str(v)) for v in df[col].astype(str).values[:200]])
            ws.column_dimensions[chr(64 + i) if i <= 26 else "A"].width = min(m + 2, 50)
    buf.seek(0)

    from datetime import datetime as _dt
    fname = f"personas_{_dt.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(buf, download_name=fname, as_attachment=True,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/persona/<int:pid>")
def persona_detail(pid):
    p = q_one(f"SELECT * FROM {SCHEMA}.personas WHERE id = :id", id=pid)
    if not p:
        abort(404)
    fotos = q(f"""
        SELECT f.*, fp.dni_detectado AS fp_dni, fp.nombre_detectado AS fp_nombre,
               fp.persona_creada, fp.campos_enriquecidos
        FROM {SCHEMA}.fotos_personas fp
        JOIN {SCHEMA}.fotos f ON f.id = fp.foto_id
        WHERE fp.persona_id = :id
        ORDER BY f.uploaded_at DESC
    """, id=pid)
    fotos_urls = [{**dict(f), "url": presigned(f["minio_object_key"])} for f in fotos]
    return render_template("persona_detail.html", p=p, fotos=fotos_urls)


@app.route("/persona/<int:pid>/edit", methods=["POST"])
def persona_edit(pid):
    f = request.form
    dni_recibido = None
    if f.get("dni_recibido") == "si":
        dni_recibido = True
    elif f.get("dni_recibido") == "no":
        dni_recibido = False

    dni = (f.get("dni") or "").strip() or None
    tomo = (f.get("tomo") or "").strip() or None
    folio = (f.get("folio") or "").strip() or None

    try:
        exec_sql(f"""
            UPDATE {SCHEMA}.personas SET
              nombre_apellido = :nombre, dni = :dni, genero = :genero,
              matricula = :matricula, tomo = :tomo, folio = :folio,
              domicilio = :domicilio, jurisdiccion = :jurisdiccion,
              dni_recibido = :dni_recibido, cotejado = :cotejado, observaciones = :observaciones
            WHERE id = :id
        """,
            id=pid,
            nombre=(f.get("nombre_apellido") or "").strip() or None,
            dni=dni,
            genero=(f.get("genero") or "").strip() or None,
            matricula=(f.get("matricula") or "").strip() or None,
            tomo=int(tomo) if tomo and tomo.isdigit() else None,
            folio=int(folio) if folio and folio.isdigit() else None,
            domicilio=(f.get("domicilio") or "").strip() or None,
            jurisdiccion=(f.get("jurisdiccion") or "").strip() or None,
            dni_recibido=dni_recibido,
            cotejado=(f.get("cotejado") or "").strip() or None,
            observaciones=(f.get("observaciones") or "").strip() or None,
        )
        flash("Persona actualizada.", "success")
    except Exception as e:
        flash(f"Error al actualizar: {e}", "danger")
    return redirect(url_for("persona_detail", pid=pid))


# ─────────── Rutas: Fotos ───────────

def _fotos_query(where_sql: str = "", **params):
    """Devuelve fotos con lista de personas vinculadas agregada como JSON."""
    return q(f"""
        SELECT f.*,
               COALESCE((
                 SELECT jsonb_agg(jsonb_build_object(
                   'persona_id', fp.persona_id,
                   'nombre',     p.nombre_apellido,
                   'dni',        p.dni,
                   'creada',     fp.persona_creada,
                   'campos',     fp.campos_enriquecidos
                 ) ORDER BY p.nombre_apellido)
                 FROM {SCHEMA}.fotos_personas fp
                 JOIN {SCHEMA}.personas p ON p.id = fp.persona_id
                 WHERE fp.foto_id = f.id
               ), '[]'::jsonb) AS personas
        FROM {SCHEMA}.fotos f
        {where_sql}
        ORDER BY f.uploaded_at DESC
        LIMIT 1000
    """, **params)


@app.route("/fotos")
def fotos_list():
    status = (request.args.get("status") or "").strip()
    tipo = (request.args.get("tipo") or "").strip()

    conds = []
    params = {}
    if status:
        conds.append("f.match_status = :st"); params["st"] = status
    if tipo:
        conds.append("f.tipo = :tp"); params["tp"] = tipo
    where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""

    fotos = _fotos_query(where_sql, **params)
    fotos = [{**dict(f), "url": presigned(f["minio_object_key"])} for f in fotos]

    counts_status = q(f"SELECT match_status, COUNT(*) AS n FROM {SCHEMA}.fotos GROUP BY match_status ORDER BY 1")
    counts_tipo   = q(f"SELECT COALESCE(tipo,'sin_analizar') AS tipo, COUNT(*) AS n FROM {SCHEMA}.fotos GROUP BY 1 ORDER BY 1")
    return render_template("fotos.html",
        fotos=fotos, counts_status=counts_status, counts_tipo=counts_tipo,
        f_status=status, f_tipo=tipo,
    )


@app.route("/fotos/revisar")
def fotos_revisar():
    """Fotos que necesitan mirada humana: tipo='otro', sin match, o sin personas vinculadas."""
    fotos = _fotos_query("""
        WHERE (f.tipo = 'otro' OR f.match_status = 'sin_match'
               OR NOT EXISTS (SELECT 1 FROM {s}.fotos_personas fp WHERE fp.foto_id = f.id))
    """.format(s=SCHEMA))
    fotos = [{**dict(f), "url": presigned(f["minio_object_key"])} for f in fotos]
    return render_template("fotos_revisar.html", fotos=fotos)


# ─────────── Upload ───────────

def _find_or_create_persona(det: dict) -> tuple[int, bool, list[str]]:
    dni = det.get("dni")
    if not dni:
        raise ValueError("DNI requerido para vincular")
    existing = q_one(f"""
        SELECT id, nombre_apellido, genero, matricula, tomo, folio, jurisdiccion
        FROM {SCHEMA}.personas WHERE dni = :d
    """, d=dni)
    if existing:
        updates = {}
        for k in ENRIQUECIBLES:
            if det.get(k) and not existing[k]:
                updates[k] = det[k]
        if updates:
            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            exec_sql(f"UPDATE {SCHEMA}.personas SET {set_clause} WHERE id = :id",
                     id=existing["id"], **updates)
        return existing["id"], False, list(updates.keys())

    new_p = q_one_write(f"""
        INSERT INTO {SCHEMA}.personas
          (nombre_apellido, dni, genero, matricula, tomo, folio, jurisdiccion, observaciones)
        VALUES (:n, :d, :g, :m, :t, :f, :j, 'Creada automáticamente desde visión')
        RETURNING id
    """,
        n=det.get("nombre_apellido") or f"(pendiente) DNI {dni}",
        d=dni, g=det.get("genero"),
        m=det.get("matricula"), t=det.get("tomo"), f=det.get("folio"),
        j=det.get("jurisdiccion"),
    )
    return new_p["id"], True, list(ENRIQUECIBLES)


def _link(foto_id: int, persona_id: int, det: dict, creada: bool, campos: list[str]):
    exec_sql(f"""
        INSERT INTO {SCHEMA}.fotos_personas
          (foto_id, persona_id, dni_detectado, nombre_detectado, persona_creada, campos_enriquecidos)
        VALUES (:f, :p, :d, :n, :c, :ce)
        ON CONFLICT (foto_id, persona_id) DO NOTHING
    """, f=foto_id, p=persona_id, d=det.get("dni"), n=det.get("nombre_apellido"),
        c=creada, ce=",".join(campos) if campos else None)


def _pdf_pages_as_jpg(pdf_bytes: bytes, dpi: int = 200, jpeg_quality: int = 85):
    """Rinde cada pagina de un PDF como JPEG. Yield (page_num, jpeg_bytes)."""
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


def _persist_upload(*, data: bytes, filename: str, content_type: str, ext: str,
                    persona_id_hint=None, analyze: bool = True) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    object_key = f"originales/{sha[:2]}/{sha}{ext}"

    try:
        minio.stat_object(BUCKET, object_key)
    except S3Error:
        minio.put_object(BUCKET, object_key, io.BytesIO(data), length=len(data),
                         content_type=content_type or "application/octet-stream")

    already = q_one(f"SELECT id FROM {SCHEMA}.fotos WHERE minio_object_key=:k", k=object_key)
    if already:
        # Si el usuario forzó una persona, aseguramos el link
        if persona_id_hint:
            _link(already["id"], persona_id_hint,
                  {"dni": None, "nombre_apellido": None}, creada=False, campos=[])
        return {"status": "duplicado", "foto_id": already["id"]}

    ocr = None
    tipo = None
    personas_det: list = []
    if analyze and not persona_id_hint:
        ocr = analyze_image(data, content_type or "")
        tipo = ocr.get("tipo")
        personas_det = [p for p in ocr.get("personas", []) if p.get("dni")]

    if persona_id_hint:
        match_status = "manual"
    elif not analyze:
        match_status = "pendiente"
    elif not personas_det:
        match_status = "sin_match"
    else:
        match_status = "matched"

    foto = q_one_write(f"""
        INSERT INTO {SCHEMA}.fotos
          (filename_original, minio_bucket, minio_object_key, content_type, size_bytes, sha256,
           source_file_sha256, tipo, match_status, raw_ocr, processed_at)
        VALUES
          (:fn, :bk, :ok, :ct, :sz, :sh, :src, :tp, :ms, CAST(:raw AS JSONB),
           CASE WHEN :raw IS NULL THEN NULL ELSE now() END)
        RETURNING id
    """,
        fn=filename, bk=BUCKET, ok=object_key, ct=content_type, sz=len(data), sh=sha,
        src=sha, tp=tipo, ms=match_status,
        raw=json.dumps(ocr, ensure_ascii=False) if ocr else None,
    )
    foto_id = foto["id"]

    creadas = enriquecidas = 0
    if persona_id_hint:
        _link(foto_id, persona_id_hint,
              {"dni": None, "nombre_apellido": None}, creada=False, campos=[])
    else:
        for det in personas_det:
            try:
                pid, creada, campos = _find_or_create_persona(det)
                _link(foto_id, pid, det, creada, campos)
                if creada: creadas += 1
                elif campos: enriquecidas += 1
            except Exception as e:
                app.logger.warning("link error: %s", e)

    return {"status": "ok", "foto_id": foto_id, "match_status": match_status,
            "tipo": tipo, "personas": len(personas_det),
            "creadas": creadas, "enriquecidas": enriquecidas}


@app.route("/fotos/upload", methods=["GET", "POST"])
def fotos_upload():
    if request.method == "GET":
        return render_template("upload.html")

    persona_id_hint = request.form.get("persona_id")
    persona_id_hint = int(persona_id_hint) if persona_id_hint and persona_id_hint.isdigit() else None
    analyze = request.form.get("analizar", "on") == "on"

    files = request.files.getlist("files")
    if not files:
        flash("No se recibieron archivos.", "warning")
        return redirect(url_for("fotos_upload"))

    stats = {"ok": 0, "matched": 0, "sin_match": 0, "creadas": 0, "enriquecidas": 0,
             "duplicadas": 0, "skipped": 0, "paginas_pdf": 0}

    def _acc(r):
        if r["status"] == "duplicado":
            stats["duplicadas"] += 1
        else:
            stats["ok"] += 1
            stats[r["match_status"]] = stats.get(r["match_status"], 0) + 1
            stats["creadas"] += r.get("creadas", 0)
            stats["enriquecidas"] += r.get("enriquecidas", 0)

    for f in files:
        name = f.filename or ""
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXT:
            stats["skipped"] += 1
            continue
        data = f.read()

        if ext == ".pdf":
            # Guardamos el PDF original en MinIO (una vez), y despues procesamos cada pagina.
            _persist_upload(
                data=data, filename=name, content_type="application/pdf",
                ext=ext, persona_id_hint=persona_id_hint, analyze=False,
            )
            try:
                stem = os.path.splitext(name)[0]
                for page_num, jpg in _pdf_pages_as_jpg(data):
                    fname = f"{stem}__p{page_num}.jpg"
                    r = _persist_upload(
                        data=jpg, filename=fname, content_type="image/jpeg",
                        ext=".jpg", persona_id_hint=persona_id_hint, analyze=analyze,
                    )
                    _acc(r)
                    stats["paginas_pdf"] += 1
            except Exception as e:
                flash(f"Error procesando PDF '{name}': {e}", "danger")
            continue

        r = _persist_upload(data=data, filename=name, content_type=f.mimetype or "",
                            ext=ext, persona_id_hint=persona_id_hint, analyze=analyze)
        _acc(r)

    flash(
        f"Subidas: {stats['ok']} · matched: {stats['matched']} · sin_match: {stats['sin_match']} · "
        f"personas creadas: {stats['creadas']} · enriquecidas: {stats['enriquecidas']} · "
        f"duplicadas: {stats['duplicadas']} · omitidas: {stats['skipped']}"
        + (f" · paginas PDF: {stats['paginas_pdf']}" if stats['paginas_pdf'] else ""),
        "success",
    )
    if persona_id_hint:
        return redirect(url_for("persona_detail", pid=persona_id_hint))
    return redirect(url_for("fotos_list"))


@app.route("/foto/<int:fid>/asignar", methods=["POST"])
def foto_asignar(fid):
    persona_id = request.form.get("persona_id")
    persona_id = int(persona_id) if persona_id and persona_id.isdigit() else None
    if not persona_id:
        flash("Persona inválida.", "warning")
        return redirect(request.form.get("next") or url_for("fotos_list"))
    _link(fid, persona_id, {"dni": None, "nombre_apellido": None}, creada=False, campos=[])
    exec_sql(f"UPDATE {SCHEMA}.fotos SET match_status='manual' WHERE id=:id AND match_status='sin_match'", id=fid)
    flash("Foto vinculada a la persona.", "success")
    return redirect(request.form.get("next") or url_for("fotos_list"))


@app.route("/foto/<int:fid>/desvincular/<int:pid>", methods=["POST"])
def foto_desvincular(fid, pid):
    exec_sql(f"DELETE FROM {SCHEMA}.fotos_personas WHERE foto_id=:f AND persona_id=:p", f=fid, p=pid)
    flash("Vínculo eliminado.", "info")
    return redirect(request.form.get("next") or url_for("fotos_list"))


@app.route("/foto/<int:fid>/eliminar", methods=["POST"])
def foto_eliminar(fid):
    f = q_one(f"SELECT * FROM {SCHEMA}.fotos WHERE id=:id", id=fid)
    if not f:
        abort(404)
    try:
        minio.remove_object(BUCKET, f["minio_object_key"])
    except S3Error:
        pass
    exec_sql(f"DELETE FROM {SCHEMA}.fotos WHERE id=:id", id=fid)  # cascade borra links
    flash("Foto eliminada.", "info")
    return redirect(request.form.get("next") or url_for("fotos_list"))


# ─────────── API ───────────

@app.route("/api/personas/search")
def api_personas_search():
    term = (request.args.get("q") or "").strip()
    if len(term) < 2:
        return jsonify([])
    rows = q(f"""
        SELECT id, nombre_apellido, dni, matricula, jurisdiccion
        FROM {SCHEMA}.personas
        WHERE nombre_apellido ILIKE :s OR dni ILIKE :s OR matricula ILIKE :s
        ORDER BY nombre_apellido
        LIMIT 20
    """, s=f"%{term}%")
    return jsonify([dict(r) for r in rows])


# ─────────── Reportes ───────────

REPORTES = {
    "aval-completo": {
        "titulo": "Aval completo (planilla + DNI)",
        "descripcion": "Personas que tienen tanto la planilla firmada como el DNI adjunto — cumplen con el instructivo.",
        "sql": """
            SELECT p.id, p.nombre_apellido, p.dni, p.matricula, p.tomo, p.folio, p.jurisdiccion,
                   (p.observaciones ILIKE 'Creada auto%%') AS auto_creada,
                   (SELECT COUNT(*) FROM avales_2026.fotos_personas fp
                    JOIN avales_2026.fotos f ON f.id = fp.foto_id
                    WHERE fp.persona_id = p.id AND f.tipo = 'planilla_aval') AS n_planillas,
                   (SELECT COUNT(*) FROM avales_2026.fotos_personas fp
                    JOIN avales_2026.fotos f ON f.id = fp.foto_id
                    WHERE fp.persona_id = p.id AND f.tipo = 'dni') AS n_dnis
            FROM avales_2026.personas p
            WHERE EXISTS (
                SELECT 1 FROM avales_2026.fotos_personas fp
                JOIN avales_2026.fotos f ON f.id = fp.foto_id
                WHERE fp.persona_id = p.id AND f.tipo = 'planilla_aval'
            ) AND EXISTS (
                SELECT 1 FROM avales_2026.fotos_personas fp
                JOIN avales_2026.fotos f ON f.id = fp.foto_id
                WHERE fp.persona_id = p.id AND f.tipo = 'dni'
            )
            ORDER BY p.jurisdiccion NULLS LAST, p.nombre_apellido
        """,
    },
    "aval-con-otros": {
        "titulo": "Aval con fotos de tipo 'otro'",
        "descripcion": "Personas con planilla firmada que además tienen al menos una foto que la visión no pudo clasificar como DNI ni planilla — revisar qué es (constancia de matrícula, ilegible, otro documento).",
        "sql": """
            SELECT p.id, p.nombre_apellido, p.dni, p.matricula, p.tomo, p.folio, p.jurisdiccion,
                   (SELECT COUNT(*) FROM avales_2026.fotos_personas fp
                    JOIN avales_2026.fotos f ON f.id = fp.foto_id
                    WHERE fp.persona_id = p.id AND f.tipo = 'planilla_aval') AS n_planillas,
                   (SELECT COUNT(*) FROM avales_2026.fotos_personas fp
                    JOIN avales_2026.fotos f ON f.id = fp.foto_id
                    WHERE fp.persona_id = p.id AND f.tipo = 'dni') AS n_dnis,
                   (SELECT COUNT(*) FROM avales_2026.fotos_personas fp
                    JOIN avales_2026.fotos f ON f.id = fp.foto_id
                    WHERE fp.persona_id = p.id AND (f.tipo = 'otro' OR f.tipo IS NULL)) AS n_otros
            FROM avales_2026.personas p
            WHERE EXISTS (
                SELECT 1 FROM avales_2026.fotos_personas fp
                JOIN avales_2026.fotos f ON f.id = fp.foto_id
                WHERE fp.persona_id = p.id AND f.tipo = 'planilla_aval'
            ) AND EXISTS (
                SELECT 1 FROM avales_2026.fotos_personas fp
                JOIN avales_2026.fotos f ON f.id = fp.foto_id
                WHERE fp.persona_id = p.id AND (f.tipo = 'otro' OR f.tipo IS NULL)
            )
            ORDER BY p.jurisdiccion NULLS LAST, p.nombre_apellido
        """,
    },
    "firmaron-sin-dni": {
        "titulo": "Firmaron pero falta DNI",
        "descripcion": "Personas que aparecen firmando en al menos una planilla de aval, pero no tienen ninguna foto tipo DNI vinculada.",
        "sql": """
            SELECT p.id, p.nombre_apellido, p.dni, p.matricula, p.tomo, p.folio,
                   p.jurisdiccion,
                   (p.observaciones ILIKE 'Creada auto%%') AS auto_creada,
                   (SELECT COUNT(*) FROM avales_2026.fotos_personas fp
                    JOIN avales_2026.fotos f ON f.id = fp.foto_id
                    WHERE fp.persona_id = p.id AND f.tipo = 'planilla_aval') AS n_planillas
            FROM avales_2026.personas p
            WHERE EXISTS (
                SELECT 1 FROM avales_2026.fotos_personas fp
                JOIN avales_2026.fotos f ON f.id = fp.foto_id
                WHERE fp.persona_id = p.id AND f.tipo = 'planilla_aval'
            ) AND NOT EXISTS (
                SELECT 1 FROM avales_2026.fotos_personas fp
                JOIN avales_2026.fotos f ON f.id = fp.foto_id
                WHERE fp.persona_id = p.id AND f.tipo = 'dni'
            )
            ORDER BY p.jurisdiccion NULLS LAST, p.nombre_apellido
        """,
    },
    "excel-sin-foto": {
        "titulo": "Personas del Excel sin ninguna foto",
        "descripcion": "Personas cargadas originalmente en el Excel (no auto-creadas) que no tienen ningún archivo vinculado. Probables faltantes.",
        "sql": """
            SELECT p.id, p.numero_excel, p.nombre_apellido, p.dni, p.matricula, p.tomo, p.folio,
                   p.jurisdiccion, p.domicilio, p.dni_recibido, p.cotejado, p.observaciones
            FROM avales_2026.personas p
            WHERE (p.observaciones IS NULL OR p.observaciones NOT ILIKE 'Creada auto%%')
              AND NOT EXISTS (
                  SELECT 1 FROM avales_2026.fotos_personas fp WHERE fp.persona_id = p.id
              )
            ORDER BY p.jurisdiccion NULLS LAST, p.nombre_apellido
        """,
    },
    "dni-sin-firma": {
        "titulo": "DNI adjunto pero sin firma en planilla",
        "descripcion": "Personas con al menos una foto tipo DNI vinculada, pero que no aparecen firmando en ninguna planilla.",
        "sql": """
            SELECT p.id, p.nombre_apellido, p.dni, p.matricula, p.tomo, p.folio, p.jurisdiccion,
                   (p.observaciones ILIKE 'Creada auto%%') AS auto_creada,
                   (SELECT COUNT(*) FROM avales_2026.fotos_personas fp
                    JOIN avales_2026.fotos f ON f.id = fp.foto_id
                    WHERE fp.persona_id = p.id AND f.tipo = 'dni') AS n_dnis
            FROM avales_2026.personas p
            WHERE EXISTS (
                SELECT 1 FROM avales_2026.fotos_personas fp
                JOIN avales_2026.fotos f ON f.id = fp.foto_id
                WHERE fp.persona_id = p.id AND f.tipo = 'dni'
            ) AND NOT EXISTS (
                SELECT 1 FROM avales_2026.fotos_personas fp
                JOIN avales_2026.fotos f ON f.id = fp.foto_id
                WHERE fp.persona_id = p.id AND f.tipo = 'planilla_aval'
            )
            ORDER BY p.jurisdiccion NULLS LAST, p.nombre_apellido
        """,
    },
}


@app.route("/reportes")
def reportes_index():
    counts = {}
    for key, cfg in REPORTES.items():
        counts[key] = q_one(f"SELECT COUNT(*) AS n FROM ({cfg['sql']}) sub")["n"]
    return render_template("reportes.html", reportes=REPORTES, counts=counts)


@app.route("/reportes/<slug>")
def reporte_detalle(slug):
    cfg = REPORTES.get(slug)
    if not cfg:
        abort(404)
    rows = q(cfg["sql"])
    return render_template("reporte_detalle.html", slug=slug, cfg=cfg, rows=rows)


@app.route("/reportes/<slug>.xlsx")
def reporte_excel(slug):
    cfg = REPORTES.get(slug)
    if not cfg:
        abort(404)
    import pandas as pd
    from io import BytesIO
    from flask import send_file
    rows = q(cfg["sql"])
    df = pd.DataFrame([dict(r) for r in rows])
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]) and getattr(df[col].dt, "tz", None) is not None:
            df[col] = df[col].dt.tz_localize(None)
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name=cfg["titulo"][:31])
    buf.seek(0)
    return send_file(buf, download_name=f"{slug}.xlsx", as_attachment=True,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ─────────── Entregables (PDFs por jurisdicción) ───────────

@app.route("/entregables")
def entregables_list():
    """Lista todos los PDFs subidos bajo entregables/ en MinIO."""
    variante_filter = (request.args.get("variante") or "").strip()

    items = []
    for obj in minio.list_objects(BUCKET, prefix="entregables/", recursive=True):
        name = obj.object_name  # entregables/YYYYMMDD/avales.<jur>.<variante>.pdf
        parts = name.split("/")
        if len(parts) < 3 or not name.endswith(".pdf"):
            continue
        fecha = parts[1]
        fname = parts[2]
        # Nuevo formato "entrega": avales.<jur>.pdf (sin sufijo).
        # Otros: avales.<jur>.<variante>.pdf.  Legacy: avales.<jur>.instructivo.pdf.
        m = re.match(
            r"^avales\.(?P<jur>.+?)(?:\.(?P<variante>completo|todos|docs|instructivo))?\.pdf$",
            fname,
        )
        if not m:
            continue
        variante = m.group("variante") or "entrega"
        if variante == "instructivo":
            variante = "entrega"  # normalizar legacy
        if variante_filter and variante != variante_filter:
            continue
        items.append({
            "fecha": fecha,
            "jurisdiccion_slug": m.group("jur"),
            "jurisdiccion": m.group("jur").replace("_", " ").title(),
            "variante": variante,
            "kb": (obj.size or 0) // 1024,
            "object_key": name,
            "url": presigned(name, expires_min=60 * 24),
            "last_modified": obj.last_modified,
        })

    items.sort(key=lambda x: (x["fecha"], x["jurisdiccion_slug"], x["variante"]))

    # Agrupar por jurisdicción para render
    grupos: dict = {}
    for it in items:
        grupos.setdefault(it["jurisdiccion"], []).append(it)

    conteos = {}
    for it in items:
        conteos[it["variante"]] = conteos.get(it["variante"], 0) + 1

    return render_template("entregables.html",
                           grupos=grupos, conteos=conteos, total=len(items),
                           variante_filter=variante_filter)


# ─────────── Duplicados ───────────

@app.route("/duplicados")
def duplicados():
    import csv
    csv_path = os.path.join(os.path.dirname(__file__), "..", "duplicados_para_revisar.csv")
    filas_csv = []
    if os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8-sig") as fh:
            filas_csv = list(csv.DictReader(fh))
    return render_template("duplicados.html", filas=filas_csv)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5057, debug=True)
