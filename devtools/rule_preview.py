"""
Render an HTML preview of sed-style replacement rules: parse results, hit
counts, and before/after snippets for each rule.

Usage:
    uv run python devtools/rule_preview.py [--rules RULES_FILE] [--input TEXT_FILE]
                                           [--out build/rules_preview.html]

With no arguments it runs a built-in demo ruleset and sample text.
"""

from __future__ import annotations

import argparse
import html
from pathlib import Path

from strif.string_replace import RuleParseError, RuleSet

DEMO_RULES = """\
# Demo rules: arbitrary delimiters, escapes, and combinable flags.
s/foo/bar/g
s|https?://old\\.example\\.com|https://new.example.com|g
s/hello/hi/I
s/\\d+/<num>/2
s/path\\/to\\/file/PATH/
s/(\\w+)@(\\w+)/\\2.\\1/
s/a+/[&]/g
"""

DEMO_TEXT = """\
Say hello to HELLO and Hello again.
foo once, foo twice, foo three times.
Visit http://old.example.com or https://old.example.com/path.
Order 111 then 222 then 333.
See path/to/file for details.
Contact user@host for access.
aaa bbb aaa ccc aaa
"""


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def render_html(rules_source: str, text: str) -> str:
    try:
        ruleset = RuleSet.from_text(rules_source)
    except RuleParseError as e:
        # Parse failures abort: nothing is applied, so we show only the error.
        body = (
            f"<p class='error'>Parse aborted at line {e.line_number}: "
            f"<code>{_esc(e.raw)}</code> &mdash; {_esc(e.reason)}</p>"
        )
        return _page(body)

    outcome = ruleset.apply(text)
    rows: list[str] = []
    for rule, result in zip(ruleset.rules, outcome.results, strict=True):
        flags = rule.flags_str or "(none)"
        rows.append(
            "<section class='rule'>"
            f"<h2>Line {rule.line_number}: <code>{_esc(rule.raw)}</code></h2>"
            "<table>"
            f"<tr><th>Delimiter</th><td><code>{_esc(rule.delimiter)}</code></td></tr>"
            f"<tr><th>Pattern</th><td><code>{_esc(rule.pattern_str)}</code></td></tr>"
            f"<tr><th>Replacement</th><td><code>{_esc(rule.replacement)}</code></td></tr>"
            f"<tr><th>Flags</th><td><code>{_esc(flags)}</code></td></tr>"
            f"<tr><th>Hits</th><td class='hits'>{result.hits}</td></tr>"
            "</table>"
            "<div class='panes'>"
            f"<div><h3>Before</h3><pre>{_esc(result.before_snippet)}</pre></div>"
            f"<div><h3>After</h3><pre>{_esc(result.after_snippet)}</pre></div>"
            "</div>"
            "</section>"
        )
    summary = (
        f"<p>{len(ruleset.rules)} rule(s), {outcome.total_hits} total hit(s).</p>"
        f"<h2>Final text</h2><pre>{_esc(outcome.final_text)}</pre>"
    )
    return _page(summary + "\n".join(rows))


def _page(body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>strif rules preview</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; }}
code {{ background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px; }}
section.rule {{ border: 1px solid #ccc; border-radius: 6px; padding: 1rem; margin: 1rem 0; }}
table {{ border-collapse: collapse; }}
th, td {{ text-align: left; padding: 0.2em 1em 0.2em 0; vertical-align: top; }}
.panes {{ display: flex; gap: 1rem; }}
.panes > div {{ flex: 1; }}
pre {{ background: #fafafa; border: 1px solid #eee; padding: 0.5rem; overflow-x: auto; white-space: pre-wrap; }}
.hits {{ font-weight: bold; }}
.error {{ color: #b00020; }}
</style>
</head>
<body>
<h1>strif rules preview</h1>
{body}
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules", type=Path, default=None, help="File of sed-style rules.")
    parser.add_argument("--input", type=Path, default=None, help="Text file to apply rules to.")
    parser.add_argument(
        "--out", type=Path, default=Path("build/rules_preview.html"), help="Output HTML path."
    )
    args = parser.parse_args()

    rules_source = args.rules.read_text(encoding="utf-8") if args.rules else DEMO_RULES
    text = args.input.read_text(encoding="utf-8") if args.input else DEMO_TEXT

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_html(rules_source, text), encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
