"""The click-list: what `paperraven fetch` could not get, for a person to open one at a time.

It is the fallback for papers that cannot be obtained directly while following polite fetching
conventions. There is no "save as" instruction, since users keep the server's file name; the page
says to download normally and run `--collect`. It includes the campus/VPN advice with the licence
caveat, and a Zotero hint.
"""

from __future__ import annotations

import csv
import html
from pathlib import Path
from urllib.parse import urlparse

from ..install import python_command, shell_word
from .client import unsafe_url

COLUMNS = ["title", "authors", "year", "venue", "identifier", "open", "why", "version_warning",
           "section"]


def write_click_list(rows: list[dict], dest: Path, bib: Path,
                     published_only: bool = False) -> tuple[Path, Path]:
    """Write `click_list.html` and `click_list.csv` into `dest`."""
    csv_path, html_path = dest / "click_list.csv", dest / "click_list.html"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    html_path.write_text(_page(rows, dest, bib, published_only), encoding="utf-8")
    return html_path, csv_path


def _intro(n: int, optional: int) -> str:
    """The page's first line: the papers to download, and the optional ones, counted apart,
    so the page never says "0 papers could not be downloaded" above a list of optional rows."""
    first = (f"<b>{n} paper{'s' if n != 1 else ''}</b> could not be downloaded automatically. "
             f"Open them one at a time:" if n else
             "Every paper was downloaded or was already in the folder.")
    if optional:
        first += (f" <b>{optional} paper{'s were' if optional != 1 else ' was'}</b> fetched as "
                  f"preprints; their published versions are listed at the end, optional.")
    return first


def collect_command(bib: Path, dest: Path, downloads: Path | None, *flags: str,
                    for_page: bool = False) -> str:
    """The command that files the user's own downloads in, every path quoted. For the
    terminal: the interpreter that runs this tool and the downloads folder, ready to paste.
    For the page, which is made to be forwarded: `paperraven` by name (an interpreter path
    names the user and their folders) and the downloads folder left to the reader."""
    extra = "".join(f" {f.strip()}" for f in flags if f and f.strip())
    tool = "paperraven" if for_page else f"{python_command()} -m paperraven"
    folder = "<your downloads folder>" if for_page else shell_word(downloads)
    return f"{tool} fetch --bib {shell_word(bib)} --dest {shell_word(dest)}{extra} --collect {folder}"


def _page(all_rows: list[dict], dest: Path, bib: Path, published_only: bool = False) -> str:
    e = html.escape
    rows = [r for r in all_rows if r.get("section", "click") != "optional"]
    optional = [r for r in all_rows if r.get("section") == "optional"]
    items = []
    for n, r in enumerate(rows + optional, 1):
        if optional and r is optional[0]:
            # Fetched, but as a preprint; the published version is the user's choice.
            items.append(f'<h2>Optional: the published version of {len(optional)} paper(s) '
                         f'fetched as preprints</h2><p class="why">A preprint is already in the '
                         f'folder. Download the published version if you need it (figures and '
                         f'numbers can change before publication).</p>')
        link = (f'<a class="open" href="{e(r["open"])}" target="_blank" rel="noopener">'
                f'Open ({e(urlparse(r["open"]).netloc.replace("www.", ""))})</a>'
                if r.get("open") and not unsafe_url(r["open"])
                else '<span class="none">no link: search for the title</span>')
        meta = " · ".join(x for x in (r.get("authors", "").split(";")[0], str(r.get("year") or ""),
                                      r.get("venue") or "") if x)
        warn = (f'<div class="warn">{e(r["version_warning"])}</div>'
                if r.get("version_warning") else "")
        items.append(f'''<div class="row" data-key="{e(r.get("identifier") or r["title"])}">
  <label class="done"><input type="checkbox"> done</label>
  <div class="body"><div class="title">{n}. {e(r["title"])}</div><div class="meta">{e(meta)}</div>
  <div class="links">{link}</div><div class="why">{e(r["why"])}</div>{warn}</div>
</div>''')
    # The same mode as this run, so following the command never fetches what the user
    # declined.
    mode = " --published-only" if published_only else ""
    collect = collect_command(bib, dest, None, mode, for_page=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF click-list</title>
<style>
:root {{ --bg:#fbfaf7; --fg:#1d1d1b; --muted:#6b6a65; --line:#e3e0d8; --accent:#2f5d8a; --warn:#8a4b12; --done:#9aa39a; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#16171a; --fg:#e8e6e1; --muted:#a09d96; --line:#2c2d31; --accent:#8ab4e0; --warn:#e0a060; --done:#5e665e; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system, system-ui, sans-serif; margin:0 auto; max-width:860px; padding:24px 16px 64px; }}
h1 {{ font-size:22px; margin:0 0 8px; }} h2 {{ font-size:17px; margin:28px 0 4px; }} .intro {{ border:1px solid var(--line); border-radius:8px; padding:12px 16px; }}
.intro li {{ margin:4px 0; }} code {{ font-size:13px; word-break:break-all; }}
.row {{ display:flex; gap:12px; border-bottom:1px solid var(--line); padding:12px 0; }}
.row.is-done .body, .row.is-done a {{ color:var(--done); }}
.done {{ white-space:nowrap; color:var(--muted); font-size:13px; padding-top:2px; }}
.title {{ font-weight:600; }} .meta, .why, .none {{ color:var(--muted); font-size:13px; }}
.open {{ color:var(--accent); font-weight:600; }} .warn {{ color:var(--warn); font-size:13px; }} #count {{ color:var(--muted); }}
</style></head><body>
<h1>PDF click-list <span id="count"></span></h1>
<div class="intro">
<p>{_intro(len(rows), len(optional))}</p>
<ul>
<li><b>On a university or institute network or VPN</b>, many subscription papers open directly. Library licences usually forbid bulk automated downloading, which is why these are left for you.</li>
<li>Click <b>Open</b> and download the PDF the usual way. Keep whatever name your browser gives it.</li>
<li>When you have downloaded some, run <code>{e(collect)}</code> with your browser's downloads folder in place of the last argument. It recognises each PDF by its first page and copies it into <code>{e(str(dest))}</code>. Running <code>paperraven fetch</code> again skips every paper already there.</li>
<li><b>Using Zotero?</b> Import <code>{e(bib.name)}</code> and choose "Find Available PDF": Zotero loads each paper's page with your access and shows you any CAPTCHA.</li>
<li>Where a row warns that a copy is not the published version, prefer the published one for figures and numbers.</li>
</ul></div>
{''.join(items)}
<script>
(function () {{
  var rows = document.querySelectorAll('.row'), store = null;
  try {{ store = window.localStorage; store.getItem('x'); }} catch (e) {{ store = null; }}
  function count() {{ document.getElementById('count').textContent =
    '(' + document.querySelectorAll('.row.is-done').length + ' of ' + rows.length + ' done'
    + ({len(optional)} ? ', {len(optional)} of them optional' : '') + ')'; }}
  rows.forEach(function (row) {{
    var box = row.querySelector('input'), key = 'clicklist:' + row.dataset.key;
    try {{ if (store && store.getItem(key) === '1') {{ box.checked = true; row.classList.add('is-done'); }} }} catch (e) {{}}
    box.addEventListener('change', function () {{
      row.classList.toggle('is-done', box.checked);
      try {{ if (store) {{ box.checked ? store.setItem(key, '1') : store.removeItem(key); }} }} catch (e) {{}}
      count(); }});
  }});
  count();
}})();
</script></body></html>"""
