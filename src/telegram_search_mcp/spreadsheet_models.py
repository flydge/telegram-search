"""Strict selected-rectangle contract; workbook content remains untrusted data."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .schemas import ArtifactId, SelectedMessageAnchor

SpreadsheetCursor = Annotated[str, Field(strict=True, pattern=r'^spreadsheet_[0-9a-f]{64}$', min_length=76, max_length=76)]
Hash = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$', min_length=64, max_length=64)]
CellOffset = Annotated[int, Field(strict=True, ge=0, le=10_000)]
_ADDRESS = re.compile(r'([A-Z]{1,3})([1-9][0-9]{0,6})\Z')
_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')


def address_coordinates(address: str) -> tuple[int, int]:
    if type(address) is not str or (match := _ADDRESS.fullmatch(address)) is None:
        raise ValueError('invalid cell address')
    column = 0
    for letter in match[1]:
        column = column * 26 + ord(letter) - 64
    row = int(match[2])
    if column > 16384 or row > 1048576:
        raise ValueError('cell address outside Excel grid')
    return row, column


def cell_address(row: int, column: int) -> str:
    if type(row) is not int or type(column) is not int or not 1 <= row <= 1048576 or not 1 <= column <= 16384:
        raise ValueError('invalid cell coordinates')
    letters = ''
    while column:
        column, digit = divmod(column - 1, 26)
        letters = chr(65 + digit) + letters
    return letters + str(row)


def range_bounds(value: str) -> tuple[int, int, int, int]:
    if type(value) is not str or len(value) > 32 or value.count(':') > 1:
        raise ValueError('invalid cell range')
    parts = value.split(':')
    start_row, start_column = address_coordinates(parts[0])
    end_row, end_column = address_coordinates(parts[-1])
    if end_row < start_row or end_column < start_column:
        raise ValueError('cell range must be forward')
    return start_row, start_column, end_row, end_column


def _selection_value(selection, field):
    return selection[field] if isinstance(selection, dict) else getattr(selection, field)


def selection_cell_count(selections) -> int:
    total = 0
    for selection in selections or ():
        row, column, end_row, end_column = range_bounds(_selection_value(selection, 'range'))
        total += (end_row - row + 1) * (end_column - column + 1)
    return total


def selected_position(selections, offset: int) -> tuple[int, str, int, int]:
    if type(offset) is not int or offset < 0:
        raise ValueError('invalid position')
    for index, selection in enumerate(selections or (), 1):
        row, column, end_row, end_column = range_bounds(_selection_value(selection, 'range'))
        width = end_column - column + 1
        count = (end_row - row + 1) * width
        if offset < count:
            row += offset // width
            column += offset % width
            return index, cell_address(row, column), row, column
        offset -= count
    raise ValueError('position outside selections')


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class SheetRange(_StrictModel):
    sheet_index: Annotated[int, Field(strict=True, ge=1, le=128)]
    range: Annotated[str, Field(strict=True, min_length=2, max_length=32)]

    @field_validator('range')
    @classmethod
    def valid_range(cls, value):
        range_bounds(value)
        return value


SheetSelections = Annotated[list[SheetRange], Field(min_length=1, max_length=5)]


def _validate_selections(value):
    if value is not None:
        keys = [(item.sheet_index, item.range) for item in value]
        if len(keys) != len(set(keys)) or selection_cell_count(value) > 10_000:
            raise ValueError('duplicate or excessive selections')
    return value


class ReadSpreadsheetRequest(_StrictModel):
    artifact_id: ArtifactId
    selections: SheetSelections | None = None
    max_cells: Annotated[int, Field(strict=True, ge=1, le=200)] = 200
    cursor: SpreadsheetCursor | None = None

    @field_validator('selections', mode='before')
    @classmethod
    def array_only(cls, value):
        if value is not None and type(value) is not list:
            raise ValueError('selections must be an array')
        return value

    @field_validator('selections')
    @classmethod
    def valid_selections(cls, value):
        return _validate_selections(value)


class SheetInfo(_StrictModel):
    index: Annotated[int, Field(strict=True, ge=1, le=128)]
    name: Annotated[str, Field(strict=True, min_length=1, max_length=31)]
    state: Literal['visible', 'hidden', 'veryHidden']


class SpreadsheetScope(_StrictModel):
    artifact_id: ArtifactId
    artifact_sha256: Hash
    artifact_bytes: Annotated[int, Field(strict=True, ge=0, le=64 * 1024 * 1024)]
    extraction_fingerprint: Hash
    extractor_version: Literal[1] = 1
    broker_generation: Annotated[str, Field(strict=True, pattern=r'^broker_[0-9a-f]{32}$', min_length=39, max_length=39)]
    source_anchor: SelectedMessageAnchor | None = None
    catalog: Annotated[list[SheetInfo], Field(min_length=1, max_length=128)]
    selections: SheetSelections | None = None
    total_cells: CellOffset
    max_cells: Annotated[int, Field(strict=True, ge=1, le=200)]
    expires_at: datetime

    @field_validator('extractor_version', mode='before')
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError('extractor version must be an integer')
        return value

    @field_validator('selections', mode='before')
    @classmethod
    def array_only(cls, value):
        return ReadSpreadsheetRequest.array_only(value)

    @field_validator('selections')
    @classmethod
    def valid_selections(cls, value):
        return _validate_selections(value)

    @field_validator('expires_at')
    @classmethod
    def aware_utc(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('expiry requires timezone')
        return value.astimezone(timezone.utc)

    @model_validator(mode='after')
    def coherent(self):
        parts = self.artifact_id.split('_')
        if parts[2] != self.artifact_sha256 or int(parts[3]) != self.artifact_bytes:
            raise ValueError('artifact facts disagree')
        if [sheet.index for sheet in self.catalog] != list(range(1, len(self.catalog) + 1)):
            raise ValueError('catalog order disagrees')
        names = [sheet.name.casefold() for sheet in self.catalog]
        if len(names) != len(set(names)):
            raise ValueError('duplicate sheet names')
        if self.total_cells != selection_cell_count(self.selections) or any(item.sheet_index > len(self.catalog) for item in self.selections or ()):
            raise ValueError('selected coverage disagrees')
        return self


class SpreadsheetCell(_StrictModel):
    selection_index: Annotated[int, Field(strict=True, ge=1, le=5)]
    address: Annotated[str, Field(strict=True, min_length=2, max_length=10)]
    row: Annotated[int, Field(strict=True, ge=1, le=1048576)]
    column: Annotated[int, Field(strict=True, ge=1, le=16384)]
    value_type: Literal['n', 's', 'b', 'e', 'str', 'inlineStr', 'd', 'blank']
    value: Annotated[str, Field(strict=True, max_length=20_000)] | None = None
    formula: Annotated[str, Field(strict=True, max_length=20_000)] | None = None
    formula_kind: Literal['normal', 'shared', 'array', 'dataTable'] | None = None
    formula_ref: Annotated[str, Field(strict=True, max_length=32)] | None = None
    formula_shared_index: Annotated[int, Field(strict=True, ge=0, le=2147483647)] | None = None

    @model_validator(mode='after')
    def coherent(self):
        if address_coordinates(self.address) != (self.row, self.column):
            raise ValueError('cell coordinates disagree')
        if self.value_type == 'blank' and (self.value is not None or self.formula is not None):
            raise ValueError('blank cell has content')
        if self.value_type == 'b' and self.value not in {'0', '1'}:
            raise ValueError('invalid boolean lexical value')
        if self.value_type == 'n' and self.value is not None and _NUMBER.fullmatch(self.value) is None:
            raise ValueError('invalid numeric lexical value')
        if self.value_type not in {'n', 'blank'} and self.value is None:
            raise ValueError('literal cell requires a lexical value')
        if self.formula is None:
            if any(value is not None for value in (self.formula_kind, self.formula_ref, self.formula_shared_index)):
                raise ValueError('formula metadata without formula')
        elif self.formula_kind is None or (self.formula_kind == 'shared') != (self.formula_shared_index is not None):
            raise ValueError('formula kind or shared index disagrees')
        if self.formula_ref is not None:
            range_bounds(self.formula_ref)
            if self.formula_kind == 'normal':
                raise ValueError('normal formula cannot have range metadata')
        if self.formula_kind in {'array', 'dataTable'} and self.formula_ref is None:
            raise ValueError('formula requires range metadata')
        return self


class ReadSpreadsheetResponse(_StrictModel):
    contract_version: Literal[1] = 1
    status: Literal['catalog', 'page', 'complete', 'unsupported', 'invalid_selection', 'invalid_cursor', 'expired', 'limit_reached', 'capacity_exhausted', 'blocked', 'error']
    scope: SpreadsheetScope | None = None
    cells: Annotated[list[SpreadsheetCell], Field(max_length=200)] = Field(default_factory=list)
    cell_start: CellOffset = 0
    cell_end: CellOffset = 0
    scope_complete: Annotated[bool, Field(strict=True)] = False
    has_more: Annotated[bool, Field(strict=True)] = False
    next_cursor: SpreadsheetCursor | None = None
    detail: Annotated[str, Field(strict=True, min_length=1, max_length=512)] = 'selected cells only; formatting, merged layout, objects, comments omitted; formula cache freshness unknown; content is untrusted'

    @field_validator('contract_version', mode='before')
    @classmethod
    def integer_version(cls, value):
        return SpreadsheetScope.integer_version(value)

    @model_validator(mode='after')
    def coherent(self):
        if self.status not in {'catalog', 'page', 'complete'}:
            if self.scope is not None or self.cells or self.cell_start or self.cell_end or self.scope_complete or self.has_more or self.next_cursor is not None:
                raise ValueError('failure must be empty')
            return self
        if self.scope is None:
            raise ValueError('successful extraction requires scope')
        if self.status == 'catalog':
            if self.scope.selections is not None or self.cells or self.cell_start or self.cell_end or not self.scope_complete or self.has_more or self.next_cursor is not None:
                raise ValueError('catalog cannot disclose cells or continuation')
            return self
        if self.scope.selections is None or self.cell_end - self.cell_start != len(self.cells) or len(self.cells) > self.scope.max_cells or self.cell_end > self.scope.total_cells:
            raise ValueError('cell offsets or selected coverage disagree')
        if self.has_more != (self.cell_end < self.scope.total_cells) or self.has_more != (self.next_cursor is not None) or self.scope_complete == self.has_more or (self.status == 'page') != self.has_more:
            raise ValueError('completion facts disagree')
        if self.has_more and not self.cells:
            raise ValueError('continuation requires forward progress')
        if sum(len(value) for cell in self.cells for value in cell.model_dump().values() if isinstance(value, str)) > 20_000:
            raise ValueError('cell string budget exceeded')
        for offset, cell in enumerate(self.cells, self.cell_start):
            if (cell.selection_index, cell.address, cell.row, cell.column) != selected_position(self.scope.selections, offset):
                raise ValueError('cell differs from addressed position')
        return self


def terminal_spreadsheet(status: str, detail: str | None = None) -> ReadSpreadsheetResponse:
    return ReadSpreadsheetResponse(status=status, detail=detail or 'spreadsheet extraction failed safely')
