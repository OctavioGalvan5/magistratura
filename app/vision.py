"""
Análisis multi-persona de fotos/scans de avales:
- Foto de DNI argentino  → 1 persona
- Planilla de avales     → N personas (nombre, DNI, matrícula, jurisdicción)
- Otro                   → 0 personas

Devuelve dict:
{
  "tipo": "dni" | "planilla_aval" | "otro",
  "personas": [
    {
      "dni":              str|None,
      "nombre_apellido":  str|None,
      "genero":           "M"|"F"|"X"|None,
      "tomo":             int|None,
      "folio":            int|None,
      "matricula":        str|None,        # texto original si no parsea T° X F° Y
      "jurisdiccion":     str|None,
      "fecha_nacimiento": str|None,        # YYYY-MM-DD
    }, ...
  ],
  "notas": str|None,
  "_provider": "anthropic:..." | "openai:..." | None,
}

Estrategia:
1) Anthropic Claude Opus 4.7 (mejor visión, structured outputs).
2) Fallback OpenAI gpt-4o-mini si Anthropic falla o no hay API key.
"""
import os
import io
import base64
import json
import logging
import re
from typing import Optional

import anthropic
from openai import OpenAI
from PIL import Image

log = logging.getLogger(__name__)

_MODEL_ALIASES = {
    "opus":   "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5",
}

def _resolve_model(name: str | None, fallback: str) -> str:
    if not name: return fallback
    return _MODEL_ALIASES.get(name.lower().strip(), name)

# Default: Sonnet 4.6 (~40% mas barato que Opus 4.7, calidad muy similar para OCR estructurado).
# Se puede overridear con env VISION_MODEL=opus|sonnet|haiku (o el id completo).
ANTHROPIC_MODEL = _resolve_model(os.environ.get("VISION_MODEL"), "claude-sonnet-4-6")
OPENAI_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini")


def set_model(name: str) -> None:
    """Cambia el modelo Anthropic en runtime (llamado por --model desde los scripts)."""
    global ANTHROPIC_MODEL
    ANTHROPIC_MODEL = _resolve_model(name, ANTHROPIC_MODEL)

PROMPT_MULTIPAGINA = (
    "Sos un extractor de datos para un padrón de abogados. Te paso VARIAS páginas "
    "consecutivas de un mismo PDF (aval, planilla, DNIs, credenciales, mezclados). "
    "Tu tarea: identificar TODOS los DOCUMENTOS presentes y las personas que aparecen "
    "en cada uno, aprovechando el contexto entre páginas.\n\n"
    "Tipos de documento:\n"
    "  - 'dni'            → una página con anverso/reverso de un DNI argentino.\n"
    "  - 'credencial'     → una página con la credencial/carnet de matrícula profesional.\n"
    "                       Puede ocupar 1 página (frente+dorso en la misma) o 2 páginas\n"
    "                       consecutivas (frente en una, dorso en la siguiente).\n"
    "  - 'planilla_aval'  → una página con TABLA de firmantes (nombre, DNI, matrícula, jur).\n"
    "  - 'otro'           → cualquier otra cosa.\n\n"
    "IMPORTANTE — combinar frente + dorso de credenciales:\n"
    "  Si en la página N ves una credencial con FOTO + NOMBRE pero sin tomo/folio\n"
    "  visibles, y en la página N+1 ves los datos de matrícula (Tomo, Folio, jur.)\n"
    "  sin nombre, ASUMÍ que son frente y dorso de la MISMA credencial de la misma\n"
    "  persona y devolvé UN solo documento con paginas=[N, N+1].\n\n"
    "Devolvé una lista 'documentos'. Cada documento con:\n"
    "  - tipo\n"
    "  - paginas: array de números de página 1-indexed donde aparece (ej: [1] o [3,4])\n"
    "  - personas: lista de personas detectadas en el documento con:\n"
    "      dni, nombre_apellido, genero (M/F/X), tomo, folio, matricula, jurisdiccion,\n"
    "      fecha_nacimiento (YYYY-MM-DD).\n"
    "  - notas: texto opcional con cualquier ambigüedad relevante.\n\n"
    "Reglas:\n"
    "  - 'dni' solo dígitos, sin puntos ni espacios.\n"
    "  - 'genero' exactamente 'M', 'F' o 'X'.\n"
    "  - Si un campo no es legible, devolvé null (no adivines).\n"
    "  - Para un DNI: 1 documento con 1 persona.\n"
    "  - Para una credencial: 1 documento con 1 persona, paginas=[N] o [N, N+1].\n"
    "  - Para una planilla: 1 documento con N personas (una por fila).\n"
    "  - Ignorá páginas obviamente basura (chats, stickers, blancos) o marcalas como 'otro'."
)

# Schema multi-página: lista de documentos, cada uno con páginas y personas
SCHEMA_MULTI = {
    "type": "object",
    "properties": {
        "documentos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string",
                             "enum": ["dni", "planilla_aval", "credencial", "otro"]},
                    "paginas": {"type": "array", "items": {"type": "integer"}},
                    "personas": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "dni":              {"type": ["string", "null"]},
                                "nombre_apellido":  {"type": ["string", "null"]},
                                "genero":           {"type": ["string", "null"]},
                                "tomo":             {"type": ["integer", "null"]},
                                "folio":            {"type": ["integer", "null"]},
                                "matricula":        {"type": ["string", "null"]},
                                "jurisdiccion":     {"type": ["string", "null"]},
                                "fecha_nacimiento": {"type": ["string", "null"]},
                            },
                            "required": ["dni", "nombre_apellido", "genero", "tomo", "folio",
                                         "matricula", "jurisdiccion", "fecha_nacimiento"],
                            "additionalProperties": False,
                        },
                    },
                    "notas": {"type": ["string", "null"]},
                },
                "required": ["tipo", "paginas", "personas", "notas"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["documentos"],
    "additionalProperties": False,
}


PROMPT = (
    "Sos un extractor de datos para un padrón de abogados. "
    "Analizá la imagen y clasificala en UNO de estos tipos:\n"
    "  - 'dni'            → foto del anverso o reverso de un DNI argentino.\n"
    "  - 'planilla_aval'  → PLANILLA con una tabla de firmantes (fila por persona con "
    "                       columnas nombre, DNI, matrícula, jurisdicción, firma, etc.).\n"
    "  - 'credencial'     → CREDENCIAL / carnet de matrícula profesional del Colegio de "
    "                       Abogados o Cámara Federal. Suele tener foto, nombre, tomo, folio, "
    "                       y a veces DNI. NO es un DNI ni una planilla.\n"
    "  - 'otro'           → cualquier otra cosa (foto ilegible, chat, sticker, comprobante, etc.).\n\n"
    "Devolvé UNA lista 'personas' con TODAS las personas detectadas:\n"
    "  - Si es un DNI: 1 sola persona con dni + nombre_apellido + genero + fecha_nacimiento.\n"
    "  - Si es una credencial: 1 sola persona con nombre_apellido + tomo + folio + matrícula "
    "    (y dni + jurisdicción si son visibles).\n"
    "  - Si es una planilla: una entrada por FILA CON DATOS (ignorá filas vacías). "
    "    Extraé DNI, nombre_apellido, genero (M/F/X), matrícula, jurisdicción. "
    "    Cuando la matrícula tenga formato 'Tomo N Folio M' o 'T° N F° M', devolvé también los enteros "
    "    'tomo' y 'folio'; si no parsea, dejalos en null y poné el texto crudo en 'matricula'.\n"
    "  - Si es 'otro': devolvé personas: [].\n\n"
    "Reglas de formato:\n"
    "  - 'dni' solo dígitos, sin puntos ni espacios.\n"
    "  - 'genero' exactamente 'M', 'F' o 'X'.\n"
    "  - 'fecha_nacimiento' formato YYYY-MM-DD.\n"
    "  - Si un campo no es legible, devolvé null (no adivines).\n"
    "  - Nombres respetá la capitalización del original pero corregí obvios errores de OCR."
)

# JSON Schema — mismo shape para Anthropic y OpenAI (structured outputs)
SCHEMA = {
    "type": "object",
    "properties": {
        "tipo": {"type": "string", "enum": ["dni", "planilla_aval", "credencial", "otro"]},
        "personas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dni":              {"type": ["string", "null"]},
                    "nombre_apellido":  {"type": ["string", "null"]},
                    "genero":           {"type": ["string", "null"]},
                    "tomo":             {"type": ["integer", "null"]},
                    "folio":            {"type": ["integer", "null"]},
                    "matricula":        {"type": ["string", "null"]},
                    "jurisdiccion":     {"type": ["string", "null"]},
                    "fecha_nacimiento": {"type": ["string", "null"]},
                },
                "required": ["dni", "nombre_apellido", "genero", "tomo", "folio",
                             "matricula", "jurisdiccion", "fecha_nacimiento"],
                "additionalProperties": False,
            },
        },
        "notas": {"type": ["string", "null"]},
    },
    "required": ["tipo", "personas", "notas"],
    "additionalProperties": False,
}

IMAGE_MEDIA_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}

# Anthropic rechaza imágenes > 8000px en cualquier lado. Opus 4.7 procesa en máx 2576px.
# Bajamos a 3000px (margen de seguridad, y ahorra ~30% de tokens).
MAX_IMAGE_DIM = 3000

MAT_RE = re.compile(r"^\s*[TtTº°]\s*[º°]?\s*(\d+)\s*[FfFº°]\s*[º°]?\s*(\d+)\s*$")


def _downscale_if_needed(image_bytes: bytes, media_type: str) -> tuple[bytes, str]:
    """Si la imagen supera MAX_IMAGE_DIM en algún lado, la redimensiona a JPEG."""
    if media_type not in IMAGE_MEDIA_TYPES:
        return image_bytes, media_type
    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        long_edge = max(w, h)
        if long_edge <= MAX_IMAGE_DIM:
            return image_bytes, media_type
        ratio = MAX_IMAGE_DIM / long_edge
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88, optimize=True)
        log.info("downscale %dx%d -> %dx%d", w, h, int(w * ratio), int(h * ratio))
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        log.warning("downscale falló, se envía original: %s", e)
        return image_bytes, media_type


def _empty(tipo: str, nota: str) -> dict:
    return {"tipo": tipo, "personas": [], "notas": nota, "_provider": None}


def _normalize_persona(p: dict) -> dict:
    """Limpieza + parseo de matrícula si vino como texto."""
    if p.get("dni"):
        p["dni"] = "".join(c for c in str(p["dni"]) if c.isdigit()) or None
    if p.get("genero"):
        g = str(p["genero"]).strip().upper()[:1]
        p["genero"] = g if g in ("M", "F", "X") else None
    for k in ("nombre_apellido", "matricula", "jurisdiccion", "fecha_nacimiento"):
        if p.get(k):
            p[k] = str(p[k]).strip() or None
    # Si vino matrícula texto pero no tomo/folio, intentar parsear
    if p.get("matricula") and (not p.get("tomo") or not p.get("folio")):
        m = MAT_RE.match(p["matricula"])
        if m:
            p["tomo"] = int(m.group(1))
            p["folio"] = int(m.group(2))
    return p


def _normalize_result(r: dict) -> dict:
    if "tipo" not in r: r["tipo"] = "otro"
    if "personas" not in r or r["personas"] is None: r["personas"] = []
    r["personas"] = [_normalize_persona(p) for p in r["personas"]]
    # descartar personas totalmente vacías
    r["personas"] = [p for p in r["personas"] if p.get("dni") or p.get("nombre_apellido")]
    return r


def _analyze_anthropic(image_bytes: bytes, media_type: str) -> Optional[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or media_type not in IMAGE_MEDIA_TYPES:
        return None
    image_bytes, media_type = _downscale_if_needed(image_bytes, media_type)
    client = anthropic.Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "low"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text: return None
    data = json.loads(text)
    data["_provider"] = f"anthropic:{ANTHROPIC_MODEL}"
    return _normalize_result(data)


def _analyze_openai(image_bytes: bytes, media_type: str) -> Optional[dict]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or media_type not in IMAGE_MEDIA_TYPES:
        return None
    image_bytes, media_type = _downscale_if_needed(image_bytes, media_type)
    client = OpenAI(api_key=api_key)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{media_type};base64,{b64}"
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        max_tokens=4096,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "avales_extraction", "strict": True, "schema": SCHEMA},
        },
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    )
    text = resp.choices[0].message.content
    if not text: return None
    data = json.loads(text)
    data["_provider"] = f"openai:{OPENAI_MODEL}"
    return _normalize_result(data)


def analyze_image(image_bytes: bytes, media_type: str) -> dict:
    """Analiza la imagen y devuelve dict con tipo + lista de personas detectadas."""
    if media_type not in IMAGE_MEDIA_TYPES:
        return _empty("otro", f"tipo no soportado por vision ({media_type})")
    try:
        r = _analyze_anthropic(image_bytes, media_type)
        if r: return r
    except Exception as e:
        log.warning("Anthropic falló, intentando OpenAI: %s", e)
    try:
        r = _analyze_openai(image_bytes, media_type)
        if r: return r
    except Exception as e:
        log.error("OpenAI también falló: %s", e)
        return _empty("otro", f"error de proveedores de vision: {e}")
    return _empty("otro", "ningún proveedor de vision disponible")


# Backward-compat alias
def analyze_dni(image_bytes: bytes, media_type: str) -> dict:
    return analyze_image(image_bytes, media_type)


# ─── Multi-página ────────────────────────────────────────────

def _normalize_documento(d: dict) -> dict:
    if "personas" not in d or d["personas"] is None:
        d["personas"] = []
    d["personas"] = [_normalize_persona(p) for p in d["personas"]]
    d["personas"] = [p for p in d["personas"] if p.get("dni") or (p.get("tomo") and p.get("folio")) or p.get("nombre_apellido")]
    if "paginas" not in d or not d["paginas"]:
        d["paginas"] = []
    return d


def _analyze_multi_anthropic(pages: list[bytes]) -> Optional[dict]:
    """pages: lista de bytes JPEG (una por página). Devuelve dict {documentos: [...]}."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not pages:
        return None
    client = anthropic.Anthropic(api_key=api_key)
    content = []
    for i, page_bytes in enumerate(pages, 1):
        img_bytes, media_type = _downscale_if_needed(page_bytes, "image/jpeg")
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        content.append({"type": "text", "text": f"--- Pagina {i} ---"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
    content.append({"type": "text", "text": PROMPT_MULTIPAGINA})

    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=8192,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA_MULTI}, "effort": "low"},
        messages=[{"role": "user", "content": content}],
    )
    text_out = next((b.text for b in resp.content if b.type == "text"), None)
    if not text_out:
        return None
    data = json.loads(text_out)
    data["_provider"] = f"anthropic:{ANTHROPIC_MODEL}"
    docs = data.get("documentos", [])
    data["documentos"] = [_normalize_documento(d) for d in docs]
    return data


def _analyze_multi_openai(pages: list[bytes]) -> Optional[dict]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or not pages:
        return None
    client = OpenAI(api_key=api_key)
    content: list = [{"type": "text", "text": PROMPT_MULTIPAGINA}]
    for i, page_bytes in enumerate(pages, 1):
        img_bytes, media_type = _downscale_if_needed(page_bytes, "image/jpeg")
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        content.append({"type": "text", "text": f"--- Pagina {i} ---"})
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{b64}"},
        })
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        max_tokens=8192,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "aval_multipagina", "strict": True, "schema": SCHEMA_MULTI},
        },
        messages=[{"role": "user", "content": content}],
    )
    text_out = resp.choices[0].message.content
    if not text_out:
        return None
    data = json.loads(text_out)
    data["_provider"] = f"openai:{OPENAI_MODEL}"
    docs = data.get("documentos", [])
    data["documentos"] = [_normalize_documento(d) for d in docs]
    return data


MAX_PAGES_PER_CALL = 20  # límite razonable para Anthropic multi-image


def analyze_pages(pages: list[bytes]) -> dict:
    """
    Analiza N páginas de un PDF como un solo bloque. Devuelve {documentos: [...], _provider}.
    Si son > MAX_PAGES_PER_CALL, chunkea y concatena.
    En caso de error, devuelve {documentos: [], _provider: None, error: str}.
    """
    if not pages:
        return {"documentos": [], "_provider": None, "notas": "sin paginas"}

    if len(pages) <= MAX_PAGES_PER_CALL:
        chunks = [(1, pages)]
    else:
        chunks = []
        for start in range(0, len(pages), MAX_PAGES_PER_CALL):
            chunks.append((start + 1, pages[start:start + MAX_PAGES_PER_CALL]))

    all_docs: list = []
    provider = None
    for offset, chunk in chunks:
        result = None
        try:
            result = _analyze_multi_anthropic(chunk)
        except Exception as e:
            log.warning("Anthropic multi falló, fallback OpenAI: %s", e)
        if result is None:
            try:
                result = _analyze_multi_openai(chunk)
            except Exception as e:
                log.error("OpenAI multi también falló: %s", e)
                continue
        if result is None:
            continue
        provider = result.get("_provider", provider)
        # Sumar offset a numeros de pagina para que sean absolutos en el PDF
        for d in result.get("documentos", []):
            d["paginas"] = [p + offset - 1 for p in d.get("paginas", [])]
            all_docs.append(d)

    return {"documentos": all_docs, "_provider": provider}
