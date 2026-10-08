"""Bounded, hash-pinned passive Transitional PresentationML shape-text reader.

Runs concatenate verbatim a:t text; a:br and paragraph boundaries are LF.
Text-bearing shapes (including empty shapes) join with LF in shape-tree order.
Groups recurse in document order. Notes are opt-in shape text, including any
stored header/footer shapes, not rendered speaker view. Field display text,
layout/master text, formatting and all other objects are omitted. Detected
objects are counted, without any claim of exhaustive visual fidelity.

Every package XML, content-type declaration and relationship is checked, even
when unused. The passive type/relationship allowlists below deliberately reject
unrecognized families. This is a conservative subset, not an OOXML schema
validator. It never renders, evaluates, follows links, or executes content.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import zipfile


def _load_bounds():
    # The isolated worker imports no package or arbitrary search-path module.
    # Load only this exact installed sibling's generic low-level helpers.
    identity = '_telegram_pptx_generic_bounds'
    specification = importlib.util.spec_from_file_location(identity, Path(__file__).with_name('xlsx_reader.py'))
    if specification is None or specification.loader is None:
        raise ImportError('bounded helpers unavailable')
    module = importlib.util.module_from_spec(specification)
    sys.modules[identity] = module
    specification.loader.exec_module(module)
    return module


_common = _load_bounds()
_fail = _common._fail
_ReadFailure = _common._ReadFailure
MAX_INPUT_BYTES = _common.MAX_INPUT_BYTES
MAX_EXPANSION_BYTES = _common.MAX_EXPANSION_BYTES
MAX_XML_BYTES = _common.MAX_XML_BYTES
MAX_ZIP_MEMBERS = _common.MAX_ZIP_MEMBERS
MAX_COMPRESSION_RATIO = _common.MAX_COMPRESSION_RATIO
MAX_SELECTED_CHARS = _common.MAX_SELECTED_CHARS
MAX_PAGE_CHARS = _common.MAX_PAGE_CHARS
MAX_WORKER_OUTPUT_BYTES = _common.MAX_WORKER_OUTPUT_BYTES
MAX_REQUEST_BYTES = _common.MAX_REQUEST_BYTES
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
DOCREL = _common.DOCREL
PKGREL = _common.PKGREL
XML = _common.XML
CT = _common.CT
REL_TYPE = _common.REL_TYPE
PREFIX = 'application/vnd.openxmlformats-officedocument.'
PRESENTATION_TYPE = PREFIX + 'presentationml.presentation.main+xml'
SLIDE_TYPE = PREFIX + 'presentationml.slide+xml'
NOTES_TYPE = PREFIX + 'presentationml.notesSlide+xml'

# Explicit passive families, including graphics/media that are never extracted.
_ROOTS = {
    PRESENTATION_TYPE: (P, 'presentation'), SLIDE_TYPE: (P, 'sld'), NOTES_TYPE: (P, 'notes'),
    PREFIX + 'presentationml.slideLayout+xml': (P, 'sldLayout'),
    PREFIX + 'presentationml.slideMaster+xml': (P, 'sldMaster'),
    PREFIX + 'presentationml.notesMaster+xml': (P, 'notesMaster'),
    PREFIX + 'presentationml.handoutMaster+xml': (P, 'handoutMaster'),
    PREFIX + 'presentationml.presProps+xml': (P, 'presentationPr'),
    PREFIX + 'presentationml.viewProps+xml': (P, 'viewPr'),
    PREFIX + 'presentationml.tableStyles+xml': (A, 'tblStyleLst'),
    PREFIX + 'presentationml.commentAuthors+xml': (P, 'cmAuthorLst'),
    PREFIX + 'presentationml.comments+xml': (P, 'cmLst'),
    PREFIX + 'presentationml.tags+xml': (P, 'tagLst'),
    PREFIX + 'theme+xml': (A, 'theme'),
    PREFIX + 'drawingml.chart+xml': ('http://schemas.openxmlformats.org/drawingml/2006/chart', 'chartSpace'),
    PREFIX + 'drawingml.diagramData+xml': ('http://schemas.openxmlformats.org/drawingml/2006/diagram', 'dataModel'),
    PREFIX + 'drawingml.diagramLayout+xml': ('http://schemas.openxmlformats.org/drawingml/2006/diagram', 'layoutDef'),
    PREFIX + 'drawingml.diagramColors+xml': ('http://schemas.openxmlformats.org/drawingml/2006/diagram', 'colorsDef'),
    PREFIX + 'drawingml.diagramStyle+xml': ('http://schemas.openxmlformats.org/drawingml/2006/diagram', 'styleDef'),
    'application/vnd.openxmlformats-package.core-properties+xml': ('http://schemas.openxmlformats.org/package/2006/metadata/core-properties', 'coreProperties'),
    PREFIX + 'extended-properties+xml': ('http://schemas.openxmlformats.org/officeDocument/2006/extended-properties', 'Properties'),
    PREFIX + 'custom-properties+xml': ('http://schemas.openxmlformats.org/officeDocument/2006/custom-properties', 'Properties'),
}
_IMAGES = {'image/png', 'image/jpeg', 'image/gif', 'image/tiff', 'image/bmp', 'image/x-emf', 'image/x-wmf'}
_AUDIO = {'audio/mpeg', 'audio/wav', 'audio/x-wav', 'audio/mp4', 'audio/x-ms-wma'}
_VIDEO = {'video/mp4', 'video/mpeg', 'video/quicktime', 'video/x-ms-wmv', 'video/x-msvideo'}
_CONTENT_TYPES = set(_ROOTS) | _IMAGES | _AUDIO | _VIDEO | {REL_TYPE, 'application/xml', 'text/xml'}
_EXPECTED_RELATIONS = {
    'officeDocument': {PRESENTATION_TYPE}, 'slide': {SLIDE_TYPE}, 'notesSlide': {NOTES_TYPE},
    'slideLayout': {PREFIX + 'presentationml.slideLayout+xml'},
    'slideMaster': {PREFIX + 'presentationml.slideMaster+xml'},
    'notesMaster': {PREFIX + 'presentationml.notesMaster+xml'},
    'handoutMaster': {PREFIX + 'presentationml.handoutMaster+xml'},
    'presProps': {PREFIX + 'presentationml.presProps+xml'},
    'viewProps': {PREFIX + 'presentationml.viewProps+xml'},
    'tableStyles': {PREFIX + 'presentationml.tableStyles+xml'},
    'commentAuthors': {PREFIX + 'presentationml.commentAuthors+xml'},
    'comments': {PREFIX + 'presentationml.comments+xml'},
    'tags': {PREFIX + 'presentationml.tags+xml'}, 'theme': {PREFIX + 'theme+xml'},
    'chart': {PREFIX + 'drawingml.chart+xml'}, 'diagramData': {PREFIX + 'drawingml.diagramData+xml'},
    'diagramLayout': {PREFIX + 'drawingml.diagramLayout+xml'},
    'diagramColors': {PREFIX + 'drawingml.diagramColors+xml'},
    'diagramQuickStyle': {PREFIX + 'drawingml.diagramStyle+xml'},
    'image': _IMAGES, 'audio': _AUDIO, 'video': _VIDEO,
    'extended-properties': {PREFIX + 'extended-properties+xml'},
    'custom-properties': {PREFIX + 'custom-properties+xml'},
    'hyperlink': _CONTENT_TYPES - {REL_TYPE},
}
_RELATIONS = {DOCREL + '/' + name: kinds for name, kinds in _EXPECTED_RELATIONS.items()}
_RELATIONS[PKGREL + '/metadata/core-properties'] = {'application/vnd.openxmlformats-package.core-properties+xml'}
_RELATIONS['http://schemas.microsoft.com/office/2007/relationships/media'] = _AUDIO | _VIDEO
_STATUS = {'catalog', 'complete', 'unsupported', 'invalid_selection', 'limit_reached', 'error'}
_SUCCESS_DETAIL = ('Only supported stored shape text and explicitly requested notes are returned; '
                   'layout/master text, formatting, rendering, OCR and other objects are omitted. '
                   'Detected object counts do not establish visual completeness.')
_DETAILS = {'', _SUCCESS_DETAIL, 'invalid_arguments', 'invalid_slides', 'artifact_changed', 'source_invalid',
            'input_limit', 'expansion_limit', 'xml_limit', 'selection_limit', 'response_limit',
            'unsupported_package', 'unsupported_xml', 'invalid_package', 'parser_error',
            'worker_timeout', 'worker_limit', 'worker_error'}


@dataclass(frozen=True)
class PresentationReadResult:
    status: str
    catalog: tuple[dict, ...] = ()
    slides: tuple[dict, ...] = ()
    detail: str = ''


def _validate_arguments(sha256, size_bytes, name, mime_type, slides, include_notes, timeout):
    if (type(size_bytes) is not int or size_bytes < 0 or type(sha256) is not str
            or re.fullmatch(r'[0-9a-f]{64}', sha256) is None or type(include_notes) is not bool
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
            or (name is not None and (type(name) is not str or len(name) > 1024))
            or (mime_type is not None and (type(mime_type) is not str or len(mime_type) > 256))):
        _fail('invalid_arguments', 'invalid_selection')
    if slides is not None and (type(slides) is not tuple or not 1 <= len(slides) <= 5
            or any(type(index) is not int or not 1 <= index <= 128 for index in slides)
            or len(set(slides)) != len(slides)):
        _fail('invalid_slides', 'invalid_selection')


def _relationships(trees, members, types):
    relations, notes_owners = {}, {}
    for name, root in trees.items():
        if not name.endswith('.rels'):
            continue
        source = _common._relationship_source(name)
        if source and source not in members:
            _fail()
        if root.tag != f'{{{PKGREL}}}Relationships' or root.attrib or (root.text or '').strip():
            _fail('unsupported_package', 'unsupported')
        items, slide_targets = {}, set()
        for element in root:
            if (element.tag != f'{{{PKGREL}}}Relationship' or not {'Id', 'Type', 'Target'} <= set(element.attrib)
                    or set(element.attrib) - {'Id', 'Type', 'Target', 'TargetMode'} or len(element)):
                _fail()
            identity, kind = element.get('Id'), element.get('Type')
            if (not identity or len(identity) > 256 or identity in items or any(ch.isspace() for ch in identity)
                    or (element.text or '').strip() or (element.tail or '').strip()):
                _fail()
            if kind not in _RELATIONS or element.get('TargetMode', 'Internal') != 'Internal':
                _fail('unsupported_package', 'unsupported')
            lexical_target = element.get('Target')
            if type(lexical_target) is not str:
                _fail()
            # OPC siblings legitimately use leading ../. Dot segments in the
            # remaining path create aliases and are not part of this subset.
            remainder = lexical_target.lstrip('/')
            while remainder.startswith('../'):
                remainder = remainder[3:]
            if '..' in remainder.split('/'):
                _fail()
            target = _common._target(source, lexical_target)
            if target not in members:
                _fail()
            if types.get(target) not in _RELATIONS[kind]:
                _fail('unsupported_package', 'unsupported')
            if kind in {DOCREL + '/slide', DOCREL + '/notesSlide'}:
                if (kind, target) in slide_targets:
                    _fail()
                slide_targets.add((kind, target))
            items[identity] = (kind, target)
        notes = [target for kind, target in items.values() if kind == DOCREL + '/notesSlide']
        if notes and (types.get(source) != SLIDE_TYPE or len(notes) != 1 or notes[0] in notes_owners):
            _fail()
        if notes:
            notes_owners[notes[0]] = source
        if (types.get(source) == NOTES_TYPE and sum(kind == DOCREL + '/slide' for kind, target in items.values()) > 1
                or source and any(kind == DOCREL + '/officeDocument' for kind, target in items.values())):
            _fail()
        relations[source] = items
    for path, owner in notes_owners.items():
        backlinks = [target for kind, target in relations.get(path, {}).values() if kind == DOCREL + '/slide']
        if backlinks and backlinks != [owner]:
            _fail()
    return relations


def _load_package(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_MEMBERS or sum(item.file_size for item in entries) > MAX_EXPANSION_BYTES:
            _fail('expansion_limit', 'limit_reached')
        members, aliases = {}, set()
        for item in entries:
            name = item.orig_filename
            _common._part_name(name[:-1] if item.is_dir() else name)
            if item.is_dir() and item.file_size:
                _fail()
            if name.lower().endswith('.rels') and not name.endswith('.rels'):
                _fail('unsupported_package', 'unsupported')
            if (name.lower() in aliases or name != item.filename or stat.S_ISLNK(item.external_attr >> 16)
                    or stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                _fail()
            aliases.add(name.lower())
            if item.flag_bits & 1 or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                _fail('unsupported_package', 'unsupported')
            if item.file_size > max(1, item.compress_size) * MAX_COMPRESSION_RATIO:
                _fail('expansion_limit', 'limit_reached')
            if not item.is_dir():
                members[name] = item
        if '[Content_Types].xml' not in members:
            _fail()
        names = ['[Content_Types].xml'] + [name for name in members if name != '[Content_Types].xml']
        trees, types, budget, expansion = {}, {}, _common._XMLBudget(), 0
        for name in names:
            item = members[name]
            is_xml = (name.endswith(('.xml', '.rels')) or types.get(name, '').endswith('+xml')
                      or types.get(name) in {'application/xml', 'text/xml'})
            maximum = MAX_XML_BYTES if is_xml else MAX_EXPANSION_BYTES
            if item.file_size > maximum:
                _fail('xml_limit' if is_xml else 'expansion_limit', 'limit_reached')
            chunks, size = [], 0
            with archive.open(item) as source:
                while value := source.read(min(65536, maximum - size + 1)):
                    size += len(value)
                    expansion += len(value)
                    if size > maximum or expansion > MAX_EXPANSION_BYTES or size > max(1, item.compress_size) * MAX_COMPRESSION_RATIO:
                        _fail('expansion_limit', 'limit_reached')
                    if is_xml:
                        chunks.append(value)
            if size != item.file_size:
                _fail()
            if is_xml:
                trees[name] = budget.parse(b''.join(chunks))
            if name == '[Content_Types].xml':
                types = _common._content_types(trees[name], members)
                if any(child.get('ContentType') not in _CONTENT_TYPES for child in trees[name]):
                    _fail('unsupported_package', 'unsupported')
                if any((kind == REL_TYPE) != part.endswith('.rels') for part, kind in types.items()):
                    _fail('unsupported_package', 'unsupported')
        # Package metadata namespaces cannot be disguised as passive generic
        # XML or nested inside omitted object/extension subtrees. Canonical
        # relationship/content-type documents receive their full validation
        # below and above; every other parsed part must exclude these nodes.
        for name, root in trees.items():
            forbidden = ({CT} if name != '[Content_Types].xml' else set())
            if not name.endswith('.rels'):
                forbidden.add(PKGREL)
            for element in root.iter():
                if any(element.tag.startswith('{' + namespace + '}')
                       or any(key.startswith('{' + namespace + '}') for key in element.attrib)
                       for namespace in forbidden):
                    _fail('unsupported_package', 'unsupported')
        relations = _relationships(trees, members, types)
        for name, root in trees.items():
            if types.get(name) in _ROOTS and root.tag != '{%s}%s' % _ROOTS[types[name]]:
                _fail('unsupported_xml', 'unsupported')
            for element in root.iter():
                if ('purl.oclc.org/ooxml' in element.tag or any('purl.oclc.org/ooxml' in key for key in element.attrib)
                        or element.tag.rsplit('}', 1)[-1].lower() in {'oleobj', 'oleobject', 'control', 'controls', 'vba', 'script'}):
                    _fail('unsupported_xml', 'unsupported')
                for key, value in element.attrib.items():
                    if key.startswith('{' + DOCREL + '}'):
                        local = key.split('}', 1)[1]
                        if local not in {'id', 'embed', 'link', 'dm', 'lo', 'qs', 'cs'} or value not in relations.get(name, {}):
                            _fail()
                        kind = relations[name][value][0]
                        expected = {'dm': 'diagramData', 'lo': 'diagramLayout', 'qs': 'diagramQuickStyle', 'cs': 'diagramColors'}.get(local)
                        if element.tag == f'{{{A}}}blip':
                            expected = 'image'
                        if element.tag in {f'{{{A}}}audioFile', f'{{{A}}}wavAudioFile'}:
                            expected = 'audio'
                        if element.tag in {f'{{{A}}}videoFile', f'{{{A}}}quickTimeFile'}:
                            expected = 'video'
                        if element.tag in {f'{{{P}}}sldId', f'{{{P}}}sld'}:
                            expected = 'slide'
                        if element.tag in {f'{{{P}}}sldMasterId', f'{{{P}}}notesMasterId', f'{{{P}}}handoutMasterId'}:
                            expected = element.tag.split('}', 1)[1].replace('sld', 'slide').removesuffix('Id')
                        if element.tag == '{http://schemas.openxmlformats.org/drawingml/2006/chart}chart':
                            expected = 'chart'
                        if element.tag == '{http://schemas.microsoft.com/office/powerpoint/2010/main}media' and kind not in {DOCREL + '/audio', DOCREL + '/video', 'http://schemas.microsoft.com/office/2007/relationships/media'}:
                            _fail('unsupported_package', 'unsupported')
                        if expected is not None and kind != DOCREL + '/' + expected:
                            _fail('unsupported_package', 'unsupported')
                    if key == 'action' and any(word in value.lower() for word in ('macro', 'program', 'script', 'ole')):
                        _fail('unsupported_xml', 'unsupported')
        return trees, types, relations


def _nontext(element):
    if (element.text or '').strip() or any((child.tail or '').strip() for child in element):
        _fail('unsupported_xml', 'unsupported')


def _formatting(element):
    """Omitted known properties may not conceal supported shape text."""
    if element.tag in {f'{{{P}}}extLst', f'{{{A}}}extLst'}:
        return
    if element.tag in {f'{{{A}}}t', f'{{{A}}}r', f'{{{A}}}p', f'{{{P}}}txBody', f'{{{P}}}sp', f'{{{P}}}grpSp'}:
        _fail('unsupported_xml', 'unsupported')
    _nontext(element)
    for child in element:
        _formatting(child)


def _one(parent, tag, *, required=True):
    matches = parent.findall(tag)
    if len(matches) > 1 or (required and not matches):
        _fail()
    return matches[0] if matches else None


def _bool(value):
    if value not in {'0', '1', 'false', 'true'}:
        _fail('unsupported_xml', 'unsupported')
    return value in {'1', 'true'}


def _catalog(trees, types, relations):
    main = [target for kind, target in relations.get('', {}).values() if kind == DOCREL + '/officeDocument']
    if len(main) != 1 or [name for name, kind in types.items() if kind == PRESENTATION_TYPE] != main:
        _fail('unsupported_package', 'unsupported')
    presentation = trees[main[0]]
    _nontext(presentation)
    passive_children = {'sldMasterIdLst', 'notesMasterIdLst', 'handoutMasterIdLst', 'sldIdLst', 'sldSz',
                        'notesSz', 'smartTags', 'embeddedFontLst', 'custShowLst', 'photoAlbum', 'custDataLst',
                        'kinsoku', 'defaultTextStyle', 'modifyVerifier', 'extLst'}
    if any(child.tag not in {f'{{{P}}}{name}' for name in passive_children} for child in presentation):
        _fail('unsupported_xml', 'unsupported')
    listing = _one(presentation, f'{{{P}}}sldIdLst')
    _nontext(listing)
    if listing.attrib or not len(listing):
        _fail()
    if len(listing) > 128:
        _fail('selection_limit', 'limit_reached')
    catalog, paths, notes_paths, identities, used_notes = [], [], [], set(), set()
    for index, element in enumerate(listing, 1):
        if element.tag != f'{{{P}}}sldId' or set(element.attrib) != {'id', f'{{{DOCREL}}}id'} or len(element) or (element.text or '').strip():
            _fail('unsupported_xml', 'unsupported')
        identity = element.get('id')
        if not re.fullmatch(r'[0-9]{1,10}', identity) or not 256 <= int(identity) <= 2147483647 or int(identity) in identities:
            _fail()
        identities.add(int(identity))
        relation = relations.get(main[0], {}).get(element.get(f'{{{DOCREL}}}id'))
        if relation is None or relation[0] != DOCREL + '/slide' or relation[1] in paths:
            _fail()
        path = relation[1]
        root = trees[path]
        hidden = not _bool(root.get('show', 'true'))
        note_relations = [target for kind, target in relations.get(path, {}).values() if kind == DOCREL + '/notesSlide']
        if len(note_relations) > 1 or (note_relations and note_relations[0] in used_notes):
            _fail()
        notes_path = note_relations[0] if note_relations else None
        if notes_path:
            used_notes.add(notes_path)
            backlinks = [target for kind, target in relations.get(notes_path, {}).values() if kind == DOCREL + '/slide']
            if backlinks and backlinks != [path]:
                _fail()
        paths.append(path)
        notes_paths.append(notes_path)
        catalog.append(dict(index=index, hidden=hidden, has_notes=bool(notes_path)))
    if set(paths) != {target for kind, target in relations.get(main[0], {}).values() if kind == DOCREL + '/slide'}:
        _fail()
    return tuple(catalog), paths, notes_paths


def _text_value(element):
    if len(element) or set(element.attrib) - {f'{{{XML}}}space'} or element.get(f'{{{XML}}}space', 'default') not in {'default', 'preserve'}:
        _fail('unsupported_xml', 'unsupported')
    value = element.text or ''
    if re.search(r'_x[0-9a-fA-F]{4}_', value):
        _fail('unsupported_xml', 'unsupported')
    return value


class _SelectionBudget:
    def __init__(self):
        self.characters = 0

    def add(self, count):
        self.characters += count
        if self.characters > MAX_SELECTED_CHARS:
            _fail('selection_limit', 'limit_reached')


def _text_body(body, counts, budget, collect):
    _nontext(body)
    if body.attrib or not len(body) or body[0].tag != f'{{{A}}}bodyPr':
        _fail('unsupported_xml', 'unsupported')
    _one(body, f'{{{A}}}bodyPr')
    _one(body, f'{{{A}}}lstStyle', required=False)
    paragraphs, seen_paragraph = [], False
    for child in body:
        if child.tag in {f'{{{A}}}bodyPr', f'{{{A}}}lstStyle'}:
            if seen_paragraph:
                _fail('unsupported_xml', 'unsupported')
            _formatting(child)
            continue
        if child.tag != f'{{{A}}}p' or child.attrib:
            _fail('unsupported_xml', 'unsupported')
        seen_paragraph = True
        _nontext(child)
        pieces = []
        for position, element in enumerate(child):
            if element.tag == f'{{{A}}}pPr':
                if position != 0:
                    _fail()
                _formatting(element)
            elif element.tag == f'{{{A}}}endParaRPr':
                if position != len(child) - 1:
                    _fail()
                _formatting(element)
            elif element.tag == f'{{{A}}}r':
                _nontext(element)
                if element.attrib or [node.tag for node in element] not in ([f'{{{A}}}t'], [f'{{{A}}}rPr', f'{{{A}}}t']):
                    _fail('unsupported_xml', 'unsupported')
                if len(element) == 2:
                    _formatting(element[0])
                value = _text_value(element[-1])
                if collect:
                    budget.add(len(value))
                    pieces.append(value)
            elif element.tag == f'{{{A}}}br':
                _nontext(element)
                if element.attrib or len(element) > 1 or (len(element) and element[0].tag != f'{{{A}}}rPr'):
                    _fail('unsupported_xml', 'unsupported')
                if len(element):
                    _formatting(element[0])
                if collect:
                    budget.add(1)
                    pieces.append('\n')
            elif element.tag == f'{{{A}}}fld':
                _nontext(element)
                if (set(element.attrib) - {'id', 'type'} or not element.get('id')
                        or any(node.tag not in {f'{{{A}}}rPr', f'{{{A}}}pPr', f'{{{A}}}t'} for node in element)
                        or any(len(element.findall(tag)) > 1 for tag in {f'{{{A}}}rPr', f'{{{A}}}pPr', f'{{{A}}}t'})):
                    _fail('unsupported_xml', 'unsupported')
                for node in element:
                    if node.tag == f'{{{A}}}t':
                        _text_value(node)
                    else:
                        _formatting(node)
                counts['field'] = counts.get('field', 0) + 1
            else:
                _fail('unsupported_xml', 'unsupported')
        if collect:
            if paragraphs:
                budget.add(1)
            paragraphs.append(''.join(pieces))
    if not seen_paragraph:
        _fail('unsupported_xml', 'unsupported')
    return '\n'.join(paragraphs) if collect else ''


def _slide(root, budget, collect):
    _nontext(root)
    notes = root.tag == f'{{{P}}}notes'
    allowed = {'showMasterSp', 'showMasterPhAnim'} | (set() if notes else {'show'})
    if set(root.attrib) - allowed:
        _fail('unsupported_xml', 'unsupported')
    for value in root.attrib.values():
        _bool(value)
    common = _one(root, f'{{{P}}}cSld')
    _nontext(common)
    if set(common.attrib) - {'name'}:
        _fail('unsupported_xml', 'unsupported')
    tree = _one(common, f'{{{P}}}spTree')
    counts, texts, identities = {}, [], set()
    for element in root.iter(f'{{{P}}}cNvPr'):
        identity = element.get('id')
        if identity is None or re.fullmatch(r'[0-9]{1,10}', identity) is None or not 1 <= int(identity) <= 4294967295 or int(identity) in identities:
            _fail()
        identities.add(int(identity))
    counts['extension'] = sum(1 for element in root.iter() if element.tag in {f'{{{P}}}extLst', f'{{{A}}}extLst'})
    counts['media'] = sum(1 for node in root.iter() if node.tag in {
        f'{{{A}}}audioFile', f'{{{A}}}videoFile', f'{{{A}}}wavAudioFile', f'{{{A}}}quickTimeFile',
        '{http://schemas.microsoft.com/office/powerpoint/2010/main}media'})
    for parent, harmless in ((root, {f'{{{P}}}cSld', f'{{{P}}}clrMapOvr', f'{{{P}}}extLst'}),
                             (common, {f'{{{P}}}spTree', f'{{{P}}}extLst'})):
        counts['other'] = counts.get('other', 0) + sum(1 for child in parent if child.tag not in harmless)

    def visit(group):
        _nontext(group)
        if group.attrib or len(group) < 2 or [node.tag for node in group[:2]] != [f'{{{P}}}nvGrpSpPr', f'{{{P}}}grpSpPr']:
            _fail('unsupported_xml', 'unsupported')
        _one(group, f'{{{P}}}nvGrpSpPr')
        _one(group, f'{{{P}}}grpSpPr')
        _formatting(group[0])
        _formatting(group[1])
        for shape in group[2:]:
            local = shape.tag.removeprefix('{' + P + '}')
            if (shape.tag.rsplit('}', 1)[-1] in {'sp', 'grpSp', 'pic', 'cxnSp', 'graphicFrame', 'txBody'}
                    and not shape.tag.startswith('{' + P + '}')):
                _fail('unsupported_xml', 'unsupported')
            if shape.tag == f'{{{P}}}grpSp':
                visit(shape)
            elif shape.tag == f'{{{P}}}sp':
                _nontext(shape)
                if set(shape.attrib) - {'useBgFill'}:
                    _fail('unsupported_xml', 'unsupported')
                if 'useBgFill' in shape.attrib:
                    _bool(shape.get('useBgFill'))
                _one(shape, f'{{{P}}}nvSpPr')
                _one(shape, f'{{{P}}}spPr')
                body = _one(shape, f'{{{P}}}txBody', required=False)
                if body is not None:
                    text = _text_body(body, counts, budget, collect)
                    if collect:
                        if texts:
                            budget.add(1)
                        texts.append(text)
                for node in shape:
                    if node.tag in {f'{{{P}}}nvSpPr', f'{{{P}}}spPr', f'{{{P}}}style'}:
                        _formatting(node)
                    if node.tag not in {f'{{{P}}}nvSpPr', f'{{{P}}}spPr', f'{{{P}}}txBody', f'{{{P}}}style', f'{{{P}}}extLst'}:
                        counts['other'] = counts.get('other', 0) + 1
            elif shape.tag == f'{{{P}}}graphicFrame':
                graphic = _one(shape, f'{{{A}}}graphic')
                data = _one(graphic, f'{{{A}}}graphicData')
                kind = {'http://schemas.openxmlformats.org/drawingml/2006/table': 'table',
                        'http://schemas.openxmlformats.org/drawingml/2006/chart': 'chart',
                        'http://schemas.openxmlformats.org/drawingml/2006/diagram': 'diagram'}.get(data.get('uri'), 'other')
                counts[kind] = counts.get(kind, 0) + 1
            elif shape.tag == f'{{{P}}}extLst':
                pass  # All extension containers were counted once above.
            else:
                kind = {'pic': 'picture', 'cxnSp': 'connector'}.get(local, 'other')
                counts[kind] = counts.get(kind, 0) + 1
    visit(tree)
    return '\n'.join(texts) if collect else '', {kind: count for kind, count in counts.items() if count}


def _read_package(data, request):
    trees, types, relations = _load_package(data)
    catalog, paths, notes_paths = _catalog(trees, types, relations)
    choices = request['slides']
    if choices is not None and any(index > len(catalog) for index in choices):
        _fail('invalid_slides', 'invalid_selection')
    selected = {paths[index - 1] for index in choices or ()}
    if request['include_notes']:
        selected |= {notes_paths[index - 1] for index in choices or () if notes_paths[index - 1]}
    budget, parsed = _SelectionBudget(), {}
    # Even metadata-only and unselected parts receive structure/text validation;
    # their text and object lists never reach the parent result.
    for name, kind in types.items():
        if kind in {SLIDE_TYPE, NOTES_TYPE}:
            text, counts = _slide(trees[name], budget, name in selected)
            if name in selected:
                parsed[name] = (text, counts)
    if choices is None:
        return PresentationReadResult('catalog', catalog=catalog, detail=_SUCCESS_DETAIL)
    returned, characters = [], 0
    for index in choices:
        text, counts = parsed[paths[index - 1]]
        notes, objects = None, [dict(source='slide', kind=kind, count=count) for kind, count in counts.items()]
        if request['include_notes'] and notes_paths[index - 1]:
            notes, counts = parsed[notes_paths[index - 1]]
            objects += [dict(source='notes', kind=kind, count=count) for kind, count in counts.items()]
        objects.sort(key=lambda item: (item['source'], item['kind']))
        characters += len(text) + len(notes or '') + sum(len(item['source']) + len(item['kind']) for item in objects)
        if characters > MAX_PAGE_CHARS:
            _fail('response_limit', 'limit_reached')
        returned.append(dict(index=index, text=text, notes=notes, unsupported_objects=objects))
    return PresentationReadResult('complete', catalog, tuple(returned), _SUCCESS_DETAIL)


def _parse_request(request):
    path, fd = Path(request['path']), -1
    try:
        data, fd, initial = _common._read_pinned(path, request['sha256'], request['size_bytes'])
        try:
            mime = (request['mime_type'] or '').split(';', 1)[0].strip().lower()
            if Path(request['name']).suffix.lower() != '.pptx' and mime != PREFIX + 'presentationml.presentation':
                _fail('unsupported_package', 'unsupported')
            result = _read_package(data, request)
        finally:
            _common._recheck_hash(path, fd, initial, request['sha256'])
        return result
    except _ReadFailure as exc:
        return PresentationReadResult(exc.status, detail=exc.detail)
    except MemoryError:
        return PresentationReadResult('limit_reached', detail='worker_limit')
    except Exception:
        return PresentationReadResult('error', detail='parser_error')
    finally:
        if fd >= 0:
            os.close(fd)


def _set_limits(timeout):
    _common._set_limits(timeout)


def _decode_result(value, request):
    from .presentation_models import PresentationInfo, PresentationSlide
    if type(value) is not dict or set(value) != {'status', 'catalog', 'slides', 'detail'}:
        raise ValueError
    if (type(value['status']) is not str or value['status'] not in _STATUS
            or type(value['detail']) is not str or value['detail'] not in _DETAILS
            or type(value['catalog']) is not list or not 0 <= len(value['catalog']) <= 128
            or type(value['slides']) is not list or len(value['slides']) > 5):
        raise ValueError
    catalog = tuple(PresentationInfo.model_validate(item).model_dump() for item in value['catalog'])
    if [item['index'] for item in catalog] != list(range(1, len(catalog) + 1)):
        raise ValueError
    returned = tuple(PresentationSlide.model_validate(item).model_dump() for item in value['slides'])
    characters = sum(len(item['text']) + len(item['notes'] or '') + sum(len(obj['source']) + len(obj['kind']) for obj in item['unsupported_objects']) for item in returned)
    if characters > MAX_PAGE_CHARS:
        raise ValueError
    status = value['status']
    if status not in {'catalog', 'complete'}:
        if catalog or returned:
            raise ValueError
    elif not catalog:
        raise ValueError
    elif status == 'catalog':
        if request['slides'] is not None or returned:
            raise ValueError
    else:
        if (request['slides'] is None or [item['index'] for item in returned] != list(request['slides'])
                or any(item['index'] > len(catalog) for item in returned)):
            raise ValueError
        for item in returned:
            has_notes = request['include_notes'] and catalog[item['index'] - 1]['has_notes']
            if (item['notes'] is not None) != has_notes or (not has_notes and any(obj['source'] == 'notes' for obj in item['unsupported_objects'])):
                raise ValueError
    return PresentationReadResult(status, catalog, returned, value['detail'])


def _worker_main(output_path, timeout):
    _set_limits(timeout)
    encoded = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(encoded) > MAX_REQUEST_BYTES:
        return
    request = json.loads(encoded)
    request['slides'] = None if request['slides'] is None else tuple(request['slides'])
    _validate_arguments(request['sha256'], request['size_bytes'], request['name'], request['mime_type'], request['slides'], request['include_notes'], timeout)
    result = _parse_request(request)
    with open(output_path, 'x', encoding='utf-8') as output:
        json.dump(asdict(result), output, ensure_ascii=False, separators=(',', ':'))


def read_pptx(path: Path, *, sha256: str, size_bytes: int, name: str | None,
              mime_type: str | None, slides: tuple[int, ...] | None,
              include_notes: bool = False, timeout: float = 15.0) -> PresentationReadResult:
    """Return selected shape text and optional notes, or only catalogue facts."""
    try:
        _validate_arguments(sha256, size_bytes, name, mime_type, slides, include_notes, timeout)
        if size_bytes > MAX_INPUT_BYTES:
            _fail('input_limit', 'limit_reached')
        path = Path(path)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            _fail('source_invalid')
        if metadata.st_size != size_bytes:
            _fail('artifact_changed')
        request = dict(path=str(path.absolute()), sha256=sha256, size_bytes=size_bytes,
                       name=name or path.name, mime_type=mime_type, slides=slides, include_notes=include_notes)
        encoded = json.dumps(request, ensure_ascii=False).encode('utf-8')
        if len(encoded) > MAX_REQUEST_BYTES:
            _fail('invalid_arguments', 'invalid_selection')
        timeout = min(float(timeout), 15.0)
        with tempfile.TemporaryDirectory(prefix='telegram-pptx-') as directory:
            output_path = Path(directory) / 'result.json'
            process = subprocess.Popen([sys.executable, '-I', '-B', str(Path(__file__).absolute()), '--worker', str(output_path), str(timeout)],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True, close_fds=True)
            try:
                process.communicate(input=encoded, timeout=timeout)
            except subprocess.TimeoutExpired:
                return PresentationReadResult('limit_reached', detail='worker_timeout')
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
                if process.stdin is not None:
                    process.stdin.close()
            if process.returncode != 0:
                return PresentationReadResult('limit_reached' if process.returncode < 0 else 'error', detail='worker_limit' if process.returncode < 0 else 'worker_error')
            with output_path.open('rb') as source:
                output = source.read(MAX_WORKER_OUTPUT_BYTES + 1)
            if len(output) > MAX_WORKER_OUTPUT_BYTES:
                _fail('worker_limit', 'limit_reached')
            result = _decode_result(json.loads(output), request)
            _, fd, initial = _common._read_pinned(path, sha256, size_bytes)
            try:
                if _common._fingerprint(initial) != _common._fingerprint(metadata):
                    _fail('artifact_changed')
            finally:
                os.close(fd)
            return result
    except _ReadFailure as exc:
        return PresentationReadResult(exc.status, detail=exc.detail)
    except (OSError, ValueError, TypeError):
        return PresentationReadResult('error', detail='worker_error')


if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == '--worker':
    _worker_main(sys.argv[2], float(sys.argv[3]))
