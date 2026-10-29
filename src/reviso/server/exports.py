"""Portable, escaped report exports."""

import html
import json
from pathlib import Path
from string import Template

def export_markdown(report):
    lines = [
        f"# {report['dataset_name']}",
        "",
        f"Report: {report['id']} · Created: {report['created_at']}",
        f"Analytic Brain · Method: {report['method_version']}",
        "",
        "## Source files",
        "",
    ]
    for v in report["versions"]:
        lines.append(
            f"- V{v['ordinal']} {v['label']}: {v['row_count']} records; SHA-256 {v['digest']}"
        )
    if report["ai"]:
        lines += ["", "## AI interpretation", "", report["ai"]["summary"]]
        for insight in report["ai"]["insights"]:
            lines += [
                "",
                insight["interpretation"] + " [" + ", ".join(insight["evidence_ids"]) + "]",
                "Next: " + insight["next_step"],
            ]
        if report["ai"].get("query_results"):
            lines += [
                "",
                "## Supporting queries",
                "",
                "```json",
                json.dumps(report["ai"]["query_results"], indent=2, ensure_ascii=False),
                "```",
            ]
    lines += ["", "## Findings", ""]
    for f in report["findings"]:
        scope = (
            f"V{f['to']} · dataset analysis"
            if f.get("scope") == "snapshot"
            else f"V{f['from']} → V{f['to']}"
        )
        lines += [
            f"### [{f['id']}] {f['title']}",
            f"{scope} · {f['severity']} · {f['assessment']}",
            "",
            f["detail"],
            "",
            "Next: " + f["action"],
            "",
            "```json",
            json.dumps(
                {"metrics": f["metrics"], "examples": f.get("examples", [])},
                indent=2,
                ensure_ascii=False,
            ),
            "```",
            "",
        ]
    if report.get("exploration"):
        lines += [
            "## Latest file exploration",
            "",
            "```json",
            json.dumps(report["exploration"], indent=2, ensure_ascii=False),
            "```",
            "",
        ]
    lines += ["## Limits", ""] + ["- " + line for line in report["limitations"]]
    return "\n".join(lines)

def _finding_html(finding):
    scope = (
        f"V{finding['to']} · dataset analysis"
        if finding.get("scope") == "snapshot"
        else f"V{finding['from']} → V{finding['to']}"
    )
    evidence = {"metrics": finding["metrics"], "examples": finding.get("examples", [])}
    return (
        f"<article><small>{html.escape(finding['id'])} · {scope} · "
        f"{html.escape(finding['severity'])}</small>"
        f"<h2>{html.escape(finding['title'])}</h2><p>{html.escape(finding['detail'])}</p>"
        f"<p><b>Next step</b> {html.escape(finding['action'])}</p>"
        "<details><summary>Evidence and record examples</summary>"
        f"<pre>{_json_html(evidence)}</pre></details></article>"
    )

def _json_html(value):
    return html.escape(json.dumps(value, indent=2, ensure_ascii=False))

def _interpretation_html(interpretation):
    if not interpretation:
        return ""
    insights = "".join(
        f"<p>{html.escape(insight['interpretation'])} "
        f"[{html.escape(', '.join(insight['evidence_ids']))}]</p>"
        for insight in interpretation["insights"]
    )
    rendered = (
        "<article><small>AI interpretation</small>"
        f"<p>{html.escape(interpretation['summary'])}</p>{insights}</article>"
    )
    if interpretation.get("query_results"):
        rendered += (
            "<article><h2>Supporting queries</h2>"
            f"<pre>{_json_html(interpretation['query_results'])}</pre></article>"
        )
    return rendered

def export_html(report):
    templates = Path(__file__).parent / "templates"
    template = Template((templates / "report.html").read_text(encoding="utf-8"))
    findings = "".join(_finding_html(finding) for finding in report["findings"])
    if report.get("exploration"):
        findings += (
            "<article><h2>Latest file exploration</h2><details>"
            "<summary>Full field profiles and exploration results</summary>"
            f"<pre>{_json_html(report['exploration'])}</pre></details></article>"
        )
    versions = "".join(
        f"<li>V{version['ordinal']} · {html.escape(version['label'])} · "
        f"{version['row_count']:,} records<br><code>{html.escape(version['digest'])}</code></li>"
        for version in report["versions"]
    )
    return template.substitute(
        dataset_name=html.escape(report["dataset_name"]),
        styles=(templates / "report.css").read_text(encoding="utf-8").rstrip(),
        version_count=report["summary"]["versions"],
        latest_records=f"{report['summary']['latest_records']:,}",
        attention=report["summary"]["attention"],
        created_at=html.escape(report["created_at"]),
        report_id=html.escape(report["id"]),
        method_version=html.escape(report["method_version"]),
        interpretation=_interpretation_html(report["ai"]),
        findings=findings,
        versions=versions,
        limitations="".join(f"<li>{html.escape(limit)}</li>" for limit in report["limitations"]),
    )
