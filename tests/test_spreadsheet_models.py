"""Risk tests for strict ranges, empty failures, and honest selected coverage."""
from datetime import datetime, timezone
import unittest
from pydantic import ValidationError

try:
    from telegram_search_mcp import spreadsheet_models as models
except ImportError:
    models = None

ARTIFACT = 'artifact_' + 'a' * 32 + '_' + 'b' * 64 + '_12'


class SpreadsheetModelTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(models, 'the bounded spreadsheet models are missing')

    def _scope(self, selections=None, total=0, max_cells=200):
        return dict(artifact_id=ARTIFACT, artifact_sha256='b' * 64,
                    artifact_bytes=12, extraction_fingerprint='c' * 64,
                    extractor_version=1, broker_generation='broker_' + 'd' * 32,
                    source_anchor=None, catalog=[dict(index=1, name='Visible', state='visible'),
                    dict(index=2, name='Private', state='veryHidden')],
                    selections=selections, total_cells=total, max_cells=max_cells,
                    expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc))

    def _cell(self, **updates):
        value = dict(selection_index=1, address='B2', row=2, column=2,
                     value_type='n', value='9007199254740993', formula=None,
                     formula_kind=None, formula_ref=None, formula_shared_index=None)
        value.update(updates)
        return value

    def test_request_accepts_ordered_overlaps_but_rejects_unsafe_and_duplicate_ranges(self):
        request = models.ReadSpreadsheetRequest(artifact_id=ARTIFACT,
            selections=[dict(sheet_index=2, range='B2:C3'), dict(sheet_index=2, range='C3')])
        self.assertEqual(request.max_cells, 200)
        self.assertEqual([item.range for item in request.selections], ['B2:C3', 'C3'])

    def test_ranges_enforce_excel_edges_forward_rectangles_and_addressed_ceiling(self):
        for value in ('a1', 'A0', 'A01', 'A:A', '1:2', '$A$1', 'XFE1', 'A1048577',
                      'B2:A3', 'A2:B1', 'A1:B10001', 'A1:B2:C3', 'A1\n'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                models.ReadSpreadsheetRequest(artifact_id=ARTIFACT,
                    selections=[dict(sheet_index=1, range=value)])
        valid = models.ReadSpreadsheetRequest(artifact_id=ARTIFACT,
            selections=[dict(sheet_index=128, range='XFD1048576')])
        self.assertEqual(valid.selections[0].range, 'XFD1048576')
        for values in ([], (dict(sheet_index=1, range='A1'),),
                       [dict(sheet_index=1, range='A1')] * 2,
                       [dict(sheet_index=True, range='A1')],
                       [dict(sheet_index=1, range='A1:B5000'), dict(sheet_index=2, range='A1')]):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                models.ReadSpreadsheetRequest(artifact_id=ARTIFACT, selections=values)

    def test_request_numbers_cursor_and_unknown_fields_are_strict(self):
        for options in (dict(max_cells=True), dict(max_cells='2'), dict(max_cells=0),
                        dict(max_cells=201), dict(cursor='spreadsheet_' + 'A' * 64),
                        dict(cursor='attachment_' + 'a' * 64), dict(path='/tmp/book.xlsx')):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                models.ReadSpreadsheetRequest(artifact_id=ARTIFACT, **options)

    def test_catalog_and_failure_responses_cannot_disclose_cell_content(self):
        response = models.ReadSpreadsheetResponse(status='catalog', scope=self._scope(),
            scope_complete=True)
        self.assertEqual(response.cells, [])
        for options in (dict(status='error', cells=[self._cell()]),
                        dict(status='error', scope=self._scope()),
                        dict(status='catalog', scope=self._scope(), cells=[self._cell()], scope_complete=True),
                        dict(status='catalog', scope=self._scope(), has_more=True, scope_complete=True),
                        dict(status='catalog', scope=self._scope([dict(sheet_index=1, range='A1')], 1), scope_complete=True)):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                models.ReadSpreadsheetResponse(**options)
        self.assertIsNone(models.terminal_spreadsheet('limit_reached').scope)

    def test_page_requires_exact_selected_coordinates_and_forward_completion(self):
        scope = self._scope([dict(sheet_index=2, range='B2:C2')], 2, 1)
        good = dict(status='page', scope=scope, cells=[self._cell()], cell_end=1,
                    has_more=True, next_cursor='spreadsheet_' + 'e' * 64)
        self.assertEqual(models.ReadSpreadsheetResponse(**good).cell_end, 1)
        for updates in (dict(cells=[self._cell(address='C2', column=3)]),
                        dict(cell_end=2), dict(next_cursor=None), dict(scope_complete=True),
                        dict(status='complete'), dict(cells=[self._cell(selection_index=2)])):
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                models.ReadSpreadsheetResponse(**(good | updates))
        complete = models.ReadSpreadsheetResponse(status='complete', scope=scope,
            cells=[self._cell(address='C2', column=3)], cell_start=1, cell_end=2,
            scope_complete=True)
        self.assertFalse(complete.has_more)

    def test_scope_binds_artifact_catalog_total_and_utc_expiry(self):
        for updates in (dict(artifact_bytes=13), dict(extractor_version=True),
                        dict(catalog=[]), dict(catalog=[dict(index=2, name='Wrong', state='visible')]),
                        dict(catalog=[dict(index=1, name='Same', state='visible'), dict(index=2, name='Same', state='hidden')]),
                        dict(selections=[dict(sheet_index=3, range='A1')], total_cells=1),
                        dict(selections=[dict(sheet_index=1, range='A1:B2')], total_cells=3),
                        dict(expires_at=datetime(2030, 1, 1))):
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                models.SpreadsheetScope(**(self._scope() | updates))

    def test_cell_lexical_and_formula_metadata_are_not_coerced_or_expanded(self):
        cell = models.SpreadsheetCell(**self._cell(formula='', formula_kind='shared', formula_shared_index=7))
        self.assertEqual(cell.formula, '')
        self.assertEqual(cell.value, '9007199254740993')
        for updates in (dict(value=2), dict(row=True), dict(column=3),
                        dict(value_type='b', value='true'), dict(value_type='blank', value='x'),
                        dict(formula_kind='shared'), dict(formula='A1', formula_shared_index=1),
                        dict(formula='A1', formula_kind='shared', formula_shared_index=-1), dict(unknown='x')):
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                models.SpreadsheetCell(**self._cell(**updates))

    def test_cell_page_budget_counts_all_string_fields_without_truncation(self):
        scope = self._scope([dict(sheet_index=1, range='B2')], 1)
        with self.assertRaises(ValidationError):
            models.ReadSpreadsheetResponse(status='complete', scope=scope,
                cells=[self._cell(value_type='inlineStr', value='x' * 20000)], cell_end=1, scope_complete=True)

    def test_impossible_lexical_caches_are_rejected_on_hostile_wire(self):
        for kind, value in [('n', 'NaN'), ('n', 'one'), ('n', ''), ('n', '1 2'),
                            ('s', None), ('str', None), ('inlineStr', None), ('e', None), ('d', None)]:
            with self.subTest(kind=kind, value=value), self.assertRaises(ValidationError):
                models.SpreadsheetCell(**self._cell(value_type=kind, value=value))
        for value in ('1.23000', '-.5', '+4E-10', None):
            self.assertEqual(models.SpreadsheetCell(**self._cell(value=value)).value, value)


if __name__ == '__main__':
    unittest.main()
