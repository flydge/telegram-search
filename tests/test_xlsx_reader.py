"""Synthetic risk fixtures exercise the actual isolated worker."""
import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest
import subprocess
import sys
import warnings
import zipfile
from unittest.mock import patch
from xlsx_fixtures import CT, DOCREL, MAIN, PKGREL, worksheet, write_xlsx, xlsx_parts

try:
    from telegram_search_mcp import xlsx_reader as reader
except ImportError:
    reader = None


class XLSXReaderTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix='xlsx-tests-', dir=os.environ.get('TELEGRAM_PARSER_TEST_TMP'))
        self.addCleanup(self.sandbox.cleanup)
        self.base = Path(self.sandbox.name)
        self.assertIsNotNone(reader, 'the bounded XLSX parser is missing')

    def _read(self, path, **options):
        args = dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size_bytes=path.stat().st_size,
                    name=path.name, mime_type=None, selections=None)
        args.update(options)
        return reader.read_xlsx(path, **args)

    def _book(self, parts=None, **options):
        return write_xlsx(self.base / 'selected.xlsx', parts, **options)

    def _empty(self, result, statuses=('error', 'unsupported', 'limit_reached')):
        self.assertIn(result.status, statuses)
        self.assertEqual((result.catalog, result.cells, result.cell_start, result.cell_end, result.total_cells, result.has_more),
                         ((), (), 0, 0, 0, False))

    def test_catalog_discloses_sheet_order_and_visibility_without_any_values(self):
        path = self._book(xlsx_parts([('Visible', 'visible', worksheet('<row r="1"><c r="A1"><v>42</v></c></row>')),
                                     ('Hidden', 'hidden', worksheet('<row r="1"><c r="A1" t="inlineStr"><is><t>secret</t></is></c></row>')),
                                     ('VeryHidden', 'veryHidden', worksheet(''))]))
        result = self._read(path)
        self.assertEqual(result.status, 'catalog')
        self.assertEqual(result.catalog, (dict(index=1, name='Visible', state='visible'),
                                        dict(index=2, name='Hidden', state='hidden'),
                                        dict(index=3, name='VeryHidden', state='veryHidden')))
        self.assertEqual((result.cells, result.total_cells, result.has_more), ((), 0, False))

    def test_selected_overlap_order_and_blanks_reassemble_exactly(self):
        path = self._book(xlsx_parts([('Visible', 'visible', worksheet('<row r="1"><c r="A1"><v>42</v></c></row>')),
                                     ('Hidden', 'hidden', worksheet('<row r="2" hidden="1"><c r="B2" t="inlineStr"><is><t>é🙂</t></is></c><c r="C2"><v>9007199254740993</v></c></row>'))]))
        selections = (dict(sheet_index=2, range='B2:C3'), dict(sheet_index=2, range='C2'))
        cells = []
        offset = 0
        for expected_status in ('page', 'page', 'complete'):
            result = self._read(path, selections=selections, offset=offset, max_cells=2)
            self.assertEqual(result.status, expected_status)
            self.assertEqual(result.cell_start, offset)
            self.assertEqual(result.cell_end, offset + len(result.cells))
            self.assertEqual(result.total_cells, 5)
            cells.extend(result.cells)
            offset = result.cell_end
        self.assertEqual([(c['selection_index'], c['address'], c['value_type'], c['value']) for c in cells],
                         [(1, 'B2', 'inlineStr', 'é🙂'), (1, 'C2', 'n', '9007199254740993'),
                          (1, 'B3', 'blank', None), (1, 'C3', 'blank', None), (2, 'C2', 'n', '9007199254740993')])

    def test_values_formulas_and_missing_caches_remain_lexical(self):
        body = '<row r="1">' + ''.join([
            '<c r="A1"><f>SUM(B1:C1)</f><v>1.234567890123456789E+30</v></c>',
            '<c r="B1"><f t="shared" ref="B1:C1" si="7">A1+1</f><v>2</v></c>',
            '<c r="C1"><f t="shared" si="7"/><v/></c>',
            '<c r="D1"><f>NOW()</f></c>',
            '<c r="E1" t="s"><v>1</v></c>',
            '<c r="F1" t="b"><v>0</v></c>',
            '<c r="G1" t="e"><v>#DIV/0!</v></c>',
            '<c r="H1" t="str"><v/></c>',
            '<c r="I1" t="d"><v>2025-01-02T03:04:05Z</v></c>',
            '<c r="J1"><f t="array" ref="J1:J2">A1:A2*2</f><v>9</v></c>',
            '<c r="K1"><f t="dataTable" ref="K1:L2"/><v>4</v></c>',
        ]) + '</row>'
        path = self._book(xlsx_parts([('Visible', 'visible', worksheet(body))], shared='<si><t>first</t></si><si><r><t>rich </t></r><r><t>text</t></r></si>'))
        result = self._read(path, selections=(dict(sheet_index=1, range='A1:K1'),))
        self.assertEqual(result.status, 'complete')
        self.assertEqual([c['value'] for c in result.cells], ['1.234567890123456789E+30', '2', None, None,
                        'rich text', '0', '#DIV/0!', '', '2025-01-02T03:04:05Z', '9', '4'])
        self.assertEqual([(c['formula'], c['formula_kind'], c['formula_ref'], c['formula_shared_index']) for c in result.cells[:4]],
                         [('SUM(B1:C1)', 'normal', None, None), ('A1+1', 'shared', 'B1:C1', 7), ('', 'shared', None, 7), ('NOW()', 'normal', None, None)])

    def test_string_budget_stops_before_whole_cell_and_never_truncates(self):
        body = '<row r="1"><c r="A1" t="inlineStr"><is><t>' + 'x' * 11000 + '</t></is></c><c r="B1" t="inlineStr"><is><t>' + 'y' * 11000 + '</t></is></c></row>'
        path = self._book(xlsx_parts([('Visible', 'visible', worksheet(body))]), compression=zipfile.ZIP_STORED)
        first = self._read(path, selections=(dict(sheet_index=1, range='A1:B1'),))
        self.assertEqual((first.status, first.cell_end, len(first.cells[0]['value'])), ('page', 1, 11000))
        second = self._read(path, selections=(dict(sheet_index=1, range='A1:B1'),), offset=1)
        self.assertEqual((second.status, second.cell_start, second.cell_end), ('complete', 1, 2))
        body = body.replace('x' * 11000, 'x' * 20001)
        path = self._book(xlsx_parts([('Visible', 'visible', worksheet(body))]), compression=zipfile.ZIP_STORED)
        self._empty(self._read(path, selections=(dict(sheet_index=1, range='A1:B1'),)), ('limit_reached',))

    def test_selected_content_ceiling_fails_without_partial_cells(self):
        body = '<row r="1"><c r="A1" t="inlineStr"><is><t>' + 'x' * 11000 + '</t></is></c></row>'
        path = self._book(xlsx_parts([('Visible', 'visible', worksheet(body))]), compression=zipfile.ZIP_STORED)
        # The value is relevant once for each explicitly addressed repeated position.
        choices = (dict(sheet_index=1, range='A1:A25'), dict(sheet_index=1, range='A1:A26'),
                   dict(sheet_index=1, range='A1:A27'), dict(sheet_index=1, range='A1:A28'), dict(sheet_index=1, range='A1:A29'))
        # This sparse selection has only five nonempty positions and must not inflate blank text.
        result = self._read(path, selections=choices)
        self.assertEqual(result.status, 'page')
        repeated = ''.join(f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>{"x" * 11000}</t></is></c></row>' for i in range(1, 93))
        path = self._book(xlsx_parts([('Visible', 'visible', worksheet(repeated))]), compression=zipfile.ZIP_STORED)
        self._empty(self._read(path, selections=(dict(sheet_index=1, range='A1:A92'),)), ('limit_reached',))

    def test_invalid_selection_is_empty_even_when_other_ranges_are_valid(self):
        path = self._book()
        for selections in ((), [dict(sheet_index=1, range='A1')], (dict(sheet_index=2, range='A1'),),
                           (dict(sheet_index=1, range='A1'),) * 2, (dict(sheet_index=True, range='A1'),),
                           (dict(sheet_index=1, range='A:A'),), (dict(sheet_index=1, range='A1:B5001'),)):
            with self.subTest(selections=selections):
                self._empty(self._read(path, selections=selections), ('invalid_selection',))
        self._empty(self._read(path, selections=(dict(sheet_index=1, range='A1'),), offset=2), ('invalid_selection',))

    def test_every_relationship_and_declaration_is_validated_in_catalog_mode(self):
        for malicious in (f'<Relationships xmlns="{PKGREL}"><Relationship Id="unused" Type="{DOCREL}/hyperlink" Target="https://example.invalid" TargetMode="External"/></Relationships>',
                          f'<Relationships xmlns="{PKGREL}"><Relationship Id="same" Type="{DOCREL}/worksheet" Target="../workbook.xml"/><Relationship Id="same" Type="{DOCREL}/worksheet" Target="../workbook.xml"/></Relationships>'):
            parts = xlsx_parts()
            parts['xl/worksheets/_rels/sheet1.xml.rels'] = malicious
            self._empty(self._read(self._book(parts)))
        parts = xlsx_parts()
        parts['[Content_Types].xml'] = parts['[Content_Types].xml'].replace('</Types>', '<Override PartName="/xl/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/></Types>')
        parts['xl/vbaProject.bin'] = b'active'
        self._empty(self._read(self._book(parts)), ('unsupported',))

    def test_unsafe_ambiguous_and_missing_relationship_targets_fail_empty(self):
        for target in ('../../escape.xml', '/etc/passwd', 'worksheets/sheet1.xml#x', 'worksheets/sheet1.xml?x',
                       'worksheets%2fsheet1.xml', 'worksheets/./sheet1.xml', 'worksheets/missing.xml'):
            parts = xlsx_parts()
            parts['xl/_rels/workbook.xml.rels'] = parts['xl/_rels/workbook.xml.rels'].replace('worksheets/sheet1.xml', target)
            with self.subTest(target=target):
                self._empty(self._read(self._book(parts)))

    def test_dtd_entities_in_utf8_and_utf16_unused_xml_are_forbidden(self):
        for encoding in ('utf-8', 'utf-16'):
            parts = xlsx_parts()
            parts['xl/unused.xml'] = ('<?xml version="1.0" encoding="' + encoding + '"?><!DOCTYPE x [<!ENTITY leak SYSTEM "file:///secret">]><x>&leak;</x>').encode(encoding)
            self._empty(self._read(self._book(parts)))

    def test_duplicate_malformed_and_out_of_range_cells_fail_atomically(self):
        for body in ('<row r="1"><c r="A1"><v>1</v></c><c r="A1"><v>2</v></c></row>',
                     '<row r="1"><c r="a1"><v>1</v></c></row>',
                     '<row r="1"><c r="XFE1"><v>1</v></c></row>',
                     '<row r="1"><c r="A2"><v>1</v></c></row>',
                     '<row r="1"><c r="A1" t="s"><v>2</v></c></row>',
                     '<row r="1"><c r="A1"><v>1</v><v>2</v></c></row>',
                     '<row r="1"><c r="A1" t="b"><v>true</v></c></row>'):
            self._empty(self._read(self._book(xlsx_parts([('Visible', 'visible', worksheet(body))], shared='<si><t>x</t></si>')),
                                   selections=(dict(sheet_index=1, range='A1'),)))

    def test_unsupported_namespaces_sheet_types_and_formula_attributes_fail_cleanly(self):
        for replace in (('worksheet', 'chartsheet'), (MAIN, 'http://purl.oclc.org/ooxml/spreadsheetml/main')):
            parts = xlsx_parts()
            parts['xl/worksheets/sheet1.xml'] = parts['xl/worksheets/sheet1.xml'].replace(*replace)
            self._empty(self._read(self._book(parts)), ('unsupported',))
        for attributes in ('ca="1"', 't="dataTable" ref="A1:B2" r1="A1"', 't="shared" si="2147483648"', 't="unknown"'):
            parts = xlsx_parts([('Visible', 'visible', worksheet(f'<row r="1"><c r="A1"><f {attributes}>A2</f><v>1</v></c></row>'))])
            self._empty(self._read(self._book(parts), selections=(dict(sheet_index=1, range='A1'),)))

    def test_zip_filename_encryption_compression_and_bombs_are_rejected(self):
        for bad_name in ('../escape.xml', '/absolute.xml', 'xl\\bad.xml'):
            parts = xlsx_parts()
            parts[bad_name] = '<x/>'
            self._empty(self._read(self._book(parts)))
        path = self._book()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(path, 'a') as archive:
                archive.writestr('xl/workbook.xml', '<x/>')
        self._empty(self._read(path))
        self._empty(self._read(self._book(compression=zipfile.ZIP_BZIP2)), ('unsupported',))
        parts = xlsx_parts()
        parts['xl/bomb.xml'] = '<x>' + 'x' * 1000000 + '</x>'
        self._empty(self._read(self._book(parts)), ('limit_reached',))

    def test_xml_depth_and_aggregate_node_limits_fail_empty(self):
        parts = xlsx_parts()
        parts['xl/deep.xml'] = '<x>' * 65 + '</x>' * 65
        self._empty(self._read(self._book(parts)), ('limit_reached',))
        parts = xlsx_parts()
        for i in range(3):
            parts[f'xl/nodes{i}.xml'] = '<x>' + '<n/>' * 70000 + '</x>'
        self._empty(self._read(self._book(parts, compression=zipfile.ZIP_STORED)), ('limit_reached',))

    def test_hash_size_symlink_corruption_and_timeout_do_not_publish_partial_content(self):
        path = self._book()
        for options in (dict(sha256='0' * 64), dict(size_bytes=path.stat().st_size + 1)):
            self._empty(self._read(path, **options), ('error',))
        link = self.base / 'link.xlsx'
        link.symlink_to(path)
        self._empty(self._read(link), ('error',))
        result = self._read(path, timeout=0.000001)
        self._empty(result, ('limit_reached',))
        path.write_bytes(b'corrupt private bytes')
        result = self._read(path)
        self._empty(result, ('error',))
        self.assertNotIn('private', result.detail)

    def test_relationship_case_alias_and_invalid_unused_content_type_are_rejected(self):
        parts = xlsx_parts()
        parts['xl/worksheets/_rels/sheet1.xml.RELS'] = f'<Relationships xmlns="{PKGREL}"><Relationship Id="unused" Type="{DOCREL}/hyperlink" Target="https://example.invalid" TargetMode="External"/></Relationships>'
        self._empty(self._read(self._book(parts)))
        parts = xlsx_parts()
        parts['[Content_Types].xml'] = parts['[Content_Types].xml'].replace('</Types>', '<Default Extension="unused" ContentType=""/></Types>')
        self._empty(self._read(self._book(parts)))

    def test_malformed_rich_string_text_is_not_silently_dropped(self):
        for content in ('lost<t>kept</t>', '<r><t>kept</t>lost</r>', '<t>kept</t>lost'):
            parts = xlsx_parts([('Visible', 'visible', worksheet('<row r="1"><c r="A1" t="inlineStr"><is>' + content + '</is></c></row>'))])
            self._empty(self._read(self._book(parts), selections=(dict(sheet_index=1, range='A1'),)))

    def test_native_memory_budget_denies_large_additional_mapping(self):
        # Exercise the same limit-setting code with a smaller bounded test
        # allocation; Darwin includes inherited VM before adding the budget.
        code = ('import runpy\n'
                f'm=runpy.run_path({str(Path(reader.__file__).absolute())!r})\n'
                "fn=m['_set_limits']\n"
                "fn.__globals__['MAX_WORKER_MEMORY_BYTES']=64*1024*1024\n"
                'fn(3.0)\n'
                'try:\n value=bytes(256*1024*1024)\n print("uncapped")\n'
                'except MemoryError:\n print("capped")\n')
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        self.assertEqual((result.returncode, result.stdout), (0, b'capped\n'))

    def test_kernel_caps_actual_output_and_cpu_use(self):
        path = self.base / 'bounded-output'
        self.assertFalse(path.exists())
        prefix = f'import runpy\nm=runpy.run_path({str(Path(reader.__file__).absolute())!r})\nm["_set_limits"](1.0)\n'
        code = prefix + f'with open({str(path)!r}, "wb") as out:\n for _ in range(512):\n  out.write(b"x"*4096)\n'
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertGreater(path.stat().st_size, 0)
        self.assertLessEqual(path.stat().st_size, 1024 * 1024)
        result = subprocess.run([sys.executable, '-I', '-B', '-c', prefix + 'while True:\n pass\n'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        self.assertLess(result.returncode, 0)

    def test_parent_timeout_kills_and_reaps_a_stalled_real_worker(self):
        path = self._book()
        script = self.base / 'stalled-worker.py'
        script.write_text('import time\ntime.sleep(10)\n')
        original, processes = subprocess.Popen, []
        def record_process(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        started = time.monotonic()
        with patch.object(reader, '__file__', str(script)), patch.object(reader.subprocess, 'Popen', side_effect=record_process):
            result = self._read(path, timeout=0.05)
        self._empty(result, ('limit_reached',))
        self.assertEqual(result.detail, 'worker_timeout')
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll(), 'timed out parser process was not reaped')

    def test_source_change_after_actual_parsing_discards_all_content(self):
        path = self._book()
        args = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size_bytes=path.stat().st_size,
                    name=path.name, mime_type=None, selections=(dict(sheet_index=1, range='A1'),), offset=0, max_cells=200)
        original = reader._read_package
        def parse_then_change(data, request):
            result = original(data, request)
            path.write_bytes(b'changed bytes')
            return result
        with patch.object(reader, '_read_package', side_effect=parse_then_change):
            result = reader._parse_request(args)
        self._empty(result, ('error',))
        self.assertEqual(result.detail, 'artifact_changed')

    def test_dimensions_do_not_claim_whole_workbook_counts_and_grid_edge_is_addressed(self):
        parts = xlsx_parts([('Visible', 'visible', worksheet(''))])
        parts['xl/worksheets/sheet1.xml'] = parts['xl/worksheets/sheet1.xml'].replace('<sheetData>', '<dimension ref="A1:XFD1048576"/><sheetData>')
        path = self._book(parts)
        selections = (dict(sheet_index=1, range='XFD1048576'),)
        result = self._read(path, selections=selections)
        self.assertEqual((result.status, result.total_cells, result.cell_end), ('complete', 1, 1))
        self.assertEqual((result.cells[0]['address'], result.cells[0]['row'], result.cells[0]['column'], result.cells[0]['value_type']),
                         ('XFD1048576', 1048576, 16384, 'blank'))
        end = self._read(path, selections=selections, offset=1)
        self.assertEqual((end.status, end.cell_start, end.cell_end, end.cells), ('complete', 1, 1, ()))

    def test_encrypted_metadata_and_symlink_members_are_rejected_before_extracting(self):
        path = self._book()
        contents = bytearray(path.read_bytes())
        for signature, flag_offset in ((b'PK\x03\x04', 6), (b'PK\x01\x02', 8)):
            location = contents.index(signature)
            contents[location + flag_offset] |= 1
        path.write_bytes(contents)
        self._empty(self._read(path), ('unsupported',))
        path = self._book()
        with zipfile.ZipFile(path, 'a') as archive:
            member = zipfile.ZipInfo('xl/link.xml')
            member.create_system = 3
            member.external_attr = 0o120777 << 16
            archive.writestr(member, 'outside.xml')
        self._empty(self._read(path))

    def test_raw_formula_escapes_and_external_references_are_never_interpreted(self):
        parts = xlsx_parts([('Visible', 'visible', worksheet('<row r="1"><c r="A1"><f>_x000D_+[1]Sheet1!B2</f><v>1</v></c></row>'))])
        result = self._read(self._book(parts), selections=(dict(sheet_index=1, range='A1'),))
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.cells[0]['formula'], '_x000D_+[1]Sheet1!B2')
        parts['xl/worksheets/sheet1.xml'] = worksheet('<row r="1"><c r="A1" t="inlineStr"><is><t>_x000D_</t></is></c></row>')
        self._empty(self._read(self._book(parts), selections=(dict(sheet_index=1, range='A1'),)), ('unsupported',))


if __name__ == '__main__':
    unittest.main()
