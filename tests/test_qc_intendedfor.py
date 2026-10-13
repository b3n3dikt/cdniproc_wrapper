"""Tests for helpers/qc_intendedfor.py on small synthetic sessions.   Run:  python3 -m unittest discover -s tests -v"""
import csv
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HELPERS = Path(__file__).resolve().parent.parent / "helpers"
SUB = "T01"
SHIM = [-1385, -7532, -1289, 479, -32, -101, 169, 52]


def wr(base, meta):
    base.parent.mkdir(parents=True, exist_ok=True)
    Path(str(base) + ".json").write_text(json.dumps(meta))
    Path(str(base) + ".nii.gz").write_bytes(b"")


def make_session(root, ses, fmaps, runs, func_pe="j", orig=None, shim=SHIM):
    """fmaps: [(dir, runnum, 'HH:MM:SS', pe)]; runs: [(runnum, 'HH:MM:SS')]"""
    d = root / f"sub-{SUB}" / f"ses-{ses}"
    for dr, n, t, pe in fmaps:
        m = {"AcquisitionTime": t, "ShimSetting": shim, "PhaseEncodingDirection": pe, "TotalReadoutTime": 0.03, "EchoNumber": 1}
        if orig:
            m["OriginalSession"] = f"ses-{orig}"
        wr(d / "fmap" / f"sub-{SUB}_ses-{ses}_dir-{dr}_run-{n:02d}_epi", m)
    for n, t in runs:
        for e in (1, 2):
            m = {"AcquisitionTime": t, "ShimSetting": shim, "PhaseEncodingDirection": func_pe, "TotalReadoutTime": 0.03, "EchoNumber": e}
            if orig:
                m["OriginalSession"] = f"ses-{orig}"
            wr(d / "func" / f"sub-{SUB}_ses-{ses}_task-rest_run-{n:02d}_echo-{e}_bold", m)


FMAPS = [("AP", 1, "09:51:57", "j-"), ("PA", 1, "09:53:23", "j"), ("AP", 2, "10:32:08", "j-"), ("PA", 2, "10:33:36", "j")]
RUNS = [(1, "09:57:00"), (2, "10:07:00"), (3, "10:37:00"), (4, "10:47:00"), (5, "10:57:00")]


def jsbr_write(root, mode, ses=None):
    cmd = [sys.executable, str(HELPERS / "IntendedFor_JSBR.py"), str(root), "--sub", f"sub-{SUB}", "--assign-mode", mode, "--write"]
    if ses:
        cmd += ["--ses", f"ses-{ses}"]
    subprocess.run(cmd, check=True, capture_output=True)


def qc(root, mode="auto", syn=False):
    out = root.parent / "qc"
    cmd = [sys.executable, str(HELPERS / "qc_intendedfor.py"), str(root), "--sub", SUB, "--out-dir", str(out), "--mode", mode]
    if syn:
        cmd.append("--syn-sdc")
    p = subprocess.run(cmd, capture_output=True, text=True)
    rep = (out / f"bids_setup_sub-{SUB}_{root.name}.md").read_text()
    return p.returncode, rep, out


class QC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "bids"

    def tearDown(self):
        self.tmp.cleanup()

    def test_lookforward_maps_each_run_to_the_pair_before_it(self):
        make_session(self.root, "A", FMAPS, RUNS, func_pe="j")
        jsbr_write(self.root, "lookforward")
        rc, rep, out = qc(self.root, "lookforward")
        self.assertEqual(rc, 0, rep)
        self.assertNotIn("not used by any run", rep)
        with open(out / f"intendedfor_sub-{SUB}_bids.tsv") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))
        pairs = {re.search(r"run-\d+", r["run"]).group(0): r["mapped_pairs"] for r in rows}
        self.assertEqual(sorted(pairs), ["run-01", "run-02", "run-03", "run-04", "run-05"])
        self.assertEqual(pairs["run-01"], pairs["run-02"])
        self.assertEqual(pairs["run-03"], pairs["run-05"])
        self.assertNotEqual(pairs["run-01"], pairs["run-03"])

    def test_auto_leaves_a_pair_unused_and_is_reported(self):
        make_session(self.root, "A", FMAPS, RUNS, func_pe="j")
        jsbr_write(self.root, "auto")          # the failure seen on real data: every run goes to the second pair
        rc, rep, _ = qc(self.root, "auto")
        self.assertEqual(rc, 0)
        self.assertIn("not used by any run", rep)
        self.assertIn("disagrees with", rep)

    def test_no_fieldmap_is_warn_with_syn_sdc_and_fail_without(self):
        make_session(self.root, "A", [], RUNS)
        rc, rep, _ = qc(self.root, syn=True)
        self.assertEqual(rc, 0, rep)
        self.assertIn("SyN-SDC only", rep)
        rc, rep, _ = qc(self.root, syn=False)
        self.assertEqual(rc, 2, rep)

    def test_fieldmaps_present_but_not_mapped_is_fail(self):
        make_session(self.root, "A", FMAPS, RUNS)       # IntendedFor never written
        rc, rep, _ = qc(self.root, syn=True)
        self.assertEqual(rc, 2, rep)
        self.assertIn("none is mapped to this run", rep)

    def test_fieldmap_mapped_across_sessions_is_fail(self):
        make_session(self.root, "combined", FMAPS[:2], RUNS[:1], orig="S1")
        d = self.root / f"sub-{SUB}" / "ses-combined"
        wr(d / "func" / f"sub-{SUB}_ses-combined_task-rest_run-09_echo-1_bold",
           {"AcquisitionTime": "10:00:00", "ShimSetting": SHIM, "PhaseEncodingDirection": "j", "OriginalSession": "ses-S2"})
        for f in (d / "fmap").glob("*.json"):
            m = json.loads(f.read_text())
            m["IntendedFor"] = [f"ses-combined/func/sub-{SUB}_ses-combined_task-rest_run-09_echo-1_bold.nii.gz"]
            f.write_text(json.dumps(m))
        rc, rep, _ = qc(self.root, syn=True)
        self.assertEqual(rc, 2, rep)
        self.assertIn("another session", rep)


if __name__ == "__main__":
    unittest.main()
