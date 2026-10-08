"""Hand-authored OOXML packages; no spreadsheet library defines expectations."""
from pathlib import Path
import zipfile

MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
DOCREL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKGREL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'


def worksheet(body: str, *, namespace=MAIN):
    return f'<worksheet xmlns="{namespace}"><sheetData>{body}</sheetData></worksheet>'


def xlsx_parts(sheets=None, shared=None):
    sheets = sheets or [('Visible', 'visible', worksheet('<row r="1"><c r="A1"><v>42</v></c></row>'))]
    overrides = ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                        for i in range(1, len(sheets) + 1))
    relationships = ''.join(f'<Relationship Id="rId{i}" Type="{DOCREL}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                            for i in range(1, len(sheets) + 1))
    sheet_entries = ''.join(f'<sheet name="{name}" sheetId="{i}" state="{state}" r:id="rId{i}"/>'
                           for i, (name, state, _) in enumerate(sheets, 1))
    if shared is not None:
        overrides += '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        relationships += f'<Relationship Id="strings" Type="{DOCREL}/sharedStrings" Target="sharedStrings.xml"/>'
    parts = {
        '[Content_Types].xml': f'<Types xmlns="{CT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>{overrides}</Types>',
        '_rels/.rels': f'<Relationships xmlns="{PKGREL}"><Relationship Id="main" Type="{DOCREL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': f'<workbook xmlns="{MAIN}" xmlns:r="{DOCREL}"><sheets>{sheet_entries}</sheets></workbook>',
        'xl/_rels/workbook.xml.rels': f'<Relationships xmlns="{PKGREL}">{relationships}</Relationships>',
    }
    parts.update({f'xl/worksheets/sheet{i}.xml': xml for i, (_, _, xml) in enumerate(sheets, 1)})
    if shared is not None:
        parts['xl/sharedStrings.xml'] = f'<sst xmlns="{MAIN}">{shared}</sst>'
    return parts


def write_xlsx(path: Path, parts=None, *, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, 'w', compression=compression) as archive:
        for name, value in (parts or xlsx_parts()).items():
            archive.writestr(name, value)
    return path
