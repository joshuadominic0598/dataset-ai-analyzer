"""Evaluate exact measurements independently from model-generated prose."""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from reviso.agent import Trace, chat, generate_report
from reviso.data import Store, now
from reviso.demo import evaluate, load_demo

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider", choices=["offline", "ollama", "openai", "gemini", "groq"], default="offline"
    )
    parser.add_argument(
        "--model", help="Model available to your provider account; key comes from environment."
    )
    parser.add_argument("--output", type=Path, default=Path("outputs/evaluation.json"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="reviso-evaluation-") as directory:
        store = Store(Path(directory) / "reviso.sqlite")
        dataset = store.create_dataset("Housing evaluation")
        manifest = load_demo(store, dataset["id"])
        if args.provider not in ("offline", "ollama"):
            store.configure(dataset["id"], {"cloud_allowed": True})
        trace = Trace(store, dataset["id"], "evaluation_report", args.provider, args.model)
        try:
            result = generate_report(store, dataset["id"], trace, args.provider, args.model)
            trace.finish(result)
            report = store.report(dataset["id"])
            measured = evaluate(report, manifest)
            output = {
                "created_at": now(),
                "method_version": report["method_version"],
                "prompt_hash": trace.payload["prompt_hash"],
                "provider": args.provider,
                "model": args.model,
                "source_hashes": [v["digest"] for v in report["versions"]],
                "measurements": measured,
                "ai": {
                    "executed": args.provider != "offline",
                    "human_review": "pending" if args.provider != "offline" else "not applicable",
                    "semantic_correctness_score": None,
                    "note": "Citation/numeric checks are guards, not a quality score. Inspect generated answers against the independent expected-change manifest.",
                },
                "answers": [],
                "report": report,
            }
            questions = [
                "How many records were added between versions 1 and 2?",
                "What changed in SalePrice during the problematic import?",
                "Was the renamed price field accompanied by a price change?",
                "Which identity problems prevent exact record counts?",
            ]
            if args.provider != "offline":
                for question in questions:
                    run = Trace(
                        store, dataset["id"], "evaluation_chat", args.provider, args.model, question
                    )
                    try:
                        answer = chat(
                            store, dataset["id"], question, run, args.provider, args.model
                        )
                        run.finish(answer)
                        output["answers"].append(
                            {"question": question, "answer": answer, "human_review": "pending"}
                        )
                    except ValueError as exc:
                        run.finish(error=exc)
                        output["answers"].append(
                            {"question": question, "error": str(exc), "human_review": "not scored"}
                        )
            output["runs"] = store.runs(dataset["id"])
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            answer_failures = sum("error" in answer for answer in output["answers"])
            execution = (
                f" AI answers completed: {len(output['answers']) - answer_failures}/{len(output['answers'])}."
                if output["answers"]
                else ""
            )
            print(
                f"Measured checks: {measured['passed']}/{measured['total']}.{execution} AI semantic quality: not scored. Saved {args.output}"
            )
            if measured["passed"] != measured["total"] or answer_failures:
                sys.exit(1)
        except ValueError as exc:
            trace.finish(error=exc)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps({"failed": True, "error": str(exc), "runs": store.runs(dataset["id"])}),
                encoding="utf-8",
            )
            print(f"Evaluation failed: {exc}", file=sys.stderr)
            sys.exit(1)

if __name__ == "__main__":
    main()
