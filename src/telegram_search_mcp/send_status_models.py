"""Closed, content-free read-only send status contract."""
from __future__ import annotations
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .draft_models import DraftId

Status = Literal["pending", "sent", "failed", "outcome_unknown"]
Evidence = Literal["none", "local_pending", "local_claimed", "provider_pending",
                   "provider_confirmed", "provider_failed", "local_failed"]
DETAILS = {
    "none": "outcome is unknown; no resend is authorized",
    "local_pending": "local draft is pending; no provider acceptance or approval is asserted",
    "local_claimed": "local attempt is claimed; no provider acceptance or approval is asserted",
    "provider_pending": "exact provider pending observation; terminal outcome is unconfirmed",
    "provider_confirmed": "exact provider confirmation; recipient delivery and read status are not asserted",
    "provider_failed": "exact provider rejection or send failure; no resend is authorized",
    "local_failed": "local preparation failed before provider send; no resend is authorized",
}
Detail = Literal[
    "outcome is unknown; no resend is authorized",
    "local draft is pending; no provider acceptance or approval is asserted",
    "local attempt is claimed; no provider acceptance or approval is asserted",
    "exact provider pending observation; terminal outcome is unconfirmed",
    "exact provider confirmation; recipient delivery and read status are not asserted",
    "exact provider rejection or send failure; no resend is authorized",
    "local preparation failed before provider send; no resend is authorized",
]

class GetSendStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    draft_id: DraftId

class GetSendStatusResponse(GetSendStatusRequest):
    status: Status
    evidence: Evidence
    detail: Detail
    message_id: Annotated[int, Field(strict=True, gt=0, lt=2**53)] | None = None

    @model_validator(mode="after")
    def exact_evidence(self):
        statuses = {"none":"outcome_unknown", "local_pending":"pending", "local_claimed":"pending",
            "provider_pending":"pending", "provider_confirmed":"sent", "provider_failed":"failed",
            "local_failed":"failed"}
        compatible = self.status == statuses[self.evidence] or (
            self.evidence == "provider_pending" and self.status == "outcome_unknown")
        if not compatible or self.detail != DETAILS[self.evidence]:
            raise ValueError("status evidence disagrees")
        if (self.status == "sent") != (self.message_id is not None):
            raise ValueError("only confirmed sends require a final message ID")
        return self

def send_status(draft_id: str, evidence: Evidence = "none", message_id: int | None = None,
                *, status_override: Status | None = None):
    statuses = {"none":"outcome_unknown", "local_pending":"pending", "local_claimed":"pending",
        "provider_pending":"pending", "provider_confirmed":"sent", "provider_failed":"failed", "local_failed":"failed"}
    return GetSendStatusResponse(draft_id=draft_id, status=status_override or statuses[evidence], evidence=evidence,
                                 detail=DETAILS[evidence], message_id=message_id)
