"""Verify frozen reserve provenance and complete runs; score without new labels.

Single-mode runs and combined runs are supported. Comparisons are exploratory;
different reserve slices cannot establish a paired improvement over each other.
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.prepare_public_sufficiency_pilot import select_reserve
from benchmarks import sufficiency_certificate as certificate_protocol
from benchmarks.run_public_sufficiency import (
    VERSION, compare, digest, file_sha256, load, parse_output, score, write_new,
)


def verify_run(run, input_path, inputs):
    manifest = load(run / "manifest.json")
    receipt = load(run / "complete.json")
    modes = manifest["modes"]
    if (manifest["version"] != VERSION or not modes or len(set(modes)) != len(modes)
            or set(modes) - {"direct", "chain", "chain_strict", "chain_balanced", "chain_checklist", "chain_guard", "certificate"}
            or set(manifest["prompts"]) != set(modes)
            or manifest["inputs_sha256"] != file_sha256(input_path)
            or not manifest.get("model_files") or not manifest.get("script_sha256")
            or manifest.get("no_truncation") is not True):
        raise ValueError("Invalid inference manifest")
    rows = [json.loads(line) for line in (run / "predictions.jsonl").read_text(encoding="utf-8").splitlines()]
    if (len(rows) != len(inputs) * len(modes) or receipt["rows"] != len(rows)
            or receipt["run_id"] != digest(manifest)
            or receipt["predictions_sha256"] != file_sha256(run / "predictions.jsonl")):
        raise ValueError("Incomplete or changed receipt")
    lookup = {item["id"]: item for item in inputs}
    arms = {mode: {} for mode in modes}
    for row in rows:
        mode, ident = row["mode"], row["id"]
        if mode not in arms or ident not in lookup or ident in arms[mode]:
            raise ValueError("Duplicate or foreign prediction")
        if row["run_id"] != digest(manifest) or row["input_sha256"] != digest(lookup[ident]):
            raise ValueError("Prediction fingerprint mismatch")
        if row["status"] not in {"ok", "input_overflow", "output_limit", "invalid_output", "oom"}:
            raise ValueError("Unknown status")
        if mode == "certificate":
            protocol = manifest["certificate_protocol"]
            if (protocol["sha256"] != file_sha256(certificate_protocol.__file__)
                    or protocol["proposer"] != certificate_protocol.PROPOSER
                    or protocol["verifier"] != certificate_protocol.VERIFIER
                    or protocol["limits"] != certificate_protocol.LIMITS):
                raise ValueError("Certificate protocol differs")
            for stage in row["stages"]:
                if stage["status"] not in {"generated", "input_overflow", "output_limit", "oom"}:
                    raise ValueError("Invalid stage status")
                if stage["name"] not in protocol["limits"]:
                    raise ValueError("Unknown certificate stage")
                if (type(stage["input_tokens"]) is not int or stage["input_tokens"] < 0
                        or type(stage.get("output_tokens", 0)) is not int
                        or not 0 <= stage.get("output_tokens", 0) <= protocol["limits"][stage["name"]]
                        or not isinstance(stage["seconds"], (int, float))
                        or not math.isfinite(stage["seconds"]) or stage["seconds"] < 0):
                    raise ValueError("Invalid stage counters")
                if stage["status"] == "generated" and stage["input_tokens"] > manifest["max_input_tokens"]:
                    raise ValueError("Stage overflow")
            if (row["input_tokens"] != max(s["input_tokens"] for s in row["stages"])
                    or row["output_tokens"] != sum(s.get("output_tokens", 0) for s in row["stages"])
                    or not math.isclose(row["seconds"], sum(s["seconds"] for s in row["stages"]))
                    or row["raw_output"] != row["stages"][-1]["raw_output"]
                    or (row["prediction"] is not None and type(row["prediction"]) is not bool)
                    or (row["proposal_prediction"] is not None and type(row["proposal_prediction"]) is not bool)):
                raise ValueError("Certificate aggregate mismatch")
            replay = certificate_protocol.decide(lookup[ident], row["stages"])
            if replay != (row["status"], row["prediction"], row["reason"], row["proposal_prediction"]):
                raise ValueError("Certificate replay mismatch")
        elif row["status"] == "ok":
            if (type(row["prediction"]) is not bool
                    or row["prediction"] != parse_output(row["raw_output"], mode)
                    or row["input_tokens"] > manifest["max_input_tokens"]):
                raise ValueError("Invalid successful prediction")
        elif row["prediction"] is not None:
            raise ValueError("Failure silently repaired")
        arms[mode][ident] = row
    if any(set(arm) != set(lookup) for arm in arms.values()):
        raise ValueError("Incomplete arm")
    return manifest, arms


def finalize(source, pilot, runs, baseline, output):
    frozen = load(pilot / "manifest.json")
    if frozen["source_manifest_sha256"] != file_sha256(source / "manifest.json"):
        raise ValueError("Changed source manifest")
    for kind in ("inputs", "gold"):
        if (frozen[f"source_{kind}_sha256"] != file_sha256(source / f"reserve.{kind}.json")
                or frozen[f"{kind}_sha256"] != file_sha256(pilot / f"{kind}.json")):
            raise ValueError("Changed pilot/source file")
    expected_inputs, expected_gold = select_reserve(source, frozen["pairs"], frozen.get("skip", 0))
    inputs, gold = load(pilot / "inputs.json"), load(pilot / "gold.json")
    if inputs != expected_inputs or gold != expected_gold:
        raise ValueError("Pilot differs from deterministic reserve slice")
    arms, provenance, settings = {}, {}, None
    for run in runs:
        manifest, new_arms = verify_run(run, pilot / "inputs.json", inputs)
        common = {k: v for k, v in manifest.items() if k not in {"modes", "prompts"}}
        if settings is not None and common != settings:
            raise ValueError("Model/code/inference settings differ")
        settings = common
        for mode, predictions in new_arms.items():
            if mode in arms:
                raise ValueError("Duplicate arm")
            arms[mode] = predictions
            provenance[mode] = {"manifest_sha256": file_sha256(run / "manifest.json"),
                                "predictions_sha256": file_sha256(run / "predictions.jsonl"),
                                "prompt_sha256": digest(manifest["prompts"][mode])}
    if baseline not in arms:
        raise ValueError("Missing baseline")
    report = {"pairs": frozen["pairs"], "skip": frozen.get("skip", 0),
              "baseline": baseline, "provenance": provenance,
              "inputs_sha256": file_sha256(pilot / "inputs.json"),
              "gold_sha256": file_sha256(pilot / "gold.json"),
              "scores": {mode: score(gold, arm)[0] for mode, arm in arms.items()},
              "comparisons": {mode: compare(gold, arms[baseline], arm)
                              for mode, arm in arms.items() if mode != baseline},
              "generation": {mode: {"status_counts": dict(Counter(r["status"] for r in arm.values())),
                                     "median_seconds": statistics.median(r["seconds"] for r in arm.values()),
                                     "output_tokens": sum(r.get("output_tokens", 0) for r in arm.values())}
                             for mode, arm in arms.items()},
              "production_ready": False,
              "boundary": "Exploratory reserve pilot; separate confirmation required. Failures counted incorrect, not repaired.",
              "scorer_sha256": file_sha256(__file__)}
    if "certificate" in arms:
        proposal = {ident: {"status": "ok" if row["proposal_prediction"] is not None else row["status"],
                            "prediction": row["proposal_prediction"]}
                    for ident, row in arms["certificate"].items()}
        report["certificate_diagnostics"] = {
            "proposal_only_score": score(gold, proposal)[0],
            "decision_reasons": dict(Counter(r["reason"] for r in arms["certificate"].values())),
            "verifier_calls": sum(len(r["stages"]) == 2 for r in arms["certificate"].values()),
            "boundary": "Proposal-only is a diagnostic ablation, not a selected alternative.",
        }
        candidate_score, base_score = report["scores"]["certificate"], report["scores"][baseline]
        report["certificate_diagnostics"]["recall_up_and_false_pass_down"] = (
            candidate_score["sufficient_recall"] > base_score["sufficient_recall"] and
            candidate_score["observed_false_pass_rate"] < base_score["observed_false_pass_rate"])
        report["certificate_diagnostics"]["verifier_effect_by_class"] = {
            label: dict(Counter(
                "accepted" if arms["certificate"][g["id"]]["prediction"] is True else
                "rejected" if arms["certificate"][g["id"]]["status"] == "ok" else "failed"
                for g in gold if g["sufficient"] == value and
                arms["certificate"][g["id"]]["proposal_prediction"] is True))
            for label, value in (("sufficient", True), ("insufficient", False))}
    write_new(output, report)
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--runs", type=Path, nargs="+", required=True)
    parser.add_argument("--baseline", default="chain")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    finalize(args.source, args.pilot, args.runs, args.baseline, args.output)
