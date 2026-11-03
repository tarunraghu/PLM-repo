"""Project document files: PDF (text layer), DOCX, and scanned-looking PNG.

Each document is built from the structured data of the parts it links to, as they were at
upload time, so extracted text can be checked against known answers. Every document also
writes a facts record (truth/doc_facts.jsonl) with the values it states.

Each call writes the files plus one metadata JSONL (file name, size, sha256 and the document's
fields), the way a document store would hand over a manifest.

Dirty mode adds defects at a fixed rate: a metadata row with no file, a truncated file (its
sha256 no longer matches), and a part number in the text that differs from the metadata link.
It also drops orphan files that have no metadata row.

PDF and DOCX are written by hand (standard library only). PNG scans need Pillow.
"""

import hashlib
import json
import zipfile
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from .schema import TABLES
from .util import FINISHES, MATERIALS, entity_rng, pn_of

# ---- content --------------------------------------------------------------------------------------

BANNER = {"ECCN-3A001": "EXPORT CONTROLLED - ECCN 3A001", "ITAR-USML-XIX": "ITAR CONTROLLED - USML CATEGORY XIX"}
TESTS = [("Tensile strength", "MPa", 250, 520), ("Hardness", "HRC", 20, 60), ("Flatness", "mm", 0.01, 0.2),
         ("Salt spray", "hours", 48, 500), ("Thermal cycle", "cycles", 50, 500), ("Dimensional check", "mm", 0.05, 0.5)]


def build_document(job, seed, wrong_part=None):
    """Return (blocks, facts). Blocks are simple layout instructions shared by every renderer."""
    g = entity_rng(seed, "docbody", job["document_id"])
    kind = job["document_type"]
    parts = job["parts"]
    shown = [dict(p) for p in parts]
    if wrong_part:
        shown[0]["part_number"] = wrong_part
    blocks = []
    if job["export_control_class"] in BANNER:
        blocks.append(("banner", BANNER[job["export_control_class"]]))
    blocks += [("title", kind.upper()), ("meta", [
        ("Document ID", job["document_id"]), ("Project", job["project_id"]), ("Uploaded on", job["uploaded_on"]),
        ("Uploaded by", job["uploaded_by"]), ("Export class", job["export_control_class"])])]
    facts = {"document_id": job["document_id"], "document_type": kind, "format": job["format"],
             "project_id": job["project_id"], "uploaded_on": job["uploaded_on"],
             "export_control_class": job["export_control_class"], "parts": []}

    if kind == "Spec":
        for p, s in zip(parts, shown):
            extra = {"material": g.choice(MATERIALS), "finish": g.choice(FINISHES),
                     "tolerance": f"+/- {g.choice([0.02, 0.05, 0.1, 0.2])} mm",
                     "operating_temperature": f"{g.choice([-55, -40, -20])} to {g.choice([85, 105, 125, 150])} C"}
            blocks += [("heading", f"Part {s['part_number']} - {s['part_name']}"), ("meta", [
                ("Part Number", s["part_number"]), ("Revision", s["revision"]), ("Lifecycle State", s["lifecycle"]),
                ("Mass (kg)", f"{s['mass_kg']:.3f}"), ("Unit of Measure", s["unit"]), ("Primary Supplier", s["supplier"]),
                ("Plant", s["plant"]), ("Material", extra["material"]), ("Finish", extra["finish"]),
                ("Tolerance", extra["tolerance"]), ("Operating Temperature", extra["operating_temperature"])])]
            facts["parts"].append({**_core(p), **extra})
    elif kind == "Drawing":
        for p, s in zip(parts, shown):
            dims = {"length_mm": round(g.uniform(20, 400), 1), "width_mm": round(g.uniform(10, 200), 1),
                    "height_mm": round(g.uniform(5, 100), 1)}
            extra = {"drawing_number": f"DWG-{g.randint(10000, 99999)}", "scale": g.choice(["1:1", "1:2", "2:1", "1:5"]),
                     "sheet": g.choice(["A4", "A3", "A2"]), **dims}
            blocks += [("heading", f"Drawing {extra['drawing_number']}"), ("table", ["Field", "Value"], [
                ["Part Number", s["part_number"]], ["Revision", s["revision"]], ["Scale", extra["scale"]],
                ["Sheet", extra["sheet"]], ["Length (mm)", f"{dims['length_mm']}"], ["Width (mm)", f"{dims['width_mm']}"],
                ["Height (mm)", f"{dims['height_mm']}"], ["Mass (kg)", f"{s['mass_kg']:.3f}"]])]
            facts["parts"].append({**_core(p), **extra})
    elif kind == "Test Report":
        overall = "PASS"
        blocks.append(("meta", [("Report No", f"TR-{g.randint(100000, 999999)}"), ("Laboratory", g.choice(["Lab North", "Lab South", "Central Test"])),
                                ("Test Date", job["uploaded_on"])]))
        for p, s in zip(parts, shown):
            rows, results = [], []
            for name, unit, low, high in g.sample(TESTS, 4):
                measured = round(g.uniform(low * 0.9, high * 1.1), 3)
                ok = low <= measured <= high
                results.append({"test": name, "unit": unit, "measured": measured, "result": "PASS" if ok else "FAIL"})
                rows.append([name, f"{low} - {high} {unit}", f"{measured}", "PASS" if ok else "FAIL"])
            part_result = "PASS" if all(r["result"] == "PASS" for r in results) else "FAIL"
            overall = "FAIL" if part_result == "FAIL" else overall
            blocks += [("heading", f"Part {s['part_number']} rev {s['revision']}"),
                       ("table", ["Test", "Requirement", "Measured", "Result"], rows),
                       ("para", f"Overall result for {s['part_number']}: {part_result}")]
            facts["parts"].append({**_core(p), "tests": results, "result": part_result})
        facts["overall_result"] = overall
    elif kind == "Certificate":
        rows = []
        for p, s in zip(parts, shown):
            lot, qty = f"LOT-{g.randint(100000, 999999)}", g.choice([50, 100, 250, 500, 1000])
            rows.append([s["part_number"], s["revision"], lot, str(qty), s["supplier"]])
            facts["parts"].append({**_core(p), "lot": lot, "quantity": qty})
        blocks += [("para", "This certifies that the items listed below conform to the applicable drawing and specification."),
                   ("table", ["Part Number", "Revision", "Lot", "Quantity", "Supplier"], rows),
                   ("para", f"Certificate No CERT-{g.randint(100000, 999999)}. Issued {job['uploaded_on']}.")]
    else:  # Work Instruction
        for p, s in zip(parts, shown):
            steps = g.randint(5, 8)
            extra = {"station": f"ST-{g.randint(10, 99)}", "torque_nm": g.choice([2.5, 5, 8, 12, 20]),
                     "cycle_time_s": g.randint(30, 300), "steps": steps}
            blocks += [("heading", f"Assembly of {s['part_number']} at plant {s['plant']}"), ("meta", [
                ("Part Number", s["part_number"]), ("Revision", s["revision"]), ("Plant", s["plant"]),
                ("Station", extra["station"]), ("Torque (Nm)", str(extra["torque_nm"])),
                ("Cycle time (s)", str(extra["cycle_time_s"]))])]
            blocks += [("para", f"Step {i}: " + g.choice(["Inspect incoming part", "Clean mating surface", "Apply thread locker",
                                                          "Fit fasteners", "Torque to specification", "Record result",
                                                          "Visual check", "Move to next station"])) for i in range(1, steps + 1)]
            facts["parts"].append({**_core(p), **extra})
    return blocks, facts


def _core(p):
    return {"part_number": p["part_number"], "part_name": p["part_name"], "revision": p["revision"],
            "lifecycle": p["lifecycle"], "mass_kg": p["mass_kg"], "unit": p["unit"], "supplier": p["supplier"],
            "plant": p["plant"], "export_control_class": p["export_control_class"]}


# ---- PDF ------------------------------------------------------------------------------------------

def _pdf_text(s):
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class _Pdf:
    W, H, M = 595, 842, 56

    def __init__(self):
        self.pages, self.ops, self.y = [], [], self.H - self.M
        self._new()

    def _new(self):
        if self.ops:
            self.pages.append("\n".join(self.ops))
        self.ops, self.y = [], self.H - self.M

    def need(self, height):
        if self.y - height < self.M:
            self._new()

    def text(self, x, y, s, size=10, bold=False):
        self.ops.append(f"BT /{'F2' if bold else 'F1'} {size} Tf {x:.1f} {y:.1f} Td ({_pdf_text(s)}) Tj ET")

    def line(self, x1, y1, x2, y2):
        self.ops.append(f"0.5 w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

    def rect(self, x, y, w, h):
        self.ops.append(f"1 w {x:.1f} {y:.1f} {w:.1f} {h:.1f} re S")

    def build(self, title):
        if self.ops:
            self.pages.append("\n".join(self.ops))
        objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
        kids = []
        for content in self.pages:
            page_no = len(objs) + 1
            kids.append(f"{page_no} 0 R")
            objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.W} {self.H}] "
                        f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {page_no + 1} 0 R >>")
            stream = content.encode("latin-1", "replace")
            objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode('latin-1')}\nendstream")
        objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
        info_no = len(objs) + 1
        objs.append(f"<< /Title ({_pdf_text(title)}) /Producer (plmgen) >>")
        out = bytearray(b"%PDF-1.4\n")
        offsets = []
        for n, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += f"{n} 0 obj\n{body}\nendobj\n".encode("latin-1", "replace")
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {info_no} 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
        return bytes(out)


def _wrap(text, width, size):
    per_line = max(10, int(width / (size * 0.5)))
    words, lines, cur = text.split(), [], ""
    for word in words:
        if len(cur) + len(word) + 1 > per_line and cur:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return lines + ([cur] if cur else [])


def render_pdf(blocks, title):
    pdf = _Pdf()
    width = pdf.W - 2 * pdf.M
    for block in blocks:
        kind = block[0]
        if kind == "banner":
            pdf.need(30)
            pdf.rect(pdf.M, pdf.y - 20, width, 22)
            pdf.text(pdf.M + 8, pdf.y - 14, block[1], 11, True)
            pdf.y -= 34
        elif kind == "title":
            pdf.need(30)
            pdf.text(pdf.M, pdf.y - 16, block[1], 18, True)
            pdf.y -= 30
        elif kind == "heading":
            pdf.need(34)
            pdf.y -= 8
            pdf.text(pdf.M, pdf.y - 12, block[1], 12, True)
            pdf.y -= 22
        elif kind == "meta":
            for key, value in block[1]:
                pdf.need(15)
                pdf.text(pdf.M, pdf.y - 10, f"{key}:", 10, True)
                pdf.text(pdf.M + 150, pdf.y - 10, str(value), 10)
                pdf.y -= 15
            pdf.y -= 6
        elif kind == "para":
            for ln in _wrap(block[1], width, 10):
                pdf.need(14)
                pdf.text(pdf.M, pdf.y - 10, ln, 10)
                pdf.y -= 14
            pdf.y -= 4
        elif kind == "table":
            header, rows = block[1], block[2]
            col = width / len(header)
            per_cell = max(4, int(col / 5.2))
            for r, cells in enumerate([header] + rows):
                pdf.need(18)
                for c, cell in enumerate(cells):
                    pdf.text(pdf.M + c * col + 3, pdf.y - 12, str(cell)[:per_cell], 9.5, r == 0)
                pdf.line(pdf.M, pdf.y - 16, pdf.M + width, pdf.y - 16)
                pdf.y -= 18
            pdf.y -= 8
    return pdf.build(title)


# ---- DOCX -----------------------------------------------------------------------------------------

def _run(text, bold=False, size=22):
    rpr = f"<w:rPr>{'<w:b/>' if bold else ''}<w:sz w:val=\"{size}\"/></w:rPr>"
    return f"<w:r>{rpr}<w:t xml:space=\"preserve\">{escape(str(text))}</w:t></w:r>"


def _paragraph(*runs):
    return f"<w:p>{''.join(runs)}</w:p>"


def render_docx(blocks, title):
    body = []
    for block in blocks:
        kind = block[0]
        if kind == "banner":
            body.append(_paragraph(_run(block[1], True, 24)))
        elif kind == "title":
            body.append(_paragraph(_run(block[1], True, 36)))
        elif kind == "heading":
            body.append(_paragraph(_run(block[1], True, 28)))
        elif kind == "meta":
            body += [_paragraph(_run(f"{k}: ", True), _run(v)) for k, v in block[1]]
        elif kind == "para":
            body.append(_paragraph(_run(block[1])))
        elif kind == "table":
            rows = []
            for r, cells in enumerate([block[1]] + block[2]):
                rows.append("<w:tr>" + "".join(f"<w:tc>{_paragraph(_run(c, r == 0))}</w:tc>" for c in cells) + "</w:tr>")
            borders = "".join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
                              for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
            body.append(f"<w:tbl><w:tblPr><w:tblBorders>{borders}</w:tblBorders></w:tblPr>{''.join(rows)}</w:tbl>")
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                + "".join(body) + "</w:body></w:document>")
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in (
            ("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'),
            ("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'),
            ("word/document.xml", document)):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))   # fixed timestamp keeps output reproducible
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, content)
    return out.getvalue()


# ---- scanned PNG ------------------------------------------------------------------------------------

def render_png(blocks, seed_key):
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    rng = entity_rng(0, "scan", seed_key)
    w, h, margin = 1000, 1400, 70
    img = Image.new("L", (w, h), 246)
    draw = ImageDraw.Draw(img)
    regular, bold = ImageFont.load_default(size=19), ImageFont.load_default(size=24)
    y = margin
    for block in blocks:
        kind = block[0]
        if kind in ("banner", "title", "heading"):
            draw.text((margin, y), block[1], font=bold, fill=20)
            y += 42
        elif kind == "meta":
            for key, value in block[1]:
                draw.text((margin, y), f"{key}:", font=regular, fill=25)
                draw.text((margin + 260, y), str(value), font=regular, fill=25)
                y += 30
            y += 10
        elif kind == "para":
            for ln in _wrap(block[1], w - 2 * margin, 19 * 0.9):
                draw.text((margin, y), ln, font=regular, fill=25)
                y += 28
            y += 8
        elif kind == "table":
            col = (w - 2 * margin) / len(block[1])
            for r, cells in enumerate([block[1]] + block[2]):
                for c, cell in enumerate(cells):
                    draw.text((margin + c * col + 4, y), str(cell)[: int(col / 11)], font=regular, fill=25)
                draw.line((margin, y + 26, w - margin, y + 26), fill=90, width=1)
                y += 32
            y += 14
        if y > h - margin:
            break
    for _ in range(1800):                                     # scanner specks
        x, yy = rng.randrange(w), rng.randrange(h)
        draw.point((x, yy), fill=rng.randrange(120, 200))
    img = img.rotate(rng.uniform(-1.4, 1.4), resample=Image.BICUBIC, fillcolor=238)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ---- writing --------------------------------------------------------------------------------------

def render(job, seed, wrong_part=None):
    blocks, facts = build_document(job, seed, wrong_part)
    fmt = job["format"]
    if fmt == "pdf":
        data = render_pdf(blocks, job["title"])
    elif fmt == "docx":
        data = render_docx(blocks, job["title"])
    else:
        data = render_png(blocks, job["document_id"])
    return data, facts


def write_documents(jobs, target_dir, seed, dirty=False, truth_file=None):
    """Write document files and metadata JSON for the given jobs. Returns a summary dict."""
    target = Path(target_dir)
    (target / "files").mkdir(parents=True, exist_ok=True)
    (target / "metadata").mkdir(parents=True, exist_ok=True)
    summary = {"documents": 0, "missing_file": 0, "corrupt_file": 0, "wrong_part_in_text": 0, "orphan_files": 0}
    truth, meta_lines = [], []
    for job in jobs:
        rng = entity_rng(seed, "docdefect", job["document_id"])
        roll = rng.random() if dirty else 1.0
        defect = None
        wrong = None
        if roll < 0.005:
            defect = "missing_file"
        elif roll < 0.010:
            defect = "corrupt_file"
        elif roll < 0.020:
            defect = "wrong_part_in_text"
            wrong = pn_of(rng.randint(1, 999))
        data, facts = render(job, seed, wrong)
        name = f"{job['document_id']}.{job['format']}"
        meta = {
            "document_id": job["document_id"], "file_name": name, "file_format": job["format"],
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "uploaded_on": job["uploaded_on"],
            "uploaded_by": job["uploaded_by"], "project_id": job["project_id"], "document_type": job["document_type"],
            "title": job["title"], "export_control_class": job["export_control_class"],
            "parts": [{"part_number": p["part_number"], "part_revision": p["revision"]} for p in job["parts"]]}
        meta_lines.append(json.dumps(meta, separators=(",", ":")))
        if defect == "missing_file":
            summary["missing_file"] += 1
        else:
            (target / "files" / name).write_bytes(data[: int(len(data) * 0.6)] if defect == "corrupt_file" else data)
            summary["corrupt_file"] += defect == "corrupt_file"
        summary["wrong_part_in_text"] += defect == "wrong_part_in_text"
        facts["file_name"] = name
        facts["defect"] = defect
        if wrong:
            facts["text_part_number"] = wrong
        truth.append(facts)
        summary["documents"] += 1

    if meta_lines:
        name = f"docs-{jobs[0]['document_id']}-{jobs[-1]['document_id']}.jsonl"
        (target / "metadata" / name).write_text("\n".join(meta_lines) + "\n", encoding="utf-8")

    if dirty and jobs:
        # About one orphan file per 300 documents, as an expectation so that small daily batches add up.
        rate = len(jobs) / 300
        rng = entity_rng(seed, "orphan-count", jobs[0]["document_id"])
        orphans = int(rate) + (1 if rng.random() < rate - int(rate) else 0)
        for i in range(orphans):
            rng = entity_rng(seed, "orphan", jobs[0]["document_id"], i)
            job = {**jobs[rng.randrange(len(jobs))], "document_id": f"ORPHAN-{jobs[0]['document_id'][4:]}-{i}", "format": "pdf"}
            data, _ = render(job, seed)
            (target / "files" / f"{job['document_id']}.pdf").write_bytes(data)
            summary["orphan_files"] += 1
    if truth_file:
        Path(truth_file).parent.mkdir(parents=True, exist_ok=True)
        with Path(truth_file).open("a", encoding="utf-8", newline="\n") as fh:
            for facts in truth:
                fh.write(json.dumps(facts, separators=(",", ":")) + "\n")
    return summary
