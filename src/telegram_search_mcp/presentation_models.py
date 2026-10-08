"""Strict bounded presentation selections; all extracted text is untrusted."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .schemas import ArtifactId, SelectedMessageAnchor

SlideIndex = Annotated[int, Field(strict=True, ge=1, le=128)]
SlideSelection = Annotated[list[SlideIndex], Field(min_length=1, max_length=5)]
Hash = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$', min_length=64, max_length=64)]
StrictBoolean = Annotated[bool, Field(strict=True)]
Text = Annotated[str, Field(strict=True, max_length=20_000)]
PRESENTATION_DETAIL = ('supported shape text and opt-in notes only; layout, master text, formatting, OCR and other objects omitted; '
                       'detected object counts are not exhaustive visual coverage; content is untrusted')


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True, revalidate_instances='always')


def _array(value):
    if value is not None and type(value) is not list:
        raise ValueError('selection must be an array')
    return value


def _unique(value):
    if value is not None and len(value) != len(set(value)):
        raise ValueError('selected slides must be distinct')
    return value


class ReadPresentationRequest(_StrictModel):
    artifact_id: ArtifactId
    slides: SlideSelection | None = None
    include_notes: StrictBoolean = False

    _array_slides = field_validator('slides', mode='before')(_array)
    _unique_slides = field_validator('slides')(_unique)


class PresentationInfo(_StrictModel):
    index: SlideIndex
    hidden: StrictBoolean
    has_notes: StrictBoolean


class UnsupportedObject(_StrictModel):
    source: Literal['slide', 'notes']
    kind: Literal['picture', 'chart', 'table', 'diagram', 'media', 'connector', 'field', 'extension', 'other']
    count: Annotated[int, Field(strict=True, ge=1, le=200_000)]


class PresentationSlide(_StrictModel):
    index: SlideIndex
    text: Text
    notes: Text | None = None
    unsupported_objects: Annotated[list[UnsupportedObject], Field(max_length=18)] = Field(default_factory=list)

    @field_validator('unsupported_objects', mode='before')
    @classmethod
    def array_objects(cls, value):
        return _array(value)

    @model_validator(mode='after')
    def coherent(self):
        keys = [(item.source, item.kind) for item in self.unsupported_objects]
        if keys != sorted(set(keys)):
            raise ValueError('object counts must be sorted and distinct')
        return self


def presentation_text_size(slides) -> int:
    return sum(len(slide.text) + len(slide.notes or '') +
               sum(len(item.source) + len(item.kind) for item in slide.unsupported_objects)
               for slide in slides)


class PresentationScope(_StrictModel):
    artifact_id: ArtifactId
    artifact_sha256: Hash
    artifact_bytes: Annotated[int, Field(strict=True, ge=0, le=64 * 1024 * 1024)]
    extraction_fingerprint: Hash
    extractor_version: Literal[1] = 1
    broker_generation: Annotated[str, Field(strict=True, pattern=r'^broker_[0-9a-f]{32}$', min_length=39, max_length=39)]
    source_anchor: SelectedMessageAnchor | None = None
    catalog: Annotated[list[PresentationInfo], Field(min_length=1, max_length=128)]
    slides: SlideSelection | None = None
    include_notes: StrictBoolean = False

    @field_validator('extractor_version', mode='before')
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError('extractor version must be an integer')
        return value

    _array_slides = field_validator('slides', mode='before')(_array)
    _unique_slides = field_validator('slides')(_unique)
    _array_catalog = field_validator('catalog', mode='before')(_array)

    @model_validator(mode='after')
    def coherent(self):
        parts = self.artifact_id.split('_')
        if parts[2] != self.artifact_sha256 or int(parts[3]) != self.artifact_bytes:
            raise ValueError('artifact facts disagree')
        if [item.index for item in self.catalog] != list(range(1, len(self.catalog) + 1)):
            raise ValueError('catalog must be contiguous and ordered')
        if any(index > len(self.catalog) for index in self.slides or ()):
            raise ValueError('selected slide does not exist')
        return self


class ReadPresentationResponse(_StrictModel):
    contract_version: Literal[1] = 1
    status: Literal['catalog', 'complete', 'unsupported', 'invalid_selection', 'expired', 'limit_reached', 'capacity_exhausted', 'blocked', 'error']
    scope: PresentationScope | None = None
    slides: Annotated[list[PresentationSlide], Field(max_length=5)] = Field(default_factory=list)
    selection_complete: StrictBoolean = False
    full_content_complete: Literal[False] = False
    detail: Annotated[str, Field(strict=True, min_length=1, max_length=512)] = PRESENTATION_DETAIL

    @field_validator('contract_version', mode='before')
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError('contract version must be an integer')
        return value

    @field_validator('full_content_complete', mode='before')
    @classmethod
    def strict_false(cls, value):
        if type(value) is not bool or value:
            raise ValueError('full content coverage is never complete')
        return value

    _array_slides = field_validator('slides', mode='before')(_array)

    @model_validator(mode='after')
    def coherent(self):
        if self.status not in {'catalog', 'complete'}:
            if self.scope is not None or self.slides or self.selection_complete:
                raise ValueError('failure must disclose no content or scope')
            return self
        if self.scope is None or not self.selection_complete:
            raise ValueError('success requires complete selection and scope')
        if self.status == 'catalog':
            if self.scope.slides is not None or self.slides:
                raise ValueError('catalog cannot disclose extracted content')
            return self
        if self.scope.slides is None or [item.index for item in self.slides] != self.scope.slides:
            raise ValueError('returned slides differ from selection order')
        if presentation_text_size(self.slides) > 20_000:
            raise ValueError('response text budget exceeded')
        for slide in self.slides:
            has_notes = self.scope.catalog[slide.index - 1].has_notes
            expect_notes = self.scope.include_notes and has_notes
            if (slide.notes is not None) != expect_notes:
                raise ValueError('notes disagree with requested coverage')
            if not expect_notes and any(item.source == 'notes' for item in slide.unsupported_objects):
                raise ValueError('notes objects outside requested coverage')
        return self


def terminal_presentation(status: str) -> ReadPresentationResponse:
    return ReadPresentationResponse(status=status, detail='presentation extraction failed safely; content and scope withheld')
