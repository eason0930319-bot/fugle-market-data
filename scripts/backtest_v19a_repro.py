from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

VOLATILE_KEYS = {"generatedAt", "createdAt", "updatedAt", "fetchedAt", "runAt", "verifiedAt", "capturedAt"}
OUTPUT = Path("data/backtest-engine-state-v19a-summary.json")
CODE_PATHS = [
    Path("research/v19a-preregister.json"),
    Path("scripts/backtest_snapshot.py"),
    Path("scripts/backtest_data.py"),
    Path("scripts/backtest_ma5.py"),
    Path("scripts/backtest_ma5_stop.py"),
    Path("scripts/backtest_ma5_robustness.py"),
    Path("scripts/backtest_execution_v17.py"),
    Path("scripts/backtest_exit_v17b.py"),
    Path("scripts/backtest_allocation_v17c.py"),
    Path("scripts/backtest_engine_state_v19a.py"),
    Path("scripts/backtest_v19a_runner.py"),
    Path("scripts/backtest_v19a_repro.py"),
]


def _clean(x):
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in sorted(x.items()) if k not in VOLATILE_KEYS}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    return x


def _fp(path: Path):
    obj = json.loads(path.read_text(encoding="utf-8"))
    payload = json.dumps(_clean(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _code_hash():
    h = hashlib.sha256()
    for path in CODE_PATHS:
        if not path.exists():
            raise RuntimeError(f"missing V1.9A code path: {path}")
        h.update(path.as_posix().encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _manifest(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def capture(output: Path, freeze_manifest: Path):
    if not OUTPUT.exists():
        raise RuntimeError(f"missing V1.9A output: {OUTPUT}")
    fm = _manifest(freeze_manifest)
    r = {
        "schemaVersion": 1,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "freezeId": fm.get("freezeId"),
        "freezeDataSha256": fm.get("dataFileSha256"),
        "codeHash": _code_hash(),
        "summaryFingerprint": _fp(OUTPUT),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")


def compare(baseline: Path, report: Path, freeze_manifest: Path):
    base = json.loads(baseline.read_text(encoding="utf-8"))
    fm = _manifest(freeze_manifest)
    code = _code_hash()
    fp = _fp(OUTPUT)
    same_freeze = base.get("freezeId") == fm.get("freezeId") and base.get("freezeDataSha256") == fm.get("dataFileSha256")
    same_code = base.get("codeHash") == code
    same_output = base.get("summaryFingerprint") == fp
    ok = bool(same_freeze and same_code and same_output)
    r = {
        "ok": ok,
        "schemaVersion": 1,
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "freezeId": fm.get("freezeId"),
        "freezeDataSha256": fm.get("dataFileSha256"),
        "codeHash": code,
        "sameFreeze": same_freeze,
        "sameCode": same_code,
        "deterministicAcrossTwoConsecutiveRuns": ok,
        "mismatchedOutputs": [] if same_output else [str(OUTPUT)],
        "baselineFingerprint": base.get("summaryFingerprint"),
        "secondRunFingerprint": fp,
        "method": "SHA256 of canonical V1.9A JSON after removing volatile timestamps; both runs use the same immutable market-data freeze and preregistered rules."
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    if not ok:
        raise SystemExit(2)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--output", required=True)
    c.add_argument("--freeze-manifest", required=True)
    x = sub.add_parser("compare")
    x.add_argument("--baseline", required=True)
    x.add_argument("--report", required=True)
    x.add_argument("--freeze-manifest", required=True)
    a = p.parse_args()
    if a.cmd == "capture":
        capture(Path(a.output), Path(a.freeze_manifest))
    else:
        compare(Path(a.baseline), Path(a.report), Path(a.freeze_manifest))


if __name__ == "__main__":
    main()
