#!/usr/bin/env python
"""Live end-to-end smoke test of the spatial QC API against a running server.

Usage: python qc_e2e.py <base_url> <work_dir> <db_path> <phase>
  phase 1: full flow (misalignment, grid mismatch, plain files, revision
           interleaving, late results)
  phase 2: after a server restart, verify persisted QC state
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.io import MemoryFile

BASE, WORK, DB, PHASE = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4]
FILES = WORK / "files"
FILES.mkdir(parents=True, exist_ok=True)
PASS = FAIL = 0


def check(desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS: {desc}")
    else:
        FAIL += 1
        print(f"FAIL: {desc} {extra}")


def make_tiff(path, *, west=116.0, south=39.0, east=117.0, north=40.0, width=16,
              height=16, fill=4):
    resx, resy = (east - west) / width, (north - south) / height
    transform = Affine(resx, 0, west, 0, -resy, north)
    data = np.full((1, height, width), fill, dtype="uint8")
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", width=width, height=height, count=1,
                      dtype="uint8", crs=CRS.from_epsg(4326), transform=transform) as ds:
            ds.write(data)
        path.write_bytes(mem.read())
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def submit(scene, rev, f04, f08, bbox=(116.0, 39.0, 117.0, 40.0),
           atime="2026-06-01T10:00:00Z"):
    w, s, e, n = bbox
    return httpx.post(
        f"{BASE}/scenes/{scene}/revisions",
        data={"revision": str(rev), "acquisition_time": atime,
              "west": str(w), "south": str(s), "east": str(e), "north": str(n),
              "sha256_b04": sha(f04), "sha256_b08": sha(f08)},
        files={"file_b04": ("b04.tif", f04.read_bytes(), "image/tiff"),
               "file_b08": ("b08.tif", f08.read_bytes(), "image/tiff")},
        timeout=30,
    )


def ids():
    return sorted(s["scene_id"] for s in httpx.get(f"{BASE}/scenes", timeout=10).json())


def wait_qc(scene, timeout=15.0):
    deadline = time.monotonic() + timeout
    while True:
        body = httpx.get(f"{BASE}/scenes/{scene}/qc", timeout=10).json()
        if body["status"] != "pending":
            return body
        if time.monotonic() > deadline:
            raise SystemExit(f"qc for {scene} never finished")
        time.sleep(0.1)


def db_status(scene, rev):
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute(
            "SELECT status FROM qc_results WHERE scene_id=? AND revision=?",
            (scene, rev)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


if PHASE == "1":
    # -- fixtures ---------------------------------------------------------- #
    ok04 = make_tiff(FILES / "ok_b04.tif", fill=4)
    ok08 = make_tiff(FILES / "ok_b08.tif", fill=8)
    v2_04 = make_tiff(FILES / "v2_b04.tif", fill=44)
    v2_08 = make_tiff(FILES / "v2_b08.tif", fill=88)
    coarse08 = make_tiff(FILES / "coarse_b08.tif", width=8, height=8, fill=8)
    plain04, plain08 = FILES / "plain_b04.bin", FILES / "plain_b08.bin"
    plain04.write_bytes(b"red-band")
    plain08.write_bytes(b"nir-band")

    print("== swagger ==")
    check("GET /docs 200", httpx.get(f"{BASE}/docs", timeout=10).status_code == 200)
    spec = httpx.get(f"{BASE}/openapi.json", timeout=10).json()
    check("openapi has qc endpoints",
          "/scenes/{scene_id}/qc" in spec["paths"])

    print("== aligned scene: approve ==")
    check("submit SCENE_OK 201", submit("SCENE_OK", 1, ok04, ok08).status_code == 201)
    check("ungated scene listed", ids() == ["SCENE_OK"])
    r = httpx.post(f"{BASE}/scenes/SCENE_OK/qc", timeout=10)
    check("POST qc -> 202 pending", r.status_code == 202 and r.json()["status"] == "pending", r.text)
    body = wait_qc("SCENE_OK")
    check("SCENE_OK approved", body["status"] == "approved" and body["reasons"] == [], json.dumps(body))
    check("approved scene listed", ids() == ["SCENE_OK"])

    print("== misaligned registered rectangle: reject ==")
    check("submit SCENE_SHIFT 201",
          submit("SCENE_SHIFT", 1, ok04, ok08, bbox=(116.5, 39.0, 117.5, 40.0)).status_code == 201)
    httpx.post(f"{BASE}/scenes/SCENE_SHIFT/qc", timeout=10)
    body = wait_qc("SCENE_SHIFT")
    txt = " ".join(body["reasons"])
    check("SCENE_SHIFT rejected", body["status"] == "rejected", json.dumps(body))
    check("reasons name west/east mismatch", "west" in txt and "east" in txt, txt)
    check("rejected scene hidden", ids() == ["SCENE_OK"], ids())
    dl = httpx.get(f"{BASE}/scenes/SCENE_SHIFT/revisions/1/bands/B04/download", timeout=10)
    check("rejected revision still downloadable", dl.status_code == 200 and dl.content == ok04.read_bytes())

    print("== band grids differ: reject ==")
    check("submit SCENE_GRID 201", submit("SCENE_GRID", 1, ok04, coarse08).status_code == 201)
    httpx.post(f"{BASE}/scenes/SCENE_GRID/qc", timeout=10)
    body = wait_qc("SCENE_GRID")
    check("SCENE_GRID rejected with grid reason",
          body["status"] == "rejected" and "dimensions differ" in " ".join(body["reasons"]),
          json.dumps(body))

    print("== plain files: refuse to start ==")
    check("submit SCENE_PLAIN 201", submit("SCENE_PLAIN", 1, plain04, plain08).status_code == 201)
    r = httpx.post(f"{BASE}/scenes/SCENE_PLAIN/qc", timeout=10)
    check("plain files -> 422", r.status_code == 422, r.text)
    check("gate not activated (still listed)", "SCENE_PLAIN" in ids(), ids())
    check("no qc record", httpx.get(f"{BASE}/scenes/SCENE_PLAIN/qc", timeout=10).status_code == 404)

    print("== revision interleaving ==")
    check("submit SCENE_EVO r1 201", submit("SCENE_EVO", 1, ok04, ok08).status_code == 201)
    httpx.post(f"{BASE}/scenes/SCENE_EVO/qc", timeout=10)
    check("SCENE_EVO r1 approved", wait_qc("SCENE_EVO")["status"] == "approved")
    check("SCENE_EVO listed", "SCENE_EVO" in ids(), ids())
    check("submit SCENE_EVO r2 201",
          submit("SCENE_EVO", 2, v2_04, v2_08, atime="2026-06-02T10:00:00Z").status_code == 201)
    body = httpx.get(f"{BASE}/scenes/SCENE_EVO/qc", timeout=10).json()
    check("new revision back to pending", body["revision"] == 2 and body["status"] == "pending", json.dumps(body))
    check("pending current revision hidden", "SCENE_EVO" not in ids(), ids())
    dl = httpx.get(f"{BASE}/scenes/SCENE_EVO/revisions/1/bands/B08/download", timeout=10)
    check("superseded revision downloadable", dl.status_code == 200 and dl.content == ok08.read_bytes())
    httpx.post(f"{BASE}/scenes/SCENE_EVO/qc", timeout=10)
    check("SCENE_EVO r2 approved", wait_qc("SCENE_EVO")["status"] == "approved")
    check("SCENE_EVO listed again", "SCENE_EVO" in ids(), ids())

    print("== late result of old revision must not approve the new one ==")
    check("submit SCENE_LATE r1 201", submit("SCENE_LATE", 1, ok04, ok08).status_code == 201)
    httpx.post(f"{BASE}/scenes/SCENE_LATE/qc", timeout=10)
    check("submit SCENE_LATE r2 201 (racing the qc run)",
          submit("SCENE_LATE", 2, v2_04, v2_08, atime="2026-06-02T10:00:00Z").status_code == 201)
    # wait until the r1 verdict lands in the db (keyed to revision 1)
    deadline = time.monotonic() + 15
    while db_status("SCENE_LATE", 1) != "approved" and time.monotonic() < deadline:
        time.sleep(0.1)
    check("r1 verdict stored as approved", db_status("SCENE_LATE", 1) == "approved", str(db_status("SCENE_LATE", 1)))
    check("r2 still pending", db_status("SCENE_LATE", 2) == "pending", str(db_status("SCENE_LATE", 2)))
    check("late r1 approval did not list the scene", "SCENE_LATE" not in ids(), ids())
    httpx.post(f"{BASE}/scenes/SCENE_LATE/qc", timeout=10)
    check("SCENE_LATE r2 approved after its own qc", wait_qc("SCENE_LATE")["status"] == "approved")
    check("SCENE_LATE listed", "SCENE_LATE" in ids(), ids())

else:
    print("== after restart: qc state persisted ==")
    body = httpx.get(f"{BASE}/scenes/SCENE_OK/qc", timeout=10).json()
    check("SCENE_OK still approved", body["status"] == "approved", json.dumps(body))
    body = httpx.get(f"{BASE}/scenes/SCENE_SHIFT/qc", timeout=10).json()
    check("SCENE_SHIFT still rejected with reasons",
          body["status"] == "rejected" and len(body["reasons"]) > 0, json.dumps(body))
    listed = ids()
    check("SCENE_OK listed", "SCENE_OK" in listed, listed)
    check("SCENE_SHIFT hidden", "SCENE_SHIFT" not in listed, listed)
    check("SCENE_GRID hidden", "SCENE_GRID" not in listed, listed)
    check("SCENE_PLAIN still ungated/listed", "SCENE_PLAIN" in listed, listed)
    check("SCENE_EVO listed", "SCENE_EVO" in listed, listed)
    check("SCENE_LATE listed", "SCENE_LATE" in listed, listed)
    dl = httpx.get(f"{BASE}/scenes/SCENE_EVO/revisions/1/bands/B04/download", timeout=10)
    check("old revision download survives restart", dl.status_code == 200)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
