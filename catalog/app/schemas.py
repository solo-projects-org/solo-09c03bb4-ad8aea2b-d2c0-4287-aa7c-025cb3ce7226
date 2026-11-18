"""Pydantic response models."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

BandName = Literal["B04", "B08"]


class BandOut(BaseModel):
    band: BandName
    sha256: str
    size_bytes: int
    download_url: str


class RevisionOut(BaseModel):
    scene_id: str
    revision: int
    acquisition_time: str
    west: float
    south: float
    east: float
    north: float
    bands: list[BandOut]
    # Only meaningful on the POST that stored the revision; null on GET.
    submission_status: Optional[
        Literal["created", "idempotent_replay"]
    ] = Field(default=None,
              description="created = stored now; idempotent_replay = identical revision already existed; null = fetched via GET")


class SceneSummary(BaseModel):
    scene_id: str
    revision: int
    acquisition_time: str
    west: float
    south: float
    east: float
    north: float
    bands: list[BandOut]


class HealthOut(BaseModel):
    status: Literal["ok"]
    database: Literal["ok"]


class QCOut(BaseModel):
    """Spatial QC state of one revision (always the current one via the API)."""

    scene_id: str
    revision: int
    status: Literal["pending", "approved", "rejected"] = Field(
        description="pending = 待检 (check not completed); "
        "approved = 通过 (grids and registered rectangle match); "
        "rejected = 不符 (see reasons)"
    )
    reasons: list[str] = Field(
        description="Mismatch reasons found by the check; empty when approved or still pending"
    )
    started_at: Optional[str] = Field(default=None, description="UTC time the last run started")
    finished_at: Optional[str] = Field(default=None, description="UTC time the verdict was stored")
    detail: Optional[str] = Field(
        default=None, description="Extra information, only set by the POST that starts QC"
    )
