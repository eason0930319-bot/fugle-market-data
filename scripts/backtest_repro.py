from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

OUTPUTS = [
    "data/backtest-summary.json",
    "data/backtest-signal-sample.json",
    "data/backtest-execution-summary.json",
    "data/backtest-execution-sample.json",
    "data/backtest-portfolio-summary.json",
    "data/backtest-ranking-summary.json",
    "data/backtest-ma5-summary.json",
    "data/backtest-ma5-stop-summary.json",
    "data/backtest-ma5-robustness-summary.json",
    "data/backtest-ma5-regime-reentry-summary.json",
    "data/backtest-execution-v17-summary.json",
]
VOLATILE_KEYS = {"generatedAt", "createdAt", "updatedAt", "fetchedAt", "runAt"}


def _clean(x):
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in sorted(x.items()) if k not in VOLATILE_KEYS}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    return x


def _json_fingerprint(path: Path):
    obj = json.loads(path.read_text(encoding="utf-8"))
    payload = json.dumps(_clean(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _code_hash():
    h = hashlib.sha256()
    paths = sorted(Path("scripts").glob("backtest_*.py")) + [Path("scripts/historical_backtest.py")]
    for path in paths:
        if not path.exists():
            continue
        h.update(path.as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def capture(output: Path, freeze_manifest: Path):
    fm = json.loads(freeze_manifest.read_text(encoding="utf-8"))
    missing = [p for p in OUTPUTS if not Path(p).exists()]
    if missing:
        raise RuntimeError(f"missing backtest outputs: {missing}")
    report = {
        "schemaVersion": 1,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "freezeId": fm.get("freezeId"),
        "freezeDataSha256": fm.get("dataFileSha256"),
        "codeHash": _code_hash(),
        "fingerprints": {p: _json_fingerprint(Path(p)) for p in OUTPUTS},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "mode": "capture", "freezeId": report["freezeId"], "codeHash": report["codeHash"]}, ensure_ascii=False))


def compare(baseline: Path, report_path: Path, freeze_manifest: Path):
    base = json.loads(baseline.read_text(encoding="utf-8"))
    fm = json.loads(freeze_manifest.read_text(encoding="utf-8"))
    current = {p: _json_fingerprint(Path(p)) for p in OUTPUTS}
    current_code = _code_hash()
    mismatches = []
    for p in OUTPUTS:
        if base.get("fingerprints", {}).get(p) != current.get(p):
            mismatches.append(p)

    same_freeze = base.get("freezeId") == fm.get("freezeId") and base.get("freezeDataSha256") == fm.get("dataFileSha256")
    same_code = base.get("codeHash") == current_code
    deterministic = bool(same_freeze and same_code and not mismatches)
    report = {
        "ok": deterministic,
        "schemaVersion": 1,
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "freezeId": fm.get("freezeId"),
        "freezeDataSha256": fm.get("dataFileSha256"),
        "codeHash": current_code,
        "sameFreeze": same_freeze,
        "sameCode": same_code,
        "deterministicAcrossTwoConsecutiveRuns": deterministic,
        "mismatchedOutputs": mismatches,
        "baselineFingerprints": base.get("fingerprints", {}),
        "secondRunFingerprints": current,
        "method": "SHA256 of canonical JSON after removing volatile timestamp fields; both runs use the same immutable market-data snapshot.",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": deterministic, "sameFreeze": same_freeze, "sameCode": same_code, "mismatches": mismatches}, ensure_ascii=False))
    if not deterministic:
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

    args = p.parse_args()
    if args.cmd == "capture":
        capture(Path(args.output), Path(args.freeze_manifest))
    else:
        compare(Path(args.baseline), Path(args.report), Path(args.freeze_manifest))


if __name__ == "__main__":
    main()
