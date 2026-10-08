"""Synthetic hostile boundary tests run the real isolated PPTX worker."""
import hashlib
import importlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from unittest.mock import patch

from pptx_fixtures import A, P, CT, DOCREL, PKGREL, make_pptx, pptx_parts, shape_xml, text_shape

try:
    reader = importlib.import_module('telegram_search_mcp.pptx_reader')
except ModuleNotFoundError:
    reader = None


class PPTXReaderTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix='pptx-tests-', dir=os.environ.get('TELEGRAM_PARSER_TEST_TMP'))
        self.addCleanup(self.sandbox.cleanup)
        self.base = Path(self.sandbox.name)
        self.assertIsNotNone(reader, 'the bounded PPTX reader is missing')

    def _read(self, path, **options):
        arguments = dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size_bytes=path.stat().st_size,
                         name=path.name, mime_type=None, slides=None)
        arguments.update(options)
        return reader.read_pptx(path, **arguments)

    def _book(self, parts=None, **options):
        return make_pptx(self.base / 'selected.pptx', parts, **options)

    def _empty(self, result, statuses=('error', 'unsupported', 'limit_reached')):
        self.assertIn(result.status, statuses)
        self.assertEqual((result.catalog, result.slides), ((), ()))

    def test_catalog_validates_unused_external_relationship_without_leaking_selected_text(self):
        parts = pptx_parts([dict(body=text_shape('private selected synthetic'))])
        parts['ppt/slides/_rels/slide1.xml.rels'] = (f'<Relationships xmlns="{PKGREL}"><Relationship Id="unused" '
            f'Type="{DOCREL}/hyperlink" Target="https://example.invalid" TargetMode="External"/></Relationships>')
        result = self._read(self._book(parts))
        self._empty(result, ('unsupported',))
        self.assertNotIn('private', result.detail)

    def test_catalog_has_order_hidden_notes_facts_and_no_content(self):
        parts = pptx_parts([dict(part='slide9.xml', body=text_shape('first secret')),
                            dict(part='slide2.xml', hidden=True, body=text_shape('hidden secret'), notes=text_shape('note secret'))])
        result = self._read(self._book(parts), include_notes=True)
        self.assertEqual(result.status, 'catalog')
        self.assertEqual(result.catalog, (dict(index=1, hidden=False, has_notes=False), dict(index=2, hidden=True, has_notes=True)))
        self.assertEqual(result.slides, ())

    def test_selection_preserves_caller_order_unicode_spaces_breaks_groups_and_opt_in_notes(self):
        group = '<p:grpSp><p:nvGrpSpPr/><p:grpSpPr/>' + text_shape('nested é🙂', identity=3) + '</p:grpSp>'
        paragraphs = '<a:p><a:r><a:t>  keep </a:t></a:r><a:r><a:t>spaces  </a:t></a:r><a:br/><a:r><a:t>end</a:t></a:r></a:p><a:p/><a:p><a:r><a:t>last</a:t></a:r></a:p>'
        parts = pptx_parts([dict(body=text_shape('first')), dict(hidden=True, body=shape_xml(paragraphs) + group, notes=text_shape('notes header') + text_shape('notes body', identity=3))])
        path = self._book(parts)
        result = self._read(path, slides=(2, 1), include_notes=True)
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.slides, (dict(index=2, text='  keep spaces  \nend\n\nlast\nnested é🙂', notes='notes header\nnotes body', unsupported_objects=[]),
                                         dict(index=1, text='first', notes=None, unsupported_objects=[])))
        self.assertIsNone(self._read(path, slides=(2,)).slides[0]['notes'])

    def test_field_display_and_unknown_object_text_are_omitted_and_counted(self):
        paragraphs = '<a:p><a:r><a:t>before</a:t></a:r><a:fld id="synthetic" type="slidenum"><a:t>999 SECRET</a:t></a:fld><a:r><a:t>after</a:t></a:r></a:p>'
        body = shape_xml(paragraphs) + '<p:pic/><p:cxnSp/><p:graphicFrame><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl><a:t>TABLE SECRET</a:t></a:tbl></a:graphicData></a:graphic></p:graphicFrame><p:extLst><p:ext uri="synthetic"><a:t>EXT SECRET</a:t></p:ext></p:extLst>'
        result = self._read(self._book(pptx_parts([dict(body=body, notes='<p:pic/>')])), slides=(1,), include_notes=True)
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.slides[0]['text'], 'beforeafter')
        self.assertEqual(result.slides[0]['notes'], '')
        self.assertEqual(result.slides[0]['unsupported_objects'], [dict(source='notes', kind='picture', count=1),
            dict(source='slide', kind='connector', count=1), dict(source='slide', kind='extension', count=1),
            dict(source='slide', kind='field', count=1), dict(source='slide', kind='picture', count=1), dict(source='slide', kind='table', count=1)])

    def test_response_limit_is_atomic_includes_object_labels_and_never_truncates(self):
        path = self._book(pptx_parts([dict(body=text_shape('x' * 11000)), dict(body=text_shape('y' * 10000))]))
        self._empty(self._read(path, slides=(1, 2)), ('limit_reached',))
        self.assertEqual(len(self._read(path, slides=(1,)).slides[0]['text']), 11000)
        path = self._book(pptx_parts([dict(body=text_shape('x' * 19990) + '<p:pic/>')]))
        self._empty(self._read(path, slides=(1,)), ('limit_reached',))

    def test_invalid_selection_and_argument_types_never_extract(self):
        path = self._book()
        for options in (dict(slides=()), dict(slides=[1]), dict(slides=(True,)), dict(slides=(1, 1)),
                        dict(slides=(2,)), dict(slides=(0,)), dict(slides=(129,)), dict(include_notes=1),
                        dict(timeout=float('nan')), dict(size_bytes=True), dict(sha256='0' * 63)):
            with self.subTest(options=options):
                self._empty(self._read(path, **options), ('invalid_selection',))

    def test_all_relationships_reject_unknown_families_wrong_types_duplicates_and_unsafe_targets(self):
        for changes in (dict(kind='https://example.invalid/unknown'), dict(kind=DOCREL + '/worksheet'),
                        dict(kind='http://purl.oclc.org/ooxml/officeDocument/relationships/slide'),
                        dict(target='../../escape.xml'), dict(target='/etc/passwd'),
                        dict(target='slides/slide1.xml#x'), dict(target='slides/./slide1.xml'),
                        dict(target='slides%2fslide1.xml'), dict(target='slides/missing.xml')):
            parts = pptx_parts()
            relations = parts['ppt/_rels/presentation.xml.rels']
            relations = relations.replace(DOCREL + '/slide', changes.get('kind', DOCREL + '/slide'))
            relations = relations.replace('slides/slide1.xml', changes.get('target', 'slides/slide1.xml'))
            parts['ppt/_rels/presentation.xml.rels'] = relations
            with self.subTest(changes=changes):
                self._empty(self._read(self._book(parts)))
        for relation in (f'<Relationship Id="slide1" Type="{DOCREL}/slide" Target="slides/slide1.xml"/>',
                         f'<Relationship Id="duplicate" Type="{DOCREL}/slide" Target="slides/slide1.xml"/>'):
            parts = pptx_parts()
            parts['ppt/_rels/presentation.xml.rels'] = parts['ppt/_rels/presentation.xml.rels'].replace('</Relationships>', relation + '</Relationships>')
            self._empty(self._read(self._book(parts)))

    def test_notes_relationship_ambiguity_and_invalid_unselected_references_fail_catalog(self):
        parts = pptx_parts([dict(body=text_shape('one'), notes=text_shape('note'))])
        parts['ppt/slides/_rels/slide1.xml.rels'] = parts['ppt/slides/_rels/slide1.xml.rels'].replace('</Relationships>',
            f'<Relationship Id="again" Type="{DOCREL}/notesSlide" Target="../notesSlides/notes1.xml"/></Relationships>')
        self._empty(self._read(self._book(parts)))
        parts = pptx_parts([dict(body=text_shape('one')), dict(body='<p:pic><a:blip r:embed="missing"/></p:pic>')])
        self._empty(self._read(self._book(parts), slides=(1,)))

    def test_active_objects_and_unknown_content_types_even_unused_are_rejected(self):
        for addition in ('<p:oleObj/>', '<p:controls/>', '<p:control/>'):
            self._empty(self._read(self._book(pptx_parts([dict(body=text_shape('visible') + addition)]))), ('unsupported',))
        for kind in ('application/vnd.ms-office.vbaProject', 'application/vnd.openxmlformats-officedocument.oleObject',
                     'application/x-unknown-unsafe', 'application/vnd.ms-office.activeX+xml'):
            parts = pptx_parts()
            parts['[Content_Types].xml'] = parts['[Content_Types].xml'].replace('</Types>',
                f'<Default Extension="unused" ContentType="{kind}"/></Types>')
            self._empty(self._read(self._book(parts)), ('unsupported',))

    def test_dtd_entities_and_malformed_unused_xml_utf8_utf16_are_rejected(self):
        for encoding in ('utf-8', 'utf-16'):
            parts = pptx_parts()
            parts['ppt/unused.xml'] = (f'<?xml version="1.0" encoding="{encoding}"?><!DOCTYPE x [<!ENTITY leak SYSTEM "file:///secret">]><x>&leak;</x>').encode(encoding)
            self._empty(self._read(self._book(parts)), ('unsupported',))
        parts = pptx_parts()
        parts['ppt/unused.xml'] = '<x>'
        self._empty(self._read(self._book(parts)))

    def test_supported_text_corruption_is_not_silently_omitted_even_unselected(self):
        malformed = ('<a:p>lost<a:r><a:t>kept</a:t></a:r></a:p>',
                     '<a:p><a:r><a:t>kept</a:t>lost</a:r></a:p>',
                     '<a:p><a:r><a:t>first</a:t><a:t>second</a:t></a:r></a:p>',
                     '<a:p><a:r><a:t>_x0041_</a:t></a:r></a:p>',
                     '<a:p><a:r><a:t>kept<a:t>nested</a:t></a:t></a:r></a:p>',
                     '<a:p><a:unknown><a:t>lost</a:t></a:unknown></a:p>')
        for body in malformed:
            parts = pptx_parts([dict(body=text_shape('selected')), dict(body=shape_xml(body))])
            with self.subTest(body=body):
                self._empty(self._read(self._book(parts), slides=(1,)))

    def test_typed_roots_hidden_boolean_duplicate_shape_ids_and_slide_ids_are_validated(self):
        for target, old, new in (('ppt/slides/slide1.xml', '<p:sld ', '<p:notes '),
                                 ('ppt/slides/slide1.xml', P, 'http://purl.oclc.org/ooxml/presentationml/main'),
                                 ('ppt/slides/slide1.xml', '<p:sld ', '<p:sld show="maybe" '),
                                 ('ppt/presentation.xml', '</p:sldIdLst>', '<p:sldId id="256" r:id="slide1"/></p:sldIdLst>')):
            parts = pptx_parts()
            parts[target] = parts[target].replace(old, new)
            self._empty(self._read(self._book(parts)))
        self._empty(self._read(self._book(pptx_parts([dict(body=text_shape('one') + text_shape('two'))]))))

    def test_all_detected_object_families_are_counted_and_layout_text_is_not_extracted(self):
        frames = ''.join(f'<p:graphicFrame><a:graphic><a:graphicData uri="{uri}"><a:t>OMITTED</a:t></a:graphicData></a:graphic></p:graphicFrame>'
            for uri in ('http://schemas.openxmlformats.org/drawingml/2006/chart', 'http://schemas.openxmlformats.org/drawingml/2006/diagram', 'urn:unknown'))
        body = text_shape('supported') + frames + '<p:pic><a:videoFile/></p:pic><p:unknown><a:t>OMITTED</a:t></p:unknown>'
        result = self._read(self._book(pptx_parts([dict(body=body)])), slides=(1,))
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.slides[0]['text'], 'supported')
        self.assertEqual(result.slides[0]['unsupported_objects'], [dict(source='slide', kind='chart', count=1),
            dict(source='slide', kind='diagram', count=1), dict(source='slide', kind='media', count=1),
            dict(source='slide', kind='other', count=2), dict(source='slide', kind='picture', count=1)])
        parts = pptx_parts()
        parts['ppt/slideLayouts/layout.xml'] = (f'<p:sldLayout xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>'
            + text_shape('LAYOUT SECRET') + '</p:spTree></p:cSld></p:sldLayout>')
        parts['[Content_Types].xml'] = parts['[Content_Types].xml'].replace('</Types>', '<Override PartName="/ppt/slideLayouts/layout.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/></Types>')
        parts['ppt/slides/_rels/slide1.xml.rels'] = f'<Relationships xmlns="{PKGREL}"><Relationship Id="layout" Type="{DOCREL}/slideLayout" Target="../slideLayouts/layout.xml"/></Relationships>'
        self.assertEqual(self._read(self._book(parts), slides=(1,)).slides[0]['text'], 'synthetic slide')

    def test_zip_duplicate_case_traversal_special_members_compression_and_bombs_fail_empty(self):
        for name in ('../escape.xml', '/absolute.xml', 'ppt\\bad.xml', 'ppt/slides/SLIDE1.XML'):
            parts = pptx_parts()
            parts[name] = '<x/>'
            self._empty(self._read(self._book(parts)))
        path = self._book()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(path, 'a') as archive:
                archive.writestr('ppt/presentation.xml', '<x/>')
        self._empty(self._read(path))
        self._empty(self._read(self._book(compression=zipfile.ZIP_BZIP2)), ('unsupported',))
        parts = pptx_parts()
        parts['ppt/bomb.xml'] = '<x>' + 'x' * 1000000 + '</x>'
        self._empty(self._read(self._book(parts, compression=zipfile.ZIP_DEFLATED)), ('limit_reached',))

    def test_xml_depth_aggregate_nodes_and_selected_extraction_limits_fail_empty(self):
        parts = pptx_parts()
        parts['ppt/deep.xml'] = '<x>' * 65 + '</x>' * 65
        self._empty(self._read(self._book(parts)), ('limit_reached',))
        parts = pptx_parts()
        for index in range(3):
            parts[f'ppt/nodes{index}.xml'] = '<x>' + '<n/>' * 70000 + '</x>'
        self._empty(self._read(self._book(parts)), ('limit_reached',))
        path = self._book(pptx_parts([dict(body=text_shape('x' * 1000001))]))
        self._empty(self._read(path, slides=(1,)), ('limit_reached',))

    def test_source_hash_size_symlink_and_corruption_never_expose_content(self):
        path = self._book()
        for options in (dict(sha256='0' * 64), dict(size_bytes=path.stat().st_size + 1)):
            self._empty(self._read(path, **options), ('error',))
        link = self.base / 'link.pptx'
        link.symlink_to(path)
        self._empty(self._read(link), ('error',))
        path.write_bytes(b'private malformed synthetic')
        result = self._read(path)
        self._empty(result, ('error',))
        self.assertNotIn('private', result.detail)

    def test_timeout_kills_and_reaps_the_actual_worker(self):
        path = self._book()
        processes = []
        actual_popen = subprocess.Popen
        def capture(*args, **kwargs):
            process = actual_popen(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(reader.subprocess, 'Popen', capture):
            self._empty(self._read(path, timeout=0.000001), ('limit_reached',))
        self.assertEqual(len(processes), 1)
        self.assertLess(processes[0].returncode, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
        with self.assertRaises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)

    def test_native_memory_output_cpu_fd_and_core_caps_are_enforced_in_real_subprocess(self):
        prefix = ('import runpy,resource,os\n' + f'm=runpy.run_path({str(Path(reader.__file__).absolute())!r})\n'
                  'm["_common"].MAX_WORKER_MEMORY_BYTES=64*1024*1024\nm["_set_limits"](1.0)\n')
        code = prefix + 'try:\n value=bytes(256*1024*1024)\n print("uncapped")\nexcept MemoryError:\n print("capped")\nprint(resource.getrlimit(resource.RLIMIT_CORE))\nfds=[]\ntry:\n while True: fds.append(os.open("/dev/null",os.O_RDONLY))\nexcept OSError:\n print("fd capped",len(fds)<64)\n'
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        self.assertEqual((result.returncode, result.stdout), (0, b'capped\n(0, 0)\nfd capped True\n'))
        output = self.base / 'bounded-output'
        code = prefix + f'with open({str(output)!r},"wb") as out:\n for _ in range(512): out.write(b"x"*4096)\n'
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertGreater(output.stat().st_size, 0)
        self.assertLessEqual(output.stat().st_size, 1024 * 1024)
        result = subprocess.run([sys.executable, '-I', '-B', '-c', prefix + 'while True:\n pass\n'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        self.assertLess(result.returncode, 0)

    def test_parent_rejects_hostile_worker_schema_and_selection_drift(self):
        request = dict(slides=(1,), include_notes=False)
        valid = dict(status='complete', catalog=[dict(index=1, hidden=False, has_notes=False)],
                     slides=[dict(index=1, text='supported', notes=None, unsupported_objects=[])], detail='')
        for changes in (dict(slides=[dict(index=2, text='secret', notes=None, unsupported_objects=[])]),
                        dict(slides=[dict(index=1, text='supported', notes='secret', unsupported_objects=[])]),
                        dict(status='error'), dict(detail='private exception'), dict(extra='private')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reader._decode_result(dict(valid, **changes), request)

    def test_formatting_and_group_metadata_cannot_hide_corrupted_supported_text(self):
        for malformed in ('<a:p><a:pPr><a:t>lost</a:t></a:pPr><a:r><a:t>keep</a:t></a:r></a:p>',
                          '<a:p><a:r><a:rPr>lost</a:rPr><a:t>keep</a:t></a:r></a:p>',
                          '<a:p><a:endParaRPr>lost</a:endParaRPr></a:p>'):
            with self.subTest(malformed=malformed):
                self._empty(self._read(self._book(pptx_parts([dict(body=shape_xml(malformed))])), slides=(1,)))
        body = '<p:grpSp><p:nvGrpSpPr><a:t>lost</a:t></p:nvGrpSpPr><p:grpSpPr/>' + text_shape('keep') + '</p:grpSp>'
        self._empty(self._read(self._book(pptx_parts([dict(body=body)])), slides=(1,)))

    def test_nested_media_is_counted_once_and_extensions_in_shapes_are_omitted(self):
        body = '<p:grpSp><p:nvGrpSpPr/><p:grpSpPr/><p:pic><a:videoFile/></p:pic></p:grpSp>'
        shape = text_shape('keep').replace('</p:sp>', '<p:extLst><p:ext uri="synthetic"><a:t>EXT SECRET</a:t></p:ext></p:extLst></p:sp>')
        result = self._read(self._book(pptx_parts([dict(body=shape + body)])), slides=(1,))
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.slides[0]['text'], 'keep')
        self.assertEqual(result.slides[0]['unsupported_objects'], [dict(source='slide', kind='extension', count=1),
            dict(source='slide', kind='media', count=1), dict(source='slide', kind='picture', count=1)])

    def test_wrong_namespace_structural_elements_and_relationship_kind_references_fail(self):
        body = text_shape('lost').replace('<p:sp>', '<a:sp>').replace('</p:sp>', '</a:sp>')
        self._empty(self._read(self._book(pptx_parts([dict(body=body)]))), ('unsupported',))
        parts = pptx_parts([dict(body=text_shape('keep'), notes=text_shape('note'))])
        parts['ppt/slides/slide1.xml'] = parts['ppt/slides/slide1.xml'].replace('</p:spTree>', '<p:pic><a:blip r:embed="notes"/></p:pic></p:spTree>')
        self._empty(self._read(self._book(parts)), ('unsupported',))
        parts = pptx_parts()
        parts['ppt/presentation.xml'] = parts['ppt/presentation.xml'].replace(f'xmlns:p="{P}"', f'xmlns:p="{P}" xmlns:a="{A}"').replace('</p:presentation>', '<a:sldIdLst/></p:presentation>')
        self._empty(self._read(self._book(parts)), ('unsupported',))

    def test_ambiguous_dot_targets_and_notes_links_from_unused_sources_fail(self):
        parts = pptx_parts()
        parts['ppt/_rels/presentation.xml.rels'] = parts['ppt/_rels/presentation.xml.rels'].replace('slides/slide1.xml', 'slides/../slides/slide1.xml')
        self._empty(self._read(self._book(parts)))
        parts = pptx_parts([dict(body=text_shape('keep'), notes=text_shape('note'))])
        parts['ppt/unused.xml'] = '<x/>'
        parts['ppt/_rels/unused.xml.rels'] = f'<Relationships xmlns="{PKGREL}"><Relationship Id="invalid" Type="{DOCREL}/notesSlide" Target="notesSlides/notes1.xml"/></Relationships>'
        self._empty(self._read(self._book(parts)))

    def test_passive_media_relationships_are_validated_and_never_extracted(self):
        body = '<p:pic><a:videoFile r:link="video"/><x:media xmlns:x="http://schemas.microsoft.com/office/powerpoint/2010/main" r:embed="media"/></p:pic>'
        parts = pptx_parts([dict(body=text_shape('supported') + body)])
        parts['ppt/media/video.mp4'] = b'synthetic media not played'
        parts['[Content_Types].xml'] = parts['[Content_Types].xml'].replace('</Types>', '<Default Extension="mp4" ContentType="video/mp4"/></Types>')
        parts['ppt/slides/_rels/slide1.xml.rels'] = f'<Relationships xmlns="{PKGREL}"><Relationship Id="video" Type="{DOCREL}/video" Target="../media/video.mp4"/><Relationship Id="media" Type="http://schemas.microsoft.com/office/2007/relationships/media" Target="../media/video.mp4"/></Relationships>'
        result = self._read(self._book(parts), slides=(1,))
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.slides[0]['text'], 'supported')
        self.assertEqual(result.slides[0]['unsupported_objects'], [dict(source='slide', kind='media', count=2), dict(source='slide', kind='picture', count=1)])

    def test_empty_notes_absent_notes_and_object_labels_are_consistent(self):
        path = self._book(pptx_parts([dict(body=text_shape('one'), notes=''), dict(body=text_shape('two'))]))
        result = self._read(path, slides=(1, 2), include_notes=True)
        self.assertEqual(result.status, 'complete')
        self.assertEqual([item['notes'] for item in result.slides], ['', None])
        result = self._read(path, slides=(1, 2))
        self.assertEqual([item['notes'] for item in result.slides], [None, None])

    def test_slide_id_list_order_is_independent_of_relationship_serialization(self):
        parts = pptx_parts([dict(body=text_shape('first'), part='slide9.xml'), dict(body=text_shape('second'), part='slide2.xml')])
        relations = f'<Relationships xmlns="{PKGREL}"><Relationship Id="slide2" Type="{DOCREL}/slide" Target="slides/slide2.xml"/><Relationship Id="slide1" Type="{DOCREL}/slide" Target="slides/slide9.xml"/></Relationships>'
        parts['ppt/_rels/presentation.xml.rels'] = relations
        result = self._read(self._book(parts), slides=(1, 2))
        self.assertEqual(result.status, 'complete')
        self.assertEqual([item['text'] for item in result.slides], ['first', 'second'])

    def test_input_zip_member_and_xml_size_preflights_fail_without_extraction(self):
        path = self._book()
        self._empty(self._read(path, size_bytes=64 * 1024 * 1024 + 1), ('limit_reached',))
        parts = pptx_parts()
        for index in range(1025):
            parts[f'ppt/extra{index}.xml'] = '<x/>'
        self._empty(self._read(self._book(parts)), ('limit_reached',))
        parts = pptx_parts()
        parts['ppt/oversized.xml'] = '<x>' + 'x' * (8 * 1024 * 1024) + '</x>'
        self._empty(self._read(self._book(parts)), ('limit_reached',))
        # A tiny hostile ZIP declares an aggregate over 32 MiB in its central
        # directory. Preflight must reject it before trying to inflate payloads.
        path = self._book()
        encoded = bytearray(path.read_bytes())
        offset = encoded.index(b'PK\x01\x02')
        struct.pack_into('<I', encoded, offset + 24, 32 * 1024 * 1024 + 1)
        path.write_bytes(encoded)
        self._empty(self._read(path), ('limit_reached',))

    def test_zip_encryption_and_symbolic_link_metadata_are_forbidden(self):
        path = self._book()
        encoded = bytearray(path.read_bytes())
        for signature, flag_offset in ((b'PK\x01\x02', 8), (b'PK\x03\x04', 6)):
            offset = encoded.index(signature)
            flags = struct.unpack_from('<H', encoded, offset + flag_offset)[0]
            struct.pack_into('<H', encoded, offset + flag_offset, flags | 1)
        path.write_bytes(encoded)
        self._empty(self._read(path), ('unsupported',))
        path = self._book()
        with zipfile.ZipFile(path, 'a') as archive:
            info = zipfile.ZipInfo('ppt/linked.xml')
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            archive.writestr(info, 'secret target')
        self._empty(self._read(path), ('error',))

    def test_source_drift_after_real_worker_completion_discards_its_content(self):
        path = self._book()
        actual_popen = subprocess.Popen
        def capture(*args, **kwargs):
            process = actual_popen(*args, **kwargs)
            communicate = process.communicate
            def drift(*args, **kwargs):
                output = communicate(*args, **kwargs)
                path.write_bytes(b'changed while extraction completed')
                return output
            process.communicate = drift
            return process
        with patch.object(reader.subprocess, 'Popen', capture):
            result = self._read(path, slides=(1,))
        self._empty(result, ('error',))
        self.assertEqual(result.detail, 'artifact_changed')

    def test_worker_ignores_pythonpath_and_cwd_module_injection(self):
        path = self._book()
        (self.base / 'xlsx_reader.py').write_text('raise RuntimeError("untrusted module loaded")', encoding='utf-8')
        (self.base / 'sitecustomize.py').write_text('raise RuntimeError("untrusted site loaded")', encoding='utf-8')
        previous_directory = Path.cwd()
        try:
            os.chdir(self.base)
            with patch.dict(os.environ, {'PYTHONPATH': str(self.base)}):
                result = self._read(path, slides=(1,))
        finally:
            os.chdir(previous_directory)
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.slides[0]['text'], 'synthetic slide')

    def test_relationship_content_type_and_namespace_require_canonical_relationship_paths(self):
        malicious = (f'<Relationships xmlns="{PKGREL}"><Relationship Id="external" Type="{DOCREL}/hyperlink" '
                     'Target="https://example.invalid" TargetMode="External"/></Relationships>')
        for name, kind in (('ppt/unused.xml', 'application/vnd.openxmlformats-package.relationships+xml'),
                           ('ppt/unused.bin', 'application/vnd.openxmlformats-package.relationships+xml'),
                           ('ppt/unused.xml', 'application/xml'), ('ppt/unused.xml', 'text/xml'),
                           ('ppt/unused.bin', 'application/xml')):
            parts = pptx_parts()
            parts[name] = malicious
            parts['[Content_Types].xml'] = parts['[Content_Types].xml'].replace('</Types>',
                f'<Override PartName="/{name}" ContentType="{kind}"/></Types>')
            with self.subTest(name=name, kind=kind):
                self._empty(self._read(self._book(parts), slides=(1,)), ('unsupported',))
                self._empty(self._read(self._book(parts)), ('unsupported',))
        parts = pptx_parts()
        parts['ppt/unused.xml'] = '<x/>'
        parts['[Content_Types].xml'] = parts['[Content_Types].xml'].replace('</Types>',
            '<Override PartName="/ppt/unused.xml" ContentType="application/vnd.openxmlformats-package.relationships+xml"/></Types>')
        self._empty(self._read(self._book(parts)), ('unsupported',))

    def test_nested_relationship_namespace_nodes_or_attributes_cannot_hide_in_unused_parts(self):
        relationship = (f'<r:Relationship xmlns:r="{PKGREL}" Id="external" Type="{DOCREL}/hyperlink" '
                        'Target="https://example.invalid" TargetMode="External"/>')
        for xml in ('<x>' + relationship + '</x>', f'<x xmlns:r="{PKGREL}" r:Id="spoofed"/>'):
            parts = pptx_parts()
            parts['ppt/unused.xml'] = xml
            with self.subTest(xml=xml):
                self._empty(self._read(self._book(parts), slides=(1,)), ('unsupported',))
        body = text_shape('selected') + '<p:extLst><p:ext uri="synthetic">' + relationship + '</p:ext></p:extLst>'
        self._empty(self._read(self._book(pptx_parts([dict(body=body)])), slides=(1,)), ('unsupported',))

    def test_content_type_namespace_is_exclusive_to_the_package_content_type_part(self):
        spoofed = f'<Types xmlns="{CT}"><Override PartName="/ppt/unused.xml" ContentType="application/xml"/></Types>'
        for xml in (spoofed, '<x>' + spoofed + '</x>', f'<x xmlns:c="{CT}" c:ContentType="application/xml"/>'):
            parts = pptx_parts()
            parts['ppt/unused.xml'] = xml
            with self.subTest(xml=xml):
                self._empty(self._read(self._book(parts), slides=(1,)), ('unsupported',))


if __name__ == '__main__':
    unittest.main()
