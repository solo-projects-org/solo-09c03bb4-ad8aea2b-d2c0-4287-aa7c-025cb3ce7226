"""End-to-end tests for the spatial QC API and the /scenes QC gate."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from affine import Affine
from fastapi.testclient import TestClient
from rasterio.crs import CRS
from rasterio.io import MemoryFile

ALIGNED_BBOX = (116.0, 39.0, 117.0, 40.0)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.db"
    storage_dir = tmp_path / "storage"
    monkeypatch.setenv("CATALOG_DB_PATH", str(db_path))
    monkeypatch.setenv("CATALOG_STORAGE_DIR", str(storage_dir))
    from app import config
    config.get_settings.cache_clear()

    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def blocked_qc(monkeypatch):
    """Replace the QC check with one that blocks until the test releases it."""
    from app import qc
    entered = threading.Event()
    release = threading.Event()
    real_run_checks = qc._run_checks

    def blocking(b04_path, b08_path, bbox):
        entered.set()
        assert release.wait(10), "test finished without releasing the QC run"
        return real_run_checks(b04_path, b08_path, bbox)

    monkeypatch.setattr(qc, "_run_checks", blocking)
    return SimpleNamespace(entered=entered, release=release)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def make_tiff(*, west=116.0, south=39.0, east=117.0, north=40.0, width=16, height=16,
              epsg=4326, south_up=False, rotated=False, bands=1, fill=4) -> bytes:
    """Build an in-memory single-band GeoTIFF on an EPSG:4326 north-up grid."""
    resx = (east - west) / width
    resy = (north - south) / height
    if rotated:
        transform = Affine(resx, 0.3 * resx, west, 0.3 * resy, -resy, north)
    elif south_up:
        transform = Affine(resx, 0, west, 0, resy, south)
    else:
        transform = Affine(resx, 0, west, 0, -resy, north)
    data = np.full((bands, height, width), fill, dtype="uint8")
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", width=width, height=height, count=bands,
                      dtype="uint8", crs=CRS.from_epsg(epsg), transform=transform) as ds:
            ds.write(data)
        return mem.read()


def aligned_pair(**kw) -> tuple[bytes, bytes]:
    """B04/B08 GeoTIFFs on the same grid matching ALIGNED_BBOX (distinct bytes)."""
    return make_tiff(fill=4, **kw), make_tiff(fill=8, **kw)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def submit(client, scene_id="S1", revision=1, *, b04, b08, bbox=ALIGNED_BBOX,
           acquisition_time="2026-06-01T10:00:00Z"):
    west, south, east, north = bbox
    return client.post(
        f"/scenes/{scene_id}/revisions",
        data={
            "revision": str(revision),
            "acquisition_time": acquisition_time,
            "west": str(west), "south": str(south),
            "east": str(east), "north": str(north),
            "sha256_b04": _sha(b04), "sha256_b08": _sha(b08),
        },
        files={
            "file_b04": ("b04.tif", b04, "image/tiff"),
            "file_b08": ("b08.tif", b08, "image/tiff"),
        },
    )


def wait_verdict(client, scene_id, timeout=10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        r = client.get(f"/scenes/{scene_id}/qc")
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] != "pending":
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"qc for {scene_id} still pending after {timeout}s")
        time.sleep(0.02)


def wait_db_status(db_path, scene_id, revision, want, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT status FROM qc_results WHERE scene_id = ? AND revision = ?",
                (scene_id, revision),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0] == want:
            return
        time.sleep(0.02)
    raise AssertionError(f"qc_results({scene_id}, {revision}) never became {want}")


def scene_ids(client) -> list[str]:
    return [s["scene_id"] for s in client.get("/scenes").json()]


# --------------------------------------------------------------------------- #
# Happy path and gating
# --------------------------------------------------------------------------- #
def test_qc_approved_aligned_scene(client):
    b04, b08 = aligned_pair()
    assert submit(client, b04=b04, b08=b08).status_code == 201
    r = client.post("/scenes/S1/qc")
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "pending"
    assert r.json()["detail"] == "qc started"
    assert r.json()["revision"] == 1

    body = wait_verdict(client, "S1")
    assert body["status"] == "approved"
    assert body["reasons"] == []
    assert body["started_at"] and body["finished_at"]
    assert scene_ids(client) == ["S1"]


def test_qc_pending_hides_scene_until_verdict(client, blocked_qc):
    b04, b08 = aligned_pair()
    assert submit(client, b04=b04, b08=b08).status_code == 201
    assert scene_ids(client) == ["S1"]  # never QC'd: listed as before

    assert client.post("/scenes/S1/qc").status_code == 202
    assert blocked_qc.entered.wait(10)
    # gate active + verdict pending -> hidden while the check runs
    assert scene_ids(client) == []

    blocked_qc.release.set()
    assert wait_verdict(client, "S1")["status"] == "approved"
    assert scene_ids(client) == ["S1"]


def test_qc_rejected_misaligned_rectangle(client):
    b04, b08 = aligned_pair()  # image bounds are ALIGNED_BBOX
    # registered rectangle shifted 0.5 deg east: west/east mismatch
    assert submit(client, b04=b04, b08=b08,
                  bbox=(116.5, 39.0, 117.5, 40.0)).status_code == 201
    assert client.post("/scenes/S1/qc").status_code == 202
    body = wait_verdict(client, "S1")
    assert body["status"] == "rejected"
    text = " ".join(body["reasons"])
    assert "west" in text and "east" in text
    assert "B04" in text and "B08" in text
    assert "north" not in text  # south/north do match
    # rejected current revision -> not retrievable ...
    assert scene_ids(client) == []
    # ... but its files remain downloadable
    dl = client.get("/scenes/S1/revisions/1/bands/B04/download")
    assert dl.status_code == 200
    assert dl.content == b04


def test_qc_rejected_when_band_grids_differ(client):
    b04 = make_tiff(fill=4, width=16, height=16)
    b08 = make_tiff(fill=8, width=8, height=8)  # same bbox, coarser grid
    assert submit(client, b04=b04, b08=b08).status_code == 201
    client.post("/scenes/S1/qc")
    body = wait_verdict(client, "S1")
    assert body["status"] == "rejected"
    text = " ".join(body["reasons"])
    assert "dimensions differ" in text
    assert "geotransforms differ" in text
    assert scene_ids(client) == []


def test_qc_rejected_for_non_4326_crs(client):
    b04, b08 = aligned_pair(epsg=32633)
    assert submit(client, b04=b04, b08=b08).status_code == 201
    client.post("/scenes/S1/qc")
    body = wait_verdict(client, "S1")
    assert body["status"] == "rejected"
    assert any("EPSG:4326" in r and "32633" in r for r in body["reasons"])
    assert scene_ids(client) == []


def test_qc_rejected_for_south_up_grid(client):
    b04 = make_tiff(fill=4, south_up=True)
    b08 = make_tiff(fill=8)
    assert submit(client, b04=b04, b08=b08).status_code == 201
    client.post("/scenes/S1/qc")
    body = wait_verdict(client, "S1")
    assert body["status"] == "rejected"
    assert any("not north-up" in r for r in body["reasons"])


def test_qc_rejected_for_rotated_grid(client):
    b04 = make_tiff(fill=4, rotated=True)
    b08 = make_tiff(fill=8)
    assert submit(client, b04=b04, b08=b08).status_code == 201
    client.post("/scenes/S1/qc")
    body = wait_verdict(client, "S1")
    assert body["status"] == "rejected"
    assert any("rotation/shear" in r for r in body["reasons"])


def test_qc_rejected_for_multiband_file(client):
    b04 = make_tiff(fill=4, bands=3)
    b08 = make_tiff(fill=8)
    assert submit(client, b04=b04, b08=b08).status_code == 201
    client.post("/scenes/S1/qc")
    body = wait_verdict(client, "S1")
    assert body["status"] == "rejected"
    assert any("single-band" in r for r in body["reasons"])


def test_qc_refused_for_plain_files(client):
    assert submit(client, b04=b"red-band", b08=b"nir-band").status_code == 201
    r = client.post("/scenes/S1/qc")
    assert r.status_code == 422
    assert "GeoTIFF" in r.json()["detail"]
    # refusal did not start QC: no gate, scene keeps its previous behavior
    assert client.get("/scenes/S1/qc").status_code == 404
    assert scene_ids(client) == ["S1"]


def test_qc_endpoints_404(client):
    assert client.get("/scenes/NOPE/qc").status_code == 404
    assert client.post("/scenes/NOPE/qc").status_code == 404
    b04, b08 = aligned_pair()
    assert submit(client, b04=b04, b08=b08).status_code == 201
    # scene exists but QC was never started
    assert client.get("/scenes/S1/qc").status_code == 404


# --------------------------------------------------------------------------- #
# Revision interleaving
# --------------------------------------------------------------------------- #
def test_new_revision_reverts_to_pending(client):
    b04, b08 = aligned_pair()
    assert submit(client, revision=1, b04=b04, b08=b08).status_code == 201
    client.post("/scenes/S1/qc")
    assert wait_verdict(client, "S1")["status"] == "approved"
    assert scene_ids(client) == ["S1"]

    # a new revision sends the scene back to pending
    b04v2, b08v2 = make_tiff(fill=44), make_tiff(fill=88)
    r = submit(client, revision=2, b04=b04v2, b08=b08v2,
               acquisition_time="2026-06-02T10:00:00Z")
    assert r.status_code == 201
    qc_body = client.get("/scenes/S1/qc").json()
    assert qc_body["revision"] == 2
    assert qc_body["status"] == "pending"
    assert scene_ids(client) == []
    # superseded revision still downloads fine
    assert client.get("/scenes/S1/revisions/1/bands/B04/download").status_code == 200

    # QC the new revision -> approved -> listed again
    assert client.post("/scenes/S1/qc").status_code == 202
    assert wait_verdict(client, "S1")["status"] == "approved"
    assert scene_ids(client) == ["S1"]


def test_late_result_from_old_revision_does_not_approve_new(client, tmp_path, blocked_qc):
    b04, b08 = aligned_pair()
    assert submit(client, revision=1, b04=b04, b08=b08).status_code == 201
    assert client.post("/scenes/S1/qc").status_code == 202
    assert blocked_qc.entered.wait(10)  # rev1 check is now in flight

    # while it runs, a new revision arrives -> current revision is pending
    b04v2, b08v2 = make_tiff(fill=44), make_tiff(fill=88)
    assert submit(client, revision=2, b04=b04v2, b08=b08v2,
                  acquisition_time="2026-06-02T10:00:00Z").status_code == 201
    assert client.get("/scenes/S1/qc").json()["revision"] == 2
    assert client.get("/scenes/S1/qc").json()["status"] == "pending"

    blocked_qc.release.set()
    # the in-flight run finishes and stores its verdict for REVISION 1 only
    wait_db_status(tmp_path / "catalog.db", "S1", 1, "approved")
    # the late approval of rev1 must not approve rev2: scene stays hidden
    assert client.get("/scenes/S1/qc").json()["status"] == "pending"
    assert scene_ids(client) == []

    # explicitly checking rev2 (release is already set) approves and lists it
    assert client.post("/scenes/S1/qc").status_code == 202
    assert wait_verdict(client, "S1")["status"] == "approved"
    assert scene_ids(client) == ["S1"]


def test_other_scenes_usable_during_slow_qc(client, blocked_qc):
    b04, b08 = aligned_pair()
    assert submit(client, scene_id="A", b04=b04, b08=b08).status_code == 201
    assert client.post("/scenes/A/qc").status_code == 202
    assert blocked_qc.entered.wait(10)  # A's check is stuck in the background

    # other scenes can still be submitted and queried meanwhile
    assert submit(client, scene_id="B", b04=b"plain-red", b08=b"plain-nir").status_code == 201
    assert scene_ids(client) == ["B"]  # A hidden (pending), B ungated

    blocked_qc.release.set()
    assert wait_verdict(client, "A")["status"] == "approved"
    assert sorted(scene_ids(client)) == ["A", "B"]


def test_idempotent_replay_keeps_qc_state(client):
    b04, b08 = aligned_pair()
    assert submit(client, b04=b04, b08=b08).status_code == 201
    client.post("/scenes/S1/qc")
    assert wait_verdict(client, "S1")["status"] == "approved"

    r = submit(client, b04=b04, b08=b08)  # identical resubmission
    assert r.status_code == 200
    assert r.json()["submission_status"] == "idempotent_replay"
    assert client.get("/scenes/S1/qc").json()["status"] == "approved"
    assert scene_ids(client) == ["S1"]


def test_qc_can_be_rerun_after_verdict(client):
    b04, b08 = aligned_pair()
    assert submit(client, b04=b04, b08=b08).status_code == 201
    client.post("/scenes/S1/qc")
    assert wait_verdict(client, "S1")["status"] == "approved"
    # re-running resets to pending and reaches the same verdict
    assert client.post("/scenes/S1/qc").status_code == 202
    body = wait_verdict(client, "S1")
    assert body["status"] == "approved"
    assert scene_ids(client) == ["S1"]


# --------------------------------------------------------------------------- #
# Failure semantics
# --------------------------------------------------------------------------- #
def test_failed_result_store_leaves_pending(client, monkeypatch):
    from app import qc
    attempted = threading.Event()

    def boom(*args, **kwargs):
        attempted.set()
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(qc, "_store_result", boom)
    b04, b08 = aligned_pair()
    assert submit(client, b04=b04, b08=b08).status_code == 201
    assert client.post("/scenes/S1/qc").status_code == 202
    assert attempted.wait(10)
    # verdict could not be stored -> still pending, still hidden
    assert client.get("/scenes/S1/qc").json()["status"] == "pending"
    assert scene_ids(client) == []


def test_pending_qc_state_survives_restart(client, tmp_path, blocked_qc):
    b04, b08 = aligned_pair()
    assert submit(client, b04=b04, b08=b08).status_code == 201
    assert client.post("/scenes/S1/qc").status_code == 202
    assert blocked_qc.entered.wait(10)

    # simulate a service restart against the same on-disk state
    from app import config
    config.get_settings.cache_clear()
    from app.main import app
    with TestClient(app) as second:
        # the interrupted run left the revision pending and the scene hidden
        assert second.get("/scenes/S1/qc").json()["status"] == "pending"
        assert scene_ids(second) == []
        blocked_qc.release.set()
        assert wait_verdict(second, "S1")["status"] == "approved"
        assert scene_ids(second) == ["S1"]
