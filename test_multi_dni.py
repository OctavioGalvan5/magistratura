"""Test rapido: renderizar el PDF y ver que devuelve la vision actual."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "app"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import io
import json
import pypdfium2 as pdfium
from dotenv import load_dotenv
load_dotenv()

from vision import analyze_dni

pdf_path = Path(r"c:\Users\octav\Downloads\Consejo de la magistratura\CamScanner 02-09-2026 21.31.pdf")
data = pdf_path.read_bytes()
print(f"PDF: {pdf_path.name} ({len(data)} bytes)")

pdf = pdfium.PdfDocument(data)
print(f"Paginas: {len(pdf)}")

for i in range(len(pdf)):
    page = pdf[i]
    pil = page.render(scale=200/72).to_pil()
    if pil.mode != "RGB":
        pil = pil.convert("RGB")
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=85, optimize=True)
    jpg = buf.getvalue()

    # Guardar la imagen para poder mirarla
    out = pdf_path.with_name(f"{pdf_path.stem}_p{i+1}.jpg")
    out.write_bytes(jpg)
    print(f"\n=== PAGINA {i+1} ({len(jpg)} bytes JPG, guardada en {out.name}) ===")

    r = analyze_dni(jpg, "image/jpeg")
    print(json.dumps(r, indent=2, ensure_ascii=False))
pdf.close()
