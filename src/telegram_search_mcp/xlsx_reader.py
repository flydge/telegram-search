"""Hash-pinned, bounded Transitional SpreadsheetML reading in an isolated worker.

Only stored/deflated OPC packages are supported. Strict OOXML, active content,
external relationships, phonetic strings, escaped OOXML text, and formula
attributes outside t/ref/si are rejected. Styles and cached dates are never
interpreted. All package XML and relationships are validated, even when unused.
Formula text is always raw lexical text, including external-reference and
OOXML escape-looking sequences. No formula syntax is evaluated or translated.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
import posixpath
import re
import signal
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
import xml.etree.ElementTree as ET
from xml.parsers import expat
import zipfile

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_EXPANSION_BYTES = 32 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_XML_NODES = 200_000
MAX_XML_DEPTH = 64
MAX_XML_TEXT = 32 * 1024 * 1024
MAX_ZIP_MEMBERS = 1024
MAX_COMPRESSION_RATIO = 200
MAX_SELECTED_CHARS = 1_000_000
MAX_PAGE_CHARS = 20_000
MAX_WORKER_MEMORY_BYTES = 768 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024

MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
DOCREL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKGREL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
XML = 'http://www.w3.org/XML/1998/namespace'
REL_TYPE = 'application/vnd.openxmlformats-package.relationships+xml'
BOOK_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'
SHEET_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'
STRINGS_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml'
_CELL = re.compile(r'([A-Z]{1,3})([1-9][0-9]{0,6})\Z')
_NUMERIC = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')
_STATUS = {'catalog', 'page', 'complete', 'unsupported', 'invalid_selection', 'limit_reached', 'error'}
_DETAILS = {'', 'invalid_arguments', 'invalid_ranges', 'invalid_sheets', 'offset_out_of_range',
            'artifact_changed', 'source_invalid', 'input_limit', 'expansion_limit', 'xml_limit',
            'selection_limit', 'cell_budget', 'unsupported_package', 'unsupported_xml',
            'unsupported_formula', 'invalid_package', 'parser_error', 'worker_timeout',
            'worker_limit', 'worker_error'}


@dataclass(frozen=True)
class SpreadsheetReadResult:
    status: str
    catalog: tuple[dict, ...] = ()
    cells: tuple[dict, ...] = ()
    cell_start: int = 0
    cell_end: int = 0
    total_cells: int = 0
    has_more: bool = False
    detail: str = ''


class _ReadFailure(Exception):
    def __init__(self, status: str, detail: str):
        self.status, self.detail = status, detail


def _fail(detail='invalid_package', status='error'):
    raise _ReadFailure(status, detail)


def _integer(value):
    return type(value) is int


def _coordinates(value):
    if type(value) is not str or (match := _CELL.fullmatch(value)) is None:
        _fail('invalid_ranges', 'invalid_selection')
    column = 0
    for character in match[1]:
        column = column * 26 + ord(character) - 64
    row = int(match[2])
    if column > 16384 or row > 1048576:
        _fail('invalid_ranges', 'invalid_selection')
    return row, column


def _bounds(value):
    if type(value) is not str or len(value) > 32 or value.count(':') > 1:
        _fail('invalid_ranges', 'invalid_selection')
    parts = value.split(':')
    row, column = _coordinates(parts[0])
    end_row, end_column = _coordinates(parts[-1])
    if end_row < row or end_column < column:
        _fail('invalid_ranges', 'invalid_selection')
    return row, column, end_row, end_column


def _address(row, column):
    letters = ''
    while column:
        column, digit = divmod(column - 1, 26)
        letters = chr(65 + digit) + letters
    return letters + str(row)


def _validate_arguments(sha256, size_bytes, name, mime_type, selections, offset, max_cells, timeout):
    if (not _integer(size_bytes) or size_bytes < 0 or type(sha256) is not str
            or re.fullmatch(r'[0-9a-f]{64}', sha256) is None
            or not _integer(offset) or not 0 <= offset <= 10_000
            or not _integer(max_cells) or not 1 <= max_cells <= 200
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
            or (name is not None and (type(name) is not str or len(name) > 1024))
            or (mime_type is not None and (type(mime_type) is not str or len(mime_type) > 256))):
        _fail('invalid_arguments', 'invalid_selection')
    total, seen = 0, set()
    if selections is not None:
        if type(selections) is not tuple or not 1 <= len(selections) <= 5:
            _fail('invalid_ranges', 'invalid_selection')
        for selection in selections:
            if type(selection) is not dict or set(selection) != {'sheet_index', 'range'} or not _integer(selection['sheet_index']) or not 1 <= selection['sheet_index'] <= 128:
                _fail('invalid_ranges', 'invalid_selection')
            bounds = _bounds(selection['range'])
            key = (selection['sheet_index'], selection['range'])
            if key in seen:
                _fail('invalid_ranges', 'invalid_selection')
            seen.add(key)
            total += (bounds[2] - bounds[0] + 1) * (bounds[3] - bounds[1] + 1)
            if total > 10_000:
                _fail('selection_limit', 'invalid_selection')
    if offset > total:
        _fail('offset_out_of_range', 'invalid_selection')
    return total


def _fingerprint(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _check_source(path, fd, initial):
    try:
        if _fingerprint(os.fstat(fd)) != _fingerprint(initial) or _fingerprint(path.lstat()) != _fingerprint(initial):
            _fail('artifact_changed')
    except OSError:
        _fail('artifact_changed')


def _read_pinned(path, sha256, size_bytes):
    fd = -1
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
            _fail('source_invalid')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        initial = os.fstat(fd)
        if _fingerprint(initial) != _fingerprint(before) or initial.st_size != size_bytes:
            _fail('artifact_changed')
        if size_bytes > MAX_INPUT_BYTES:
            _fail('input_limit', 'limit_reached')
        chunks, digest, length = [], hashlib.sha256(), 0
        while value := os.read(fd, min(1024 * 1024, size_bytes - length + 1)):
            length += len(value)
            if length > size_bytes:
                _fail('artifact_changed')
            digest.update(value)
            chunks.append(value)
        _check_source(path, fd, initial)
        if length != size_bytes or digest.hexdigest() != sha256:
            _fail('artifact_changed')
        return b''.join(chunks), fd, initial
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise


def _recheck_hash(path, fd, initial, sha256):
    _check_source(path, fd, initial)
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    length = 0
    while value := os.read(fd, 1024 * 1024):
        length += len(value)
        if length > initial.st_size:
            _fail('artifact_changed')
        digest.update(value)
    _check_source(path, fd, initial)
    if length != initial.st_size or digest.hexdigest() != sha256:
        _fail('artifact_changed')


class _XMLBudget:
    def __init__(self):
        self.nodes = self.text = 0

    def parse(self, data):
        # Parser callbacks operate on decoded names, so DTD/entity prohibition is
        # encoding-independent, including UTF-16 declarations and BOMs.
        parser = expat.ParserCreate(namespace_separator='}')
        stack = []
        root = None

        def expanded(name):
            return '{' + name if '}' in name else name

        def start(name, attrs):
            nonlocal root
            self.nodes += 1
            self.text += sum(len(name) + len(value) for name, value in attrs.items())
            if self.nodes > MAX_XML_NODES or len(stack) >= MAX_XML_DEPTH or self.text > MAX_XML_TEXT:
                _fail('xml_limit', 'limit_reached')
            element = ET.Element(expanded(name), {expanded(key): value for key, value in attrs.items()})
            if stack:
                stack[-1].append(element)
            else:
                root = element
            stack.append(element)

        def end(name):
            stack.pop()

        def text(value):
            self.text += len(value)
            if self.text > MAX_XML_TEXT:
                _fail('xml_limit', 'limit_reached')
            if stack:
                element = stack[-1]
                if len(element):
                    element[-1].tail = (element[-1].tail or '') + value
                else:
                    element.text = (element.text or '') + value

        def forbidden(*args):
            _fail('unsupported_xml', 'unsupported')

        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.CharacterDataHandler = text
        parser.StartDoctypeDeclHandler = forbidden
        parser.EntityDeclHandler = forbidden
        parser.ExternalEntityRefHandler = forbidden
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.Parse(data, True)
        if root is None:
            _fail()
        return root


def _part_name(value, *, absolute=False):
    if type(value) is not str or not value or len(value) > 1024 or any(ord(ch) < 32 for ch in value) or any(ch in value for ch in '\\%?#:'):
        _fail()
    if absolute:
        if not value.startswith('/'):
            _fail()
        value = value[1:]
    if value.startswith('/') or any(part in ('', '.', '..') for part in value.split('/')):
        _fail()
    return value


def _active(value):
    value = value.lower()
    return any(token in value for token in ('macroenabled', 'vbaproject', 'vba', 'activex', 'oleobject', 'externallink', '/embeddings/', 'attachedtemplate'))


def _content_types(root, members):
    if root.tag != f'{{{CT}}}Types' or root.attrib or (root.text or '').strip():
        _fail('unsupported_package', 'unsupported')
    defaults, overrides = {}, {}
    for element in root:
        if element.tag == f'{{{CT}}}Default' and set(element.attrib) == {'Extension', 'ContentType'}:
            extension = element.get('Extension').lower()
            if not re.fullmatch(r'[a-z0-9]+', extension) or extension in defaults:
                _fail()
            defaults[extension] = element.get('ContentType')
        elif element.tag == f'{{{CT}}}Override' and set(element.attrib) == {'PartName', 'ContentType'}:
            name = _part_name(element.get('PartName'), absolute=True)
            if name not in members or name.lower() in {key.lower() for key in overrides}:
                _fail()
            overrides[name] = element.get('ContentType')
        else:
            _fail('unsupported_package', 'unsupported')
        kind = element.get('ContentType', '')
        if len(element) or (element.text or '').strip() or (element.tail or '').strip() or len(kind) > 256 or re.fullmatch(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+', kind) is None or _active(kind):
            _fail('unsupported_package', 'unsupported')
    types = {}
    for name in members:
        if name == '[Content_Types].xml':
            continue
        kind = overrides.get(name, defaults.get(name.rsplit('.', 1)[-1].lower()))
        if not kind or len(kind) > 256 or _active(kind) or _active('/' + name):
            _fail('unsupported_package', 'unsupported')
        if name.endswith('.rels') and kind != REL_TYPE:
            _fail('unsupported_package', 'unsupported')
        types[name] = kind
    return types


def _relationship_source(name):
    if name == '_rels/.rels':
        return ''
    if '/_rels/' not in name or not name.endswith('.rels'):
        _fail()
    directory, filename = name.rsplit('/_rels/', 1)
    if '/' in filename or not filename[:-5]:
        _fail()
    return directory + '/' + filename[:-5]


def _target(source, value):
    if type(value) is not str or not value or len(value) > 1024 or any(ch in value for ch in '\\%?#:') or any(ord(ch) < 32 for ch in value):
        _fail()
    pieces = value.split('/')
    if any(part == '.' or (not part and index != 0) for index, part in enumerate(pieces)):
        _fail()
    name = value[1:] if value.startswith('/') else posixpath.join(posixpath.dirname(source), value)
    name = posixpath.normpath(name)
    return _part_name(name)


def _relationships(trees, members, types):
    relations = {}
    expected = {'officeDocument': BOOK_TYPE, 'worksheet': SHEET_TYPE, 'sharedStrings': STRINGS_TYPE}
    for name, root in trees.items():
        if not name.endswith('.rels'):
            continue
        source = _relationship_source(name)
        if source and source not in members:
            _fail()
        if root.tag != f'{{{PKGREL}}}Relationships' or root.attrib or (root.text or '').strip():
            _fail('unsupported_package', 'unsupported')
        items = {}
        for element in root:
            if element.tag != f'{{{PKGREL}}}Relationship' or not {'Id', 'Type', 'Target'} <= set(element.attrib) or set(element.attrib) - {'Id', 'Type', 'Target', 'TargetMode'} or len(element):
                _fail()
            identity, kind = element.get('Id'), element.get('Type')
            if not identity or len(identity) > 256 or identity in items or not kind or len(kind) > 256 or re.match(r'^(https?://|urn:)', kind) is None or any(ch.isspace() for ch in kind) or (element.text or '').strip() or (element.tail or '').strip():
                _fail()
            if element.get('TargetMode', 'Internal') != 'Internal' or _active(kind) or kind in {DOCREL + '/package', DOCREL + '/control'}:
                _fail('unsupported_package', 'unsupported')
            target = _target(source, element.get('Target'))
            if target not in members:
                _fail()
            if kind.startswith(DOCREL + '/') and kind.rsplit('/', 1)[-1] in expected:
                if types.get(target) != expected[kind.rsplit('/', 1)[-1]]:
                    _fail('unsupported_package', 'unsupported')
            items[identity] = (kind, target)
        relations[source] = items
    return relations


def _namespace(root, expected):
    if root.tag != f'{{{MAIN}}}{expected}' or any(not element.tag.startswith('{' + MAIN + '}') for element in root.iter()):
        _fail('unsupported_xml', 'unsupported')


def _string(root):
    if root.tag not in {f'{{{MAIN}}}si', f'{{{MAIN}}}is'} or root.attrib or (root.text or '').strip():
        _fail('unsupported_xml', 'unsupported')
    values = []
    for child in root:
        if child.tag == f'{{{MAIN}}}t':
            texts = [child]
        elif child.tag == f'{{{MAIN}}}r':
            if child.attrib or (child.text or '').strip() or len(child.findall(f'{{{MAIN}}}t')) != 1 or any(item.tag not in {f'{{{MAIN}}}t', f'{{{MAIN}}}rPr'} or (item.tail or '').strip() for item in child):
                _fail('unsupported_xml', 'unsupported')
            texts = child.findall(f'{{{MAIN}}}t')
        else:
            _fail('unsupported_xml', 'unsupported')
        if (child.tail or '').strip():
            _fail('unsupported_xml', 'unsupported')
        for item in texts:
            if len(item) or set(item.attrib) - {f'{{{XML}}}space'}:
                _fail('unsupported_xml', 'unsupported')
            value = item.text or ''
            if re.search(r'_x[0-9a-fA-F]{4}_', value):
                _fail('unsupported_xml', 'unsupported')
            values.append(value)
    if len(root.findall(f'{{{MAIN}}}t')) > 1 or (root.findall(f'{{{MAIN}}}t') and root.findall(f'{{{MAIN}}}r')):
        _fail()
    return ''.join(values)


def _shared_strings(root):
    if root is None:
        return ()
    _namespace(root, 'sst')
    if set(root.attrib) - {'count', 'uniqueCount'}:
        _fail('unsupported_xml', 'unsupported')
    for value in root.attrib.values():
        if not re.fullmatch(r'[0-9]{1,10}', value):
            _fail()
    return tuple(_string(child) for child in root)


def _catalog(book, book_relations):
    _namespace(book, 'workbook')
    containers = book.findall(f'{{{MAIN}}}sheets')
    if len(containers) != 1 or not len(containers[0]):
        _fail()
    if len(containers[0]) > 128:
        _fail('selection_limit', 'limit_reached')
    catalog, paths, names, ids = [], [], set(), set()
    for index, sheet in enumerate(containers[0], 1):
        if sheet.tag != f'{{{MAIN}}}sheet' or set(sheet.attrib) - {'name', 'sheetId', 'state', f'{{{DOCREL}}}id'} or len(sheet):
            _fail('unsupported_xml', 'unsupported')
        name, identity, state, relation = sheet.get('name'), sheet.get('sheetId'), sheet.get('state', 'visible'), sheet.get(f'{{{DOCREL}}}id')
        if not name or len(name) > 31 or name.casefold() in names or any(ch in name for ch in '[]:*?/\\') or not identity or not re.fullmatch(r'[1-9][0-9]{0,9}', identity) or identity in ids or state not in {'visible', 'hidden', 'veryHidden'}:
            _fail()
        if relation not in book_relations or book_relations[relation][0] != DOCREL + '/worksheet':
            _fail('unsupported_package', 'unsupported')
        target = book_relations[relation][1]
        if target in paths:
            _fail()
        names.add(name.casefold())
        ids.add(identity)
        paths.append(target)
        catalog.append(dict(index=index, name=name, state=state))
    return tuple(catalog), paths


def _lexical_cell(cell, row, shared):
    try:
        cell_row, column = _coordinates(cell.get('r'))
    except _ReadFailure:
        _fail()
    if cell_row != row or set(cell.attrib) - {'r', 's', 't', 'cm', 'vm', 'ph'}:
        _fail()
    kind = cell.get('t', 'n')
    if kind not in {'n', 's', 'b', 'e', 'str', 'inlineStr', 'd'}:
        _fail('unsupported_xml', 'unsupported')
    children = {}
    for element in cell:
        key = element.tag.removeprefix('{' + MAIN + '}')
        if key not in {'f', 'v', 'is'} or key in children:
            _fail('unsupported_xml', 'unsupported')
        children[key] = element
    value_node = children.get('v')
    value = None if value_node is None else value_node.text or ''
    if value_node is not None and (value_node.attrib or len(value_node)):
        _fail()
    if kind == 'inlineStr':
        if 'is' not in children or value_node is not None or 'f' in children:
            _fail()
        value = _string(children['is'])
    elif 'is' in children:
        _fail()
    elif kind == 's':
        if value is None or not re.fullmatch(r'[0-9]{1,10}', value) or int(value) >= len(shared):
            _fail()
        value = shared[int(value)]
    elif kind == 'n':
        if value == '':
            value = None
        if value is not None and _NUMERIC.fullmatch(value) is None:
            _fail()
    elif kind == 'b':
        if value not in {'0', '1'}:
            _fail()
    elif value is None:
        _fail()
    formula = formula_kind = formula_ref = formula_index = None
    node = children.get('f')
    if node is not None:
        if set(node.attrib) - {'t', 'ref', 'si'} or len(node):
            _fail('unsupported_formula', 'unsupported')
        formula_kind, formula_ref, formula_index = node.get('t', 'normal'), node.get('ref'), node.get('si')
        if formula_kind not in {'normal', 'shared', 'array', 'dataTable'}:
            _fail('unsupported_formula', 'unsupported')
        if formula_ref is not None:
            try:
                _bounds(formula_ref)
            except _ReadFailure:
                _fail('unsupported_formula', 'unsupported')
        if formula_kind == 'shared':
            if formula_index is None or not re.fullmatch(r'[0-9]{1,10}', formula_index) or int(formula_index) > 2147483647:
                _fail('unsupported_formula', 'unsupported')
            formula_index = int(formula_index)
        elif formula_index is not None or (formula_kind == 'normal' and formula_ref is not None):
            _fail('unsupported_formula', 'unsupported')
        if formula_kind in {'array', 'dataTable'} and formula_ref is None:
            _fail('unsupported_formula', 'unsupported')
        formula = node.text or ''
    return dict(address=cell.get('r'), row=row, column=column, value_type=kind, value=value,
                formula=formula, formula_kind=formula_kind, formula_ref=formula_ref, formula_shared_index=formula_index)


def _worksheet(root, selected, shared):
    _namespace(root, 'worksheet')
    data = root.findall(f'{{{MAIN}}}sheetData')
    if len(data) != 1:
        _fail()
    cells, rows, addresses = {}, set(), set()
    for row in data[0]:
        if row.tag != f'{{{MAIN}}}row' or not row.get('r') or not re.fullmatch(r'[1-9][0-9]{0,6}', row.get('r')):
            _fail()
        number = int(row.get('r'))
        if number > 1048576 or number in rows:
            _fail()
        rows.add(number)
        for cell in row:
            if cell.tag != f'{{{MAIN}}}c':
                _fail('unsupported_xml', 'unsupported')
            value = _lexical_cell(cell, number, shared)
            key = (value['row'], value['column'])
            if key in addresses:
                _fail()
            addresses.add(key)
            if key in selected:
                cells[key] = value
    return cells


def _read_package(data, request):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        metadata = archive.infolist()
        if len(metadata) > MAX_ZIP_MEMBERS or sum(item.file_size for item in metadata) > MAX_EXPANSION_BYTES:
            _fail('expansion_limit', 'limit_reached')
        members, aliases = {}, set()
        for item in metadata:
            name = item.orig_filename
            if item.is_dir():
                _part_name(name[:-1])
                if item.file_size:
                    _fail()
            else:
                _part_name(name)
            if name.lower().endswith('.rels') and not name.endswith('.rels'):
                _fail('unsupported_package', 'unsupported')
            if name.lower() in aliases or item.filename != name or stat.S_ISLNK(item.external_attr >> 16) or (stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)):
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
        trees, budget, expansion = {}, _XMLBudget(), 0
        # Content types are read first, then every declared XML/relationship.
        names = ['[Content_Types].xml'] + [name for name in members if name != '[Content_Types].xml']
        types = {}
        for name in names:
            item = members[name]
            is_xml = name.endswith(('.xml', '.rels')) or types.get(name, '').endswith('+xml') or types.get(name) in {'application/xml', 'text/xml'}
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
                types = _content_types(trees[name], members)
        relations = _relationships(trees, members, types)
        main = [target for kind, target in relations.get('', {}).values() if kind == DOCREL + '/officeDocument']
        if len(main) != 1 or [name for name, kind in types.items() if kind == BOOK_TYPE] != main:
            _fail('unsupported_package', 'unsupported')
        book_path = main[0]
        book_relations = relations.get(book_path, {})
        catalog, sheet_paths = _catalog(trees[book_path], book_relations)
        string_paths = [target for kind, target in book_relations.values() if kind == DOCREL + '/sharedStrings']
        if len(string_paths) > 1 or set(string_paths) != {name for name, kind in types.items() if kind == STRINGS_TYPE}:
            _fail()
        shared = _shared_strings(trees[string_paths[0]] if string_paths else None)
        choices = request['selections']
        if choices is not None and any(item['sheet_index'] > len(catalog) for item in choices):
            _fail('invalid_sheets', 'invalid_selection')
        selected = {path: set() for path in sheet_paths}
        positions = []
        for index, choice in enumerate(choices or (), 1):
            row, column, end_row, end_column = _bounds(choice['range'])
            path = sheet_paths[choice['sheet_index'] - 1]
            for r in range(row, end_row + 1):
                for c in range(column, end_column + 1):
                    selected[path].add((r, c))
                    positions.append((index, path, r, c))
        # Validate every worksheet's cell references and bodies, with no
        # dimension-based coverage and no filtering of hidden rows or columns.
        parsed = {}
        for name, kind in types.items():
            if kind == SHEET_TYPE:
                parsed[name] = _worksheet(trees[name], selected.get(name, set()), shared)
            elif kind == BOOK_TYPE:
                _namespace(trees[name], 'workbook')
        if choices is None:
            return SpreadsheetReadResult('catalog', catalog=catalog)
        cells, relevant, page_chars = [], 0, 0
        start = request['offset']
        end = start
        page_closed = False
        for offset, (index, path, row, column) in enumerate(positions):
            cell = parsed[path].get((row, column))
            if cell is None:
                cell = dict(address=_address(row, column), row=row, column=column, value_type='blank', value=None,
                            formula=None, formula_kind=None, formula_ref=None, formula_shared_index=None)
            cell = dict(selection_index=index, **cell)
            length = sum(len(value) for value in cell.values() if isinstance(value, str))
            relevant += length
            if relevant > MAX_SELECTED_CHARS:
                _fail('selection_limit', 'limit_reached')
            if offset < start or page_closed:
                continue
            if len(cells) >= request['max_cells'] or page_chars + length > MAX_PAGE_CHARS:
                page_closed = True
                continue
            cells.append(cell)
            page_chars += length
            end += 1
        if start < len(positions) and not cells:
            _fail('cell_budget', 'limit_reached')
        more = end < len(positions)
        return SpreadsheetReadResult('page' if more else 'complete', catalog, tuple(cells), start, end, len(positions), more)


def _parse_request(request):
    path, fd = Path(request['path']), -1
    try:
        data, fd, initial = _read_pinned(path, request['sha256'], request['size_bytes'])
        try:
            mime = (request['mime_type'] or '').split(';', 1)[0].strip().lower()
            if Path(request['name']).suffix.lower() != '.xlsx' and mime != 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':
                _fail('unsupported_package', 'unsupported')
            result = _read_package(data, request)
        finally:
            _recheck_hash(path, fd, initial, request['sha256'])
        return result
    except _ReadFailure as exc:
        return SpreadsheetReadResult(exc.status, detail=exc.detail)
    except MemoryError:
        return SpreadsheetReadResult('limit_reached', detail='worker_limit')
    except Exception:
        return SpreadsheetReadResult('error', detail='parser_error')
    finally:
        if fd >= 0:
            os.close(fd)


def _darwin_virtual_size():
    import ctypes
    class TaskInfo(ctypes.Structure):
        _fields_ = [('values', ctypes.c_uint64 * 6), ('counters', ctypes.c_int32 * 12)]
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    function = library.proc_pidinfo
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    info = TaskInfo()
    if function(os.getpid(), 4, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        raise OSError('worker memory measurement unavailable')
    virtual_size, resident_size = info.values[0], info.values[1]
    if virtual_size < resident_size or virtual_size <= 0:
        raise OSError('worker memory measurement unavailable')
    return virtual_size


def _set_limits(timeout):
    import resource
    # Match F8's measured inherited Darwin VM + 768 MiB allocation budget;
    # this is deliberately not described as an absolute resident-memory cap.
    memory = MAX_WORKER_MEMORY_BYTES + (_darwin_virtual_size() if sys.platform == 'darwin' else 0)
    for kind in (resource.RLIMIT_AS, resource.RLIMIT_DATA):
        resource.setrlimit(kind, (memory, memory))
    cpu = max(1, math.ceil(timeout))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_WORKER_OUTPUT_BYTES, MAX_WORKER_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def _decode_result(value, request):
    from .spreadsheet_models import SheetInfo, SpreadsheetCell, selected_position
    if type(value) is not dict or set(value) != {'status', 'catalog', 'cells', 'cell_start', 'cell_end', 'total_cells', 'has_more', 'detail'}:
        raise ValueError
    if (value['status'] not in _STATUS or value['detail'] not in _DETAILS
            or type(value['catalog']) is not list or len(value['catalog']) > 128
            or (not value['catalog'] and value['status'] in {'catalog', 'page', 'complete'})
            or type(value['cells']) is not list or len(value['cells']) > request['max_cells']
            or type(value['has_more']) is not bool):
        raise ValueError
    if any(not _integer(value[field]) or not 0 <= value[field] <= 10_000 for field in ('cell_start', 'cell_end', 'total_cells')):
        raise ValueError
    catalog = tuple(SheetInfo.model_validate(item).model_dump() for item in value['catalog'])
    if [item['index'] for item in catalog] != list(range(1, len(catalog) + 1)) or len({item['name'].casefold() for item in catalog}) != len(catalog):
        raise ValueError
    cells = tuple(SpreadsheetCell.model_validate(item).model_dump() for item in value['cells'])
    if sum(len(field) for cell in cells for field in cell.values() if isinstance(field, str)) > MAX_PAGE_CHARS:
        raise ValueError
    status = value['status']
    if status not in {'catalog', 'page', 'complete'}:
        if catalog or cells or value['cell_start'] or value['cell_end'] or value['total_cells'] or value['has_more']:
            raise ValueError
    elif status == 'catalog':
        if request['selections'] is not None or cells or value['cell_start'] or value['cell_end'] or value['total_cells'] or value['has_more']:
            raise ValueError
    else:
        total = _validate_arguments(request['sha256'], request['size_bytes'], request['name'], request['mime_type'], request['selections'], request['offset'], request['max_cells'], 15.0)
        if request['selections'] is None or value['total_cells'] != total or value['cell_start'] != request['offset'] or value['cell_end'] - value['cell_start'] != len(cells) or value['cell_end'] > total or value['has_more'] != (value['cell_end'] < total) or (status == 'page') != value['has_more'] or (value['has_more'] and not cells) or any(item['sheet_index'] > len(catalog) for item in request['selections']):
            raise ValueError
        for offset, cell in enumerate(cells, value['cell_start']):
            if (cell['selection_index'], cell['address'], cell['row'], cell['column']) != selected_position(request['selections'], offset):
                raise ValueError
    return SpreadsheetReadResult(status, catalog, cells, value['cell_start'], value['cell_end'], value['total_cells'], value['has_more'], value['detail'])


def _worker_main(output_path, timeout):
    _set_limits(timeout)
    encoded = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(encoded) > MAX_REQUEST_BYTES:
        return
    request = json.loads(encoded)
    request['selections'] = None if request['selections'] is None else tuple(request['selections'])
    _validate_arguments(request['sha256'], request['size_bytes'], request['name'], request['mime_type'], request['selections'], request['offset'], request['max_cells'], timeout)
    result = _parse_request(request)
    with open(output_path, 'x', encoding='utf-8') as output:
        json.dump(asdict(result), output, ensure_ascii=False, separators=(',', ':'))


def read_xlsx(path: Path, *, sha256: str, size_bytes: int, name: str | None,
              mime_type: str | None, selections: tuple[dict, ...] | None,
              offset: int = 0, max_cells: int = 200, timeout: float = 15.0) -> SpreadsheetReadResult:
    """Read one selected cell page, or only the sheet catalog, from pinned bytes."""
    try:
        _validate_arguments(sha256, size_bytes, name, mime_type, selections, offset, max_cells, timeout)
        if size_bytes > MAX_INPUT_BYTES:
            _fail('input_limit', 'limit_reached')
        path = Path(path)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            _fail('source_invalid')
        if metadata.st_size != size_bytes:
            _fail('artifact_changed')
        request = dict(path=str(path.absolute()), sha256=sha256, size_bytes=size_bytes,
                       name=name or path.name, mime_type=mime_type, selections=selections,
                       offset=offset, max_cells=max_cells)
        encoded = json.dumps(request, ensure_ascii=False).encode('utf-8')
        if len(encoded) > MAX_REQUEST_BYTES:
            _fail('invalid_arguments', 'invalid_selection')
        timeout = min(float(timeout), 15.0)
        with tempfile.TemporaryDirectory(prefix='telegram-xlsx-') as directory:
            output_path = Path(directory) / 'result.json'
            process = subprocess.Popen([sys.executable, '-I', '-B', str(Path(__file__).absolute()), '--worker', str(output_path), str(timeout)],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True, close_fds=True)
            try:
                process.communicate(input=encoded, timeout=timeout)
            except subprocess.TimeoutExpired:
                return SpreadsheetReadResult('limit_reached', detail='worker_timeout')
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
                return SpreadsheetReadResult('limit_reached' if process.returncode < 0 else 'error', detail='worker_limit' if process.returncode < 0 else 'worker_error')
            with output_path.open('rb') as source:
                output = source.read(MAX_WORKER_OUTPUT_BYTES + 1)
            if len(output) > MAX_WORKER_OUTPUT_BYTES:
                _fail('worker_limit', 'limit_reached')
            result = _decode_result(json.loads(output), request)
            # Worker checked pinned bytes both before/after; verify the current
            # authorized path again before exposing its returned content.
            _, fd, initial = _read_pinned(path, sha256, size_bytes)
            try:
                if _fingerprint(initial) != _fingerprint(metadata):
                    _fail('artifact_changed')
            finally:
                os.close(fd)
            return result
    except _ReadFailure as exc:
        return SpreadsheetReadResult(exc.status, detail=exc.detail)
    except (OSError, ValueError, TypeError):
        return SpreadsheetReadResult('error', detail='worker_error')


if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == '--worker':
    _worker_main(sys.argv[2], float(sys.argv[3]))
