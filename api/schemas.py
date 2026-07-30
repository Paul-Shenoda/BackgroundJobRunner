"""Pydantic request/response models.

Only the request side is modelled here. Response bodies come from
Task.to_api_dict() — models.py already owns the nine-key (well, six on the
core object) contract shape, and CLAUDE.md is explicit that field names and
shapes are fixed by the spec. Rebuilding that shape in a second, parallel
pydantic model would just be two places for it to drift; routes.py returns
to_api_dict()'s plain dict directly instead.
"""

from typing import Any

from pydantic import BaseModel, Field


class SubmitRequest(BaseModel):
    lane: str
    params: dict[str, Any] = Field(default_factory=dict)
