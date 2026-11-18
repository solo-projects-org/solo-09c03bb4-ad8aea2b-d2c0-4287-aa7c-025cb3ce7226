"""Spatial quality control (QC) for the current revision of a scene.

The check reads the two cataloged band files with Rasterio and verifies, for
single-band GeoTIFF imagery on an EPSG:4326, north-up grid:

* both bands are readable GeoTIFFs (plain files are refused up front by the
  API endpoint and never start a check);
* each band is single-band, EPSG:4326 and north-up (no rotation/shear);
* the B04 and B08 grids coincide (dimensions and geotransform);
* the registered coverage rectangle matches each band's image bounds.

Runs execute on a background thread pool so a slow check never blocks other
requests (e.g. submissions for other scenes). Only the final UPDATE persists a
verdict: if the process is interrupted or the UPDATE fails, the revision stays
``pending``. Results are keyed by (scene_id, revision), so a late result for
an old revision can never approve a newer one.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import rasterio
from rasterio.errors import RasterioError

from .db import _connect

log = logging.getLogger("catalog.qc")

#: Absolute tolerance (degrees) when comparing registered bounds and grid
#: parameters. ~0.1 mm at the equator: forgives float noise, catches real
#: misalignment.
TOLERANCE_DEG = 1e-6

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="scene-qc")
_inflight: set[tuple[str, int]] = set()
_inflight_lock = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FMT)


# --------------------------------------------------------------------------- #
# Raster inspection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BandGrid:
    driver: str
    count: int
    width: int
    height: int
    epsg: Optional[int]
    transform: tuple  # affine.Affine (6 coefficients, tuple-like)
    bounds: tuple  # (west, south, east, north)


def _inspect(path: Path) -> BandGrid:
    with rasterio.open(path) as ds:
        crs = ds.crs
        t = tuple(ds.transform)[:6]
        # Image bounds from the 4 outer pixel corners (avoids rasterio's
        # deprecated affine `*` usage inside ds.bounds).
        a, b, c, d, e, f = t
        w, h = ds.width, ds.height
        xs = (c, c + a * w, c + b * h, c + a * w + b * h)
        ys = (f, f + d * w, f + e * h, f + d * w + e * h)
        return BandGrid(
            driver=ds.driver,
            count=ds.count,
            width=w,
            height=h,
            epsg=crs.to_epsg() if crs else None,
            transform=t,
            bounds=(min(xs), min(ys), max(xs), max(ys)),
        )


def precheck_geotiff(path: Path) -> Optional[str]:
    """Return None if *path* is a GeoTIFF, else a short refusal reason.

    Used by the API endpoint to refuse starting QC for plain (non-GeoTIFF)
    band files. Only the file header is read.
    """
    try:
        with rasterio.open(path) as ds:
            if ds.driver != "GTiff":
                return f"not a GeoTIFF (driver={ds.driver})"
    except RasterioError:
        return "not a GeoTIFF (unrecognized or unreadable raster)"
    except Exception as exc:  # pragma: no cover - defensive
        return f"cannot be opened as a raster ({exc})"
    return None


def _structural_reasons(band: str, grid: BandGrid) -> list[str]:
    """Per-band requirements: GeoTIFF, single-band, EPSG:4326, north-up."""
    reasons: list[str] = []
    if grid.driver != "GTiff":
        reasons.append(f"{band}: not a GeoTIFF (driver={grid.driver})")
        return reasons  # further checks are meaningless
    if grid.count != 1:
        reasons.append(
            f"{band}: expected a single-band GeoTIFF, found {grid.count} bands"
        )
    if grid.epsg != 4326:
        got = f"EPSG:{grid.epsg}" if grid.epsg is not None else "no CRS"
        reasons.append(f"{band}: CRS must be EPSG:4326, got {got}")
    a, b, _c, d, e, _f = grid.transform
    if b != 0 or d != 0:
        reasons.append(
            f"{band}: grid is not north-up (transform has rotation/shear terms)"
        )
    elif e >= 0:
        reasons.append(
            f"{band}: grid is not north-up (pixel height must be negative, got {e})"
        )
    elif a <= 0:
        reasons.append(
            f"{band}: grid x-axis is flipped (pixel width must be positive, got {a})"
        )
    return reasons


def _close(x: float, y: float) -> bool:
    return math.isclose(x, y, rel_tol=0.0, abs_tol=TOLERANCE_DEG)


def _grid_mismatch_reasons(b04: BandGrid, b08: BandGrid) -> list[str]:
    reasons: list[str] = []
    if (b04.width, b04.height) != (b08.width, b08.height):
        reasons.append(
            f"B04 and B08 grid dimensions differ: "
            f"B04 is {b04.width}x{b04.height}px, B08 is {b08.width}x{b08.height}px"
        )
    if any(not _close(x, y) for x, y in zip(b04.transform, b08.transform)):
        reasons.append(
            "B04 and B08 geotransforms differ beyond "
            f"{TOLERANCE_DEG:g} deg: B04={tuple(round(v, 12) for v in b04.transform)}, "
            f"B08={tuple(round(v, 12) for v in b08.transform)}"
        )
    return reasons


def _bounds_reasons(
    band: str, bounds: tuple, bbox: tuple[float, float, float, float]
) -> list[str]:
    reasons: list[str] = []
    for side, registered, actual in zip(("west", "south", "east", "north"), bbox, bounds):
        if not _close(registered, actual):
            reasons.append(
                f"registered {side}={registered:.12g} does not match {band} image "
                f"{side} bound {actual:.12g} (tolerance {TOLERANCE_DEG:g} deg)"
            )
    return reasons


def _run_checks(
    b04_path: Path, b08_path: Path, bbox: tuple[float, float, float, float]
) -> list[str]:
    """Return the list of mismatch reasons; empty list means the scene passes."""
    reasons: list[str] = []
    grids: dict[str, BandGrid] = {}
    structurally_ok: dict[str, bool] = {}
    for band, path in (("B04", b04_path), ("B08", b08_path)):
        try:
            grid = _inspect(path)
        except Exception as exc:
            reasons.append(f"{band}: cannot read GeoTIFF ({exc})")
            structurally_ok[band] = False
            continue
        grids[band] = grid
        band_reasons = _structural_reasons(band, grid)
        structurally_ok[band] = not band_reasons
        reasons.extend(band_reasons)

    # Cross-band and registered-rectangle checks are only meaningful when both
    # grids are on the supported EPSG:4326 north-up frame.
    if all(structurally_ok.get(b) for b in ("B04", "B08")):
        reasons.extend(_grid_mismatch_reasons(grids["B04"], grids["B08"]))
        for band in ("B04", "B08"):
            reasons.extend(_bounds_reasons(band, grids[band].bounds, bbox))
    return reasons


# --------------------------------------------------------------------------- #
# Background execution
# --------------------------------------------------------------------------- #
def _store_result(
    db_path: str, scene_id: str, revision: int, status: str, reasons: list[str]
) -> None:
    """Persist the verdict for exactly this (scene, revision).

    Keyed by revision, so a late result for an old revision can never approve
    a newer one. Any failure leaves the row ``pending``.
    """
    conn: sqlite3.Connection = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE qc_results SET status = ?, reasons = ?, finished_at = ? "
            "WHERE scene_id = ? AND revision = ?",
            (status, json.dumps(reasons), _utcnow(), scene_id, revision),
        )
        if cur.rowcount != 1:
            log.warning(
                "qc result for %s r%s matched %s rows", scene_id, revision, cur.rowcount
            )
        conn.commit()
    finally:
        conn.close()


def _worker(
    scene_id: str,
    revision: int,
    db_path: str,
    b04_path: Path,
    b08_path: Path,
    bbox: tuple[float, float, float, float],
) -> None:
    try:
        try:
            reasons = _run_checks(b04_path, b08_path, bbox)
        except Exception:
            # A crashed check is not a verdict: leave the revision pending.
            log.exception("qc run crashed for %s r%s; leaving pending", scene_id, revision)
            return
        status = STATUS_APPROVED if not reasons else STATUS_REJECTED
        try:
            _store_result(db_path, scene_id, revision, status, reasons)
        except Exception:
            log.exception(
                "qc result for %s r%s could not be stored; leaving pending",
                scene_id,
                revision,
            )
    finally:
        with _inflight_lock:
            _inflight.discard((scene_id, revision))


def launch(
    scene_id: str,
    revision: int,
    db_path: str,
    b04_path: Path,
    b08_path: Path,
    bbox: tuple[float, float, float, float],
) -> bool:
    """Start a background QC run; return False if one is already in flight."""
    key = (scene_id, revision)
    with _inflight_lock:
        if key in _inflight:
            return False
        _inflight.add(key)
    try:
        _executor.submit(_worker, scene_id, revision, db_path, b04_path, b08_path, bbox)
    except Exception:
        with _inflight_lock:
            _inflight.discard(key)
        raise
    return True
