"""Whole-package DOCX safety and a fixed, bounded legacy prefix worker.

Only stdlib imports occur here. XML auditing retains package metadata, never a
second document tree. Guard and library parsing consume the same immutable bytes.
"""
from __future__ import annotations

import importlib.util
import io
import json
import math
import os
import posixpath
import re
import signal
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.parsers import expat

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 500
MAX_EXPANSION_BYTES = 32 * 1024 * 1024
MAX_RATIO = 200
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_XML_NODES = 200000
MAX_XML_DEPTH = 64
MAX_XML_CHARS = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
OFFICE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
WORD = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
REL_MIME = 'application/vnd.openxmlformats-package.relationships+xml'
MAIN_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
# Full Office package declarations identify embedded packages, unlike passive
# main+xml/part types. This finite denyset is not a DOCX family allowlist.
_OFFICE_PACKAGE_MIMES = {
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.template',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.template',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'application/vnd.openxmlformats-officedocument.presentationml.slideshow',
    'application/vnd.openxmlformats-officedocument.presentationml.template',
}
_ERRORS = {'invalid_arguments', 'source_invalid', 'artifact_changed', 'input_limit',
           'docx_expansion_limit', 'parser_error', 'worker_timeout', 'worker_limit', 'worker_error'}


class DocxSafetyError(ValueError):
    def __init__(self, *, budget: bool = False):
        super().__init__('docx_expansion_limit' if budget else 'parser_error')
        self.budget = budget


def _deny(*, budget=False):
    raise DocxSafetyError(budget=budget)


def _controls(value):
    return any(unicodedata.category(character).startswith('C') for character in value)


def _name(value, *, absolute=False):
    if (type(value) is not str or not value or len(value) > 1024 or _controls(value)
            or any(character in value for character in '\\%?#:')):
        _deny()
    if absolute:
        if not value.startswith('/'):
            _deny()
        value = value[1:]
    if value.startswith('/') or any(part in {'', '.', '..'} for part in value.split('/')):
        _deny()
    return value


def _active_mime(value):
    lower = value.lower()
    return lower in _OFFICE_PACKAGE_MIMES or any(token in lower for token in ('macroenabled', 'vbaproject', 'vbadata', 'activex', 'oleobject',
                                           'attachedtemplate', 'controlproperties', 'embeddedpackage'))


def _active_relationship(value):
    return value.rsplit('/', 1)[-1].lower() in {
        'vbaproject', 'activex', 'oleobject', 'package', 'attachedtemplate',
        'control', 'activexcontrol', 'activexcontrolbinary', 'controlproperties', 'ctrlprop', 'vbadata',
    }


def _relationship_source(name):
    if name == '_rels/.rels':
        return ''
    if name.startswith('_rels/'):
        directory, filename = '', name[len('_rels/'):]
    elif '/_rels/' in name:
        directory, filename = name.rsplit('/_rels/', 1)
    else:
        _deny()
    if not filename.endswith('.rels') or '/' in filename or not filename[:-5]:
        _deny()
    return _name(posixpath.join(directory, filename[:-5]))


def _target(source, value):
    if (type(value) is not str or not value or len(value) > 1024 or _controls(value)
            or value.startswith('//') or any(character in value for character in '\\%?#:')):
        _deny()
    parts = [] if value.startswith('/') else posixpath.dirname(source).split('/') if '/' in source else []
    for part in value.lstrip('/').split('/'):
        if part in {'', '.'}:
            _deny()
        if part == '..':
            if not parts:
                _deny()
            parts.pop()
        else:
            parts.append(part)
    return _name('/'.join(parts))


class _XMLBudget:
    def __init__(self):
        self.nodes = self.characters = 0

    def parse(self, data, *, content_types=False, relationships=False):
        parser = expat.ParserCreate(namespace_separator='}')
        depth = 0
        root = None
        records = []
        def start(tag, attrs):
            nonlocal depth, root
            depth += 1
            self.nodes += 1
            self.characters += sum(len(key) + len(value) for key, value in attrs.items())
            if depth > MAX_XML_DEPTH or self.nodes > MAX_XML_NODES or self.characters > MAX_XML_CHARS:
                _deny(budget=True)
            if root is None:
                root = tag
            forbidden = ([] if content_types else [CT]) + ([] if relationships else [REL])
            if any(tag.startswith(namespace + '}') or any(key.startswith(namespace + '}') for key in attrs)
                   for namespace in forbidden):
                _deny()
            if content_types:
                if depth == 1:
                    if tag != CT + '}Types' or attrs:
                        _deny()
                elif depth == 2:
                    if tag not in {CT + '}Default', CT + '}Override'}:
                        _deny()
                    records.append((tag.rsplit('}', 1)[-1], dict(attrs)))
                else:
                    _deny()
            elif relationships:
                if depth == 1:
                    if tag != REL + '}Relationships' or attrs:
                        _deny()
                elif depth == 2:
                    if tag != REL + '}Relationship':
                        _deny()
                    records.append(dict(attrs))
                else:
                    _deny()
        def end(tag):
            nonlocal depth
            depth -= 1
        def text(value):
            self.characters += len(value)
            if self.characters > MAX_XML_CHARS:
                _deny(budget=True)
            if (content_types or relationships) and value.strip():
                _deny()
        def forbidden(*args):
            _deny()
        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.CharacterDataHandler = text
        parser.StartDoctypeDeclHandler = forbidden
        parser.EntityDeclHandler = forbidden
        parser.ExternalEntityRefHandler = forbidden
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.Parse(data, True)
        if root is None:
            _deny()
        return root, records


def _types(records, members):
    defaults, overrides, aliases = {}, {}, set()
    for tag, attrs in records:
        if tag == 'Default' and set(attrs) == {'Extension', 'ContentType'}:
            extension = attrs['Extension'].lower()
            if not re.fullmatch(r'[a-z0-9]{1,128}', extension) or extension in defaults:
                _deny()
            defaults[extension] = attrs['ContentType']
        elif tag == 'Override' and set(attrs) == {'PartName', 'ContentType'}:
            name = _name(attrs['PartName'], absolute=True)
            if name not in members or name.casefold() in aliases:
                _deny()
            aliases.add(name.casefold())
            overrides[name] = attrs['ContentType']
        else:
            _deny()
        mime = attrs['ContentType']
        if len(mime) > 256 or re.fullmatch(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+', mime) is None or _active_mime(mime):
            _deny()
    types = {}
    for name in members:
        if name == '[Content_Types].xml':
            continue
        mime = overrides.get(name, defaults.get(name.rsplit('.', 1)[-1].lower()))
        if not mime or _active_mime(mime) or (mime == REL_MIME) != name.endswith('.rels'):
            _deny()
        types[name] = mime
    return types


def validate_docx(data: bytes) -> None:
    """Audit all parts, including unreachable ones, before library materialization."""
    if len(data) > MAX_INPUT_BYTES:
        _deny(budget=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_MEMBERS or sum(item.file_size for item in entries) > MAX_EXPANSION_BYTES:
            _deny(budget=True)
        members, aliases = {}, set()
        for item in entries:
            name = item.orig_filename
            _name(name[:-1] if item.is_dir() else name)
            mode = stat.S_IFMT(item.external_attr >> 16)
            if (name != item.filename or name.casefold() in aliases or mode not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or (mode == stat.S_IFDIR and not item.is_dir()) or (item.is_dir() and item.file_size)):
                _deny()
            aliases.add(name.casefold())
            if item.flag_bits & 1 or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                _deny()
            if item.file_size > max(1, item.compress_size) * MAX_RATIO:
                _deny(budget=True)
            if name.lower().endswith('.rels') and not name.endswith('.rels'):
                _deny()
            if not item.is_dir():
                members[name] = item
        if '[Content_Types].xml' not in members or '_rels/.rels' not in members:
            _deny()
        types, roots, relations = {}, {}, {}
        budget, expansion = _XMLBudget(), 0
        names = ['[Content_Types].xml'] + [name for name in members if name != '[Content_Types].xml']
        for name in names:
            item = members[name]
            mime = types.get(name, '').lower()
            is_xml = name.lower().endswith(('.xml', '.rels')) or mime.endswith('+xml') or mime in {'application/xml', 'text/xml'}
            maximum = MAX_XML_BYTES if is_xml else MAX_EXPANSION_BYTES
            if item.file_size > maximum:
                _deny(budget=True)
            size, chunks = 0, []
            with archive.open(item) as source:
                while value := source.read(min(65536, maximum - size + 1)):
                    size += len(value)
                    expansion += len(value)
                    if size > maximum or expansion > MAX_EXPANSION_BYTES or size > max(1, item.compress_size) * MAX_RATIO:
                        _deny(budget=True)
                    if is_xml:
                        chunks.append(value)
            if size != item.file_size:
                _deny()
            if is_xml:
                root, records = budget.parse(b''.join(chunks), content_types=name == '[Content_Types].xml',
                                             relationships=name.endswith('.rels'))
                roots[name] = root
                if name == '[Content_Types].xml':
                    types = _types(records, members)
                elif name.endswith('.rels'):
                    source = _relationship_source(name)
                    if source and source not in members:
                        _deny()
                    items = {}
                    for attrs in records:
                        if not {'Id', 'Type', 'Target'} <= set(attrs) or set(attrs) - {'Id', 'Type', 'Target', 'TargetMode'}:
                            _deny()
                        ident, kind, target = attrs['Id'], attrs['Type'], attrs['Target']
                        mode = attrs.get('TargetMode', 'Internal')
                        if (not ident or len(ident) > 256 or _controls(ident) or any(ch.isspace() for ch in ident)
                                or ident in items or not kind or len(kind) > 256 or _controls(kind)
                                or any(ch.isspace() for ch in kind) or re.match(r'^(https?://|urn:)', kind) is None
                                or _active_relationship(kind) or mode not in {'Internal', 'External'}):
                            _deny()
                        if mode == 'External':
                            if kind != OFFICE + 'hyperlink' or not target or len(target) > 1024 or _controls(target):
                                _deny()
                            # Inert metadata: neither resolve, fetch nor retain the target.
                            items[ident] = (kind, None)
                        else:
                            target = _target(source, target)
                            if target not in members:
                                _deny()
                            items[ident] = (kind, target)
                    relations[source] = items
        main = [target for kind, target in relations.get('', {}).values() if kind == OFFICE + 'officeDocument']
        if (len(main) != 1 or types.get(main[0]) != MAIN_MIME or roots.get(main[0]) != WORD + '}document'
                or any(kind == OFFICE + 'officeDocument' for source, items in relations.items() if source
                       for kind, target in items.values())):
            _deny()
        # Iterative graph audit is bounded by package members; cycles are inert.
        pending, visited = [''], set()
        while pending:
            source = pending.pop()
            if source in visited:
                continue
            visited.add(source)
            if len(visited) > MAX_MEMBERS + 1:
                _deny(budget=True)
            pending.extend(target for kind, target in relations.get(source, {}).values() if target is not None and target not in visited)


@dataclass(frozen=True)
class PrefixResult:
    status: str
    text: str = ''
    processed_bytes: int = 0
    total_bytes: int | None = None
    error: str | None = None


def _fingerprint(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _load_page_bounds():
    identity = '_telegram_docx_page_bounds'
    specification = importlib.util.spec_from_file_location(identity, Path(__file__).with_name('document_page_reader.py'))
    if specification is None or specification.loader is None:
        raise ImportError('bounded helpers unavailable')
    module = importlib.util.module_from_spec(specification)
    sys.modules[identity] = module
    specification.loader.exec_module(module)
    return module


def _snapshot(path, expected):
    fd = -1
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
            raise ValueError('source_invalid')
        if _fingerprint(before) != expected:
            raise ValueError('artifact_changed')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        initial = os.fstat(fd)
        if _fingerprint(initial) != expected:
            raise ValueError('artifact_changed')
        chunks, length = [], 0
        while value := os.read(fd, min(1024 * 1024, MAX_INPUT_BYTES - length + 1)):
            length += len(value)
            if length > MAX_INPUT_BYTES or length > initial.st_size:
                raise ValueError('artifact_changed')
            chunks.append(value)
        if length != initial.st_size or _fingerprint(os.fstat(fd)) != expected or _fingerprint(path.lstat()) != expected:
            raise ValueError('artifact_changed')
        return b''.join(chunks), fd
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise


def _prefix(data, max_chars):
    validate_docx(data)
    from docx import Document
    from docx.table import Table
    document = Document(io.BytesIO(data))
    segments, length, partial = [], 0, False
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            values = [cell.text for row in block.rows for cell in row.cells]
        else:
            values = [block.text]
        for value in values:
            if not value:
                continue
            separator = '\n' if segments else ''
            segment = separator + value
            remaining = max_chars - length
            if len(segment) > remaining:
                segments.append(segment[:remaining])
                partial = True
                break
            segments.append(segment)
            length += len(segment)
        if partial:
            break
    return PrefixResult('partial' if partial else 'complete', ''.join(segments), len(data), len(data))


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('invalid_arguments')
        value[key] = item
    return value


def _request(value):
    fields = {'version', 'mode', 'path', 'fingerprint', 'size_bytes', 'max_chars'}
    if (type(value) is not dict or set(value) != fields or type(value['version']) is not int or value['version'] != 1
            or value['mode'] != 'docx_prefix' or type(value['path']) is not str or len(value['path']) > 4096
            or not Path(value['path']).is_absolute() or type(value['fingerprint']) is not list
            or len(value['fingerprint']) != 7 or any(type(item) is not int for item in value['fingerprint'])
            or any(item < 0 for item in value['fingerprint'][:5])
            or type(value['size_bytes']) is not int or not 0 <= value['size_bytes'] <= MAX_INPUT_BYTES
            or value['fingerprint'][4] != value['size_bytes'] or type(value['max_chars']) is not int
            or not 1 <= value['max_chars'] <= 100000):
        raise ValueError('invalid_arguments')
    return value


def _worker_main(output_path, timeout):
    fd = -1
    total = None
    try:
        if not math.isfinite(timeout) or not 0 < timeout <= 15:
            raise ValueError('invalid_arguments')
        bounds = _load_page_bounds()
        bounds._set_limits(timeout)
        import resource
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('invalid_arguments')
        request = _request(json.loads(raw, object_pairs_hook=_json_object))
        total = request['size_bytes']
        path, expected = Path(request['path']), tuple(request['fingerprint'])
        data, fd = _snapshot(path, expected)
        try:
            result = _prefix(data, request['max_chars'])
        finally:
            if _fingerprint(os.fstat(fd)) != expected or _fingerprint(path.lstat()) != expected:
                raise ValueError('artifact_changed')
    except MemoryError:
        result = PrefixResult('error', total_bytes=total, error='worker_limit')
    except DocxSafetyError as error:
        result = PrefixResult('error', total_bytes=total, error='docx_expansion_limit' if error.budget else 'parser_error')
    except Exception as error:
        code = str(error) if type(error) is ValueError and str(error) in _ERRORS else 'parser_error'
        result = PrefixResult('error', total_bytes=total, error=code)
    finally:
        if fd >= 0:
            os.close(fd)
    value = {'status': result.status, 'text': result.text, 'processed_bytes': result.processed_bytes,
             'total_bytes': result.total_bytes, 'error': result.error}
    encoded = json.dumps(value, ensure_ascii=False).encode('utf-8')
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError('worker_limit')
    Path(output_path).write_bytes(encoded)


def _decode(value, *, max_chars, size_bytes):
    if (type(value) is not dict or set(value) != {'status', 'text', 'processed_bytes', 'total_bytes', 'error'}
            or value['status'] not in {'complete', 'partial', 'error'} or type(value['text']) is not str
            or len(value['text']) > max_chars or type(value['processed_bytes']) is not int
            or type(value['total_bytes']) is not int or value['total_bytes'] != size_bytes):
        raise ValueError('worker_error')
    value['text'].encode('utf-8', errors='strict')
    if value['status'] == 'error':
        if value['text'] or value['processed_bytes'] != 0 or value['error'] not in _ERRORS:
            raise ValueError('worker_error')
    elif (value['processed_bytes'] != size_bytes or value['error'] is not None
          or (value['status'] == 'partial' and len(value['text']) != max_chars)):
        raise ValueError('worker_error')
    return PrefixResult(**value)


def read_docx_prefix(path, *, max_chars, timeout=15.0):
    """Read an already authorized DOCX as a prefix; never compute page full scope."""
    total = None
    try:
        if (type(max_chars) is not int or not 1 <= max_chars <= 100000 or isinstance(timeout, bool)
                or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0):
            return PrefixResult('error', error='invalid_arguments')
        path = Path(path)
        initial = path.lstat()
        total = initial.st_size
        if not stat.S_ISREG(initial.st_mode) or initial.st_uid != os.geteuid():
            return PrefixResult('error', total_bytes=total, error='source_invalid')
        if total > MAX_INPUT_BYTES:
            return PrefixResult('error', total_bytes=total, error='input_limit')
        expected = _fingerprint(initial)
        request = dict(version=1, mode='docx_prefix', path=str(path.absolute()), fingerprint=list(expected),
                       size_bytes=total, max_chars=max_chars)
        encoded = json.dumps(request).encode('utf-8')
        if len(encoded) > MAX_REQUEST_BYTES:
            return PrefixResult('error', total_bytes=total, error='invalid_arguments')
        timeout = min(float(timeout), 15.0)
        with tempfile.TemporaryDirectory(prefix='telegram-docx-prefix-') as directory:
            output_path = Path(directory) / 'result.json'
            process = subprocess.Popen([sys.executable, '-I', '-B', str(Path(__file__).absolute()),
                                        '--legacy-worker', str(output_path), str(timeout)],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True, close_fds=True)
            try:
                process.communicate(input=encoded, timeout=timeout)
            except subprocess.TimeoutExpired:
                return PrefixResult('error', total_bytes=total, error='worker_timeout')
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
                return PrefixResult('error', total_bytes=total, error='worker_limit' if process.returncode < 0 else 'worker_error')
            with output_path.open('rb') as source:
                raw = source.read(MAX_OUTPUT_BYTES + 1)
            if len(raw) > MAX_OUTPUT_BYTES:
                return PrefixResult('error', total_bytes=total, error='worker_limit')
            result = _decode(json.loads(raw, object_pairs_hook=_json_object), max_chars=max_chars, size_bytes=total)
            if _fingerprint(path.lstat()) != expected:
                return PrefixResult('error', total_bytes=total, error='artifact_changed')
            return result
    except (OSError, ValueError, TypeError, RecursionError):
        return PrefixResult('error', total_bytes=total, error='worker_error')


if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == '--legacy-worker':
    _worker_main(sys.argv[2], float(sys.argv[3]))
