"""
Generate an HTML preview of sed-style substitution rules: parse results,
hit counts, and before/after snippets for each rule.

Usage:
    uv run python devtools/rule_preview.py [rules_file] [input_file]

With no arguments, uses the embedded demo rules and input. Output is written
to build/rules_preview.html.
"""

import html
import sys
from pathlib import Path

from strif.string_replace import Rule, RuleSyntaxError

MAX_SNIPPETS_PER_RULE = 5
SNIPPET_CONTEXT = 40

DEMO_RULES = """\
# Demo rules for rule_preview.py
s/foo/bar/g
s|bar|baz|2
s#baz#qux#I
s#(\\d+)-(\\d+)#\\2/\\1#g
s/not-a-match/never-seen/
"""

DEMO_INPUT = """\
foo and Foo meet foo at the foo bar.
Order matters: 123-456 and 9-42.
bar bar bar, then nothing more.
"""

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>strif rule preview</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #222; }}
  h1 {{ font-size: 1.4rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
  th, td {{ border: 1px solid #ccc; padding: 0.35rem 0.6rem; text-align: left;
           vertical-align: top; font-size: 0.9rem; }}
  th {{ background: #f0f0f0; }}
  code, pre {{ font-family: ui-monospace, Consolas, monospace; }}
  pre {{ margin: 0.2rem 0; white-space: pre-wrap; word-break: break-all; }}
  mark {{ background: #ffe58f; padding: 0 1px; }}
  .error {{ color: #b00020; }}
  .ok {{ color: #1a7f37; }}
  .meta {{ color: #666; font-size: 0.85rem; }}
</style>
</head>
<body>
<h1>strif substitution rule preview</h1>
<p class="meta">Rules are applied in order; each rule sees the output of the
previous one. Hit counts and snippets reflect the text at that step.</p>
{body}
</body>
</html>
"""


def escape(text: str) -> str:
    return html.escape(text, quote=True)


def snippets_for(rule: Rule, text: str) -> list[tuple[str, str]]:
    """
    Up to MAX_SNIPPETS_PER_RULE (before, after) snippet pairs for `rule`
    applied to `text`, with the matched region wrapped in <mark>.
    """
    snippets: list[tuple[str, str]] = []
    matches = list(rule.regex.finditer(text))
    if rule.count > 0:
        matches = matches[: rule.count]
    for match in matches[:MAX_SNIPPETS_PER_RULE]:
        start = max(0, match.start() - SNIPPET_CONTEXT)
        end = min(len(text), match.end() + SNIPPET_CONTEXT)
        before = (
            text[start : match.start()],
            text[match.start() : match.end()],
            text[match.end() : end],
        )
        window = text[start:end]
        after_window = rule.apply(window)
        before_html = (
            escape(before[0]) + "<mark>" + escape(before[1]) + "</mark>" + escape(before[2])
        )
        snippets.append((before_html, escape(after_window)))
    return snippets


def render_rule_row(
    index: int,
    line_no: int,
    line: str,
    parsed_html: str,
    hits: str,
    snippets: list[tuple[str, str]],
) -> str:
    snippet_html = ""
    for before, after in snippets:
        snippet_html += f"<pre>{before}</pre><pre>&#8594; {after}</pre>"
    if not snippet_html:
        snippet_html = '<span class="meta">(no matches)</span>'
    return (
        f"<tr><td>{index}</td><td>{line_no}</td>"
        f"<td><code>{escape(line)}</code></td>"
        f"<td>{parsed_html}</td><td>{hits}</td><td>{snippet_html}</td></tr>"
    )


def build_report(rules_text: str, input_text: str) -> str:
    rows: list[str] = []
    text = input_text
    for line_no, line in enumerate(rules_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            rule = Rule.parse(line, line_no=line_no)
        except RuleSyntaxError as e:
            rows.append(
                render_rule_row(
                    line_no,
                    line_no,
                    line,
                    f'<span class="error">parse error: {escape(e.message)}</span>',
                    "-",
                    [],
                )
            )
            continue
        snippets = snippets_for(rule, text)
        text, hit = rule.apply_with_count(text)
        parsed = (
            f'<span class="ok">ok</span> '
            f"<code>pattern={escape(rule.pattern)!r} replacement={escape(rule.replacement)!r} "
            f"count={rule.count} ignore_case={rule.ignore_case}</code>"
        )
        rows.append(render_rule_row(line_no, line_no, line, parsed, str(hit), snippets))

    table = (
        "<table><tr><th>#</th><th>line</th><th>rule</th><th>parsed</th>"
        "<th>hits</th><th>before &#8594; after</th></tr>" + "".join(rows) + "</table>"
    )
    output_section = "<h2>Final output</h2><pre>" + escape(text) + "</pre>"
    return HTML_TEMPLATE.format(body=table + output_section)


def main() -> int:
    args = sys.argv[1:]
    if len(args) >= 1:
        rules_text = Path(args[0]).read_text(encoding="utf-8")
    else:
        rules_text = DEMO_RULES
    if len(args) >= 2:
        input_text = Path(args[1]).read_text(encoding="utf-8")
    else:
        input_text = DEMO_INPUT

    out_path = Path("build") / "rules_preview.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_report(rules_text, input_text), encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
