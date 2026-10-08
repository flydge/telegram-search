"""Fixed synthetic PresentationML packages; no Telegram or user data."""
from pathlib import Path
from xml.sax.saxutils import escape
import zipfile

P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
DOCREL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKGREL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'


def shape_xml(paragraphs, *, identity=2):
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{identity}" name="synthetic"/>'
            '<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/>'
            f'<p:txBody><a:bodyPr/><a:lstStyle/>{paragraphs}</p:txBody></p:sp>')


def text_shape(text, *, identity=2):
    return shape_xml('<a:p><a:r><a:t>' + escape(text) + '</a:t></a:r></a:p>', identity=identity)


def slide_xml(body, *, hidden=False, notes=False):
    kind = 'notes' if notes else 'sld'
    show = '' if notes or not hidden else ' show="0"'
    return (f'<p:{kind} xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{DOCREL}"{show}>'
            '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/>'
            '<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
            f'{body}</p:spTree></p:cSld></p:{kind}>')


def pptx_parts(slides=None):
    slides = slides or [dict(body=text_shape('synthetic slide'))]
    parts = {'_rels/.rels': f'<Relationships xmlns="{PKGREL}"><Relationship Id="main" '
             f'Type="{DOCREL}/officeDocument" Target="ppt/presentation.xml"/></Relationships>'}
    ids, rels, overrides = [], [], []
    for index, value in enumerate(slides, 1):
        part = value.get('part', f'slide{index}.xml')
        path = 'ppt/slides/' + part
        parts[path] = slide_xml(value.get('body', ''), hidden=value.get('hidden', False))
        ids.append(f'<p:sldId id="{255 + index}" r:id="slide{index}"/>')
        rels.append(f'<Relationship Id="slide{index}" Type="{DOCREL}/slide" Target="slides/{part}"/>')
        overrides.append(f'<Override PartName="/{path}" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>')
        if value.get('notes') is not None:
            notes = f'ppt/notesSlides/notes{index}.xml'
            parts[notes] = slide_xml(value['notes'], notes=True)
            parts[f'ppt/slides/_rels/{part}.rels'] = (f'<Relationships xmlns="{PKGREL}"><Relationship Id="notes" '
                f'Type="{DOCREL}/notesSlide" Target="../notesSlides/notes{index}.xml"/></Relationships>')
            parts[f'ppt/notesSlides/_rels/notes{index}.xml.rels'] = (f'<Relationships xmlns="{PKGREL}"><Relationship Id="slide" '
                f'Type="{DOCREL}/slide" Target="../slides/{part}"/></Relationships>')
            overrides.append(f'<Override PartName="/{notes}" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>')
    parts['ppt/presentation.xml'] = (f'<p:presentation xmlns:p="{P}" xmlns:r="{DOCREL}"><p:sldIdLst>'
                                   + ''.join(ids) + '</p:sldIdLst></p:presentation>')
    parts['ppt/_rels/presentation.xml.rels'] = f'<Relationships xmlns="{PKGREL}">' + ''.join(rels) + '</Relationships>'
    parts['[Content_Types].xml'] = (f'<Types xmlns="{CT}"><Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        + ''.join(overrides) + '</Types>')
    return parts


def make_pptx(path, parts=None, *, compression=zipfile.ZIP_STORED):
    path = Path(path)
    with zipfile.ZipFile(path, 'w', compression=compression) as archive:
        for name, content in (pptx_parts() if parts is None else parts).items():
            archive.writestr(name, content)
    return path
