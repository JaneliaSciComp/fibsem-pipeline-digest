"""Render the fetched board plus the Claude digest into one self-contained HTML page.

Layout: board (who is in which column) → needs attention → one collapsible card per
dataset (title, timeline, owner line; expand for progress/blockers, post-mortem for
finished datasets, raw activity). All facts here come from the board; only the
bullet text comes from the digest.
"""
import html as H
import re
from collections import Counter
from datetime import datetime
from typing import Any

from .digest import Digest, clean_text, dt

COLLABORATORS = {"CellMap", "FuncEWOrm", "eFIB-SEM SR"}
STAGES = [
    ("Imaging", "IM"),
    ("Assembly", "ASM"),
    ("Review", "REV"),
    ("Advanced Processing", "AP"),
    ("Done", "DONE"),
]
COLUMNS = [s for s, _ in STAGES]
# Earlier names of today's columns; timeline events keep the name in use at the time.
RENAMED = {"Alignment": "Assembly", "R&D": "Advanced Processing"}


def _css(stage: str) -> str:
    return stage.lower().replace(" ", "-")


def facts(snap: dict[str, Any], start: datetime) -> dict[str, Any]:
    """Everything the page shows about a dataset that is not narrative."""
    issue = snap["issue"]
    body = issue["body"] or ""
    owner = re.search(r'"owner"\s*:\s*"([^"]+)"', body)
    # First time each column was entered; later re-entries (QC bouncing) are ignored.
    entered: dict[str, datetime] = {}
    for t in snap["transitions"]:
        entered.setdefault(RENAMED.get(t["to"], t["to"]), dt(t["at"]))
    if snap.get("status_changed_at"):
        entered.setdefault(snap["status"], dt(snap["status_changed_at"]))
    preview = None
    for src in [body] + [c["body"] or "" for c in snap["comments"]]:
        if m := re.search(r"\[imaging_preview\]\((http[^)\s]+)\)", src):
            preview = m.group(1)
    in_window = [c for c in snap["comments"] if dt(c["createdAt"]) > start]
    return {
        "number": issue["number"],
        "title": issue["title"],
        "url": issue["url"],
        "status": snap["status"],
        "assignees": issue["assignees"],
        "owner": owner.group(1) if owner else "unknown",
        "collab": next((l for l in issue["labels"] if l in COLLABORATORS), "unknown"),
        "entered": entered,
        "new": min(entered.values(), default=dt(issue["created_at"])) > start,
        "preview": preview,
        "in_window": in_window,
        "last_activity": max(
            [dt(c["createdAt"]) for c in snap["comments"]] + [dt(issue["created_at"])]
        ),
        "people": Counter((c.get("author") or {}).get("login", "?") for c in in_window),
    }


# --------------------------------------------------------------------------- #
# HTML pieces
# --------------------------------------------------------------------------- #


def esc(s: Any) -> str:
    return H.escape(str(s))


def fmt(d: datetime) -> str:
    return d.strftime("%b %-d")


def inline_md(s: str) -> str:
    s = H.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', s)


def stages(d: dict[str, Any]) -> list[tuple[str, str, str, datetime | None]]:
    """(stage, short, state, first-entry date). Everything left of the current column and
    every column ever entered is "reached"; the current one is "current"."""
    cur = COLUMNS.index(d["status"])
    return [
        (stage, short, "current" if i == cur else ("reached" if i < cur or stage in d["entered"] else ""), d["entered"].get(stage))
        for i, (stage, short) in enumerate(STAGES)
    ]


def timeline(d: dict[str, Any], start: datetime) -> str:
    """IM → ASM → REV → AP → DONE with the first-entry date below (bold if this cycle)."""
    out = []
    for stage, short, state, date in stages(d):
        label = "&nbsp;"
        if date:
            label = fmt(date)
            if date > start:
                label = f"<b>{label}</b>"
        out.append(f'<span class="stage {_css(stage)} {state}"><i>{short}</i><small>{label}</small></span>')
    return '<div class="tl">' + '<span class="arrow">→</span>'.join(out) + "</div>"


def activity(d: dict[str, Any]) -> str:
    rows = []
    for c in d["in_window"]:
        body = clean_text(c["body"] or "")
        first = (body.splitlines() or [""])[0]
        author = (c.get("author") or {}).get("login", "?")
        rows.append(
            f'<tr><td class="muted">{c["createdAt"][:10]}</td><td><span class="who">{esc(author)}</span></td>'
            f'<td>{inline_md(first[:200])}{"…" if len(body) > 200 else ""}</td></tr>'
        )
    return f'<table class="act">{"".join(rows)}</table>' if rows else '<p class="muted">No comments in this period.</p>'


def _bullets(items: list[str]) -> str:
    return "".join(f"<li>{inline_md(i)}</li>" for i in items)


def card(d: dict[str, Any], digest: Digest, start: datetime, last: datetime, flagged: set[int]) -> str:
    n = digest.datasets.get(d["number"])
    prog = _bullets(n.progress) if n and n.progress else '<li class="muted">No progress reported.</li>'
    blk = _bullets(n.blockers) if n else ""
    who = " ".join(f'<span class="who">{esc(p)}</span>' for p, _ in d["people"].most_common(4))
    eye = (
        f'<a class="eye" href="{esc(d["preview"])}" title="open imaging preview (neuroglancer)" target="_blank">'
        '<svg viewBox="0 0 24 16" width="18" height="12"><path d="M1 8c3-5 7-7 11-7s8 2 11 7c-3 5-7 7-11 7S4 13 1 8z" '
        'fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="8" r="3.2" fill="currentColor"/></svg></a>'
        if d["preview"] else ""
    )
    mail = (
        '<button class="mail" type="button" title="copy for an e-mail: click = summary text, shift-click = card image, alt-click = raw activity">'
        '<svg viewBox="0 0 24 16" width="18" height="12"><rect x="1" y="1" width="22" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/>'
        '<path d="M1 2l11 8 11-8" fill="none" stroke="currentColor" stroke-width="1.8"/></svg></button>'
    )
    attn = '<span class="attn" title="needs attention">!</span>' if d["number"] in flagged else ""
    pm_html = ""
    if pm := digest.postmortem.get(d["number"]):
        sec = lambda t, items: f"<section><h4>{t}</h4><ul>{_bullets(items)}</ul></section>"  # noqa: E731
        pm_html = (
            '<details class="pm" open><summary>Post-mortem</summary><div class="cols3">'
            f'{sec("What went well", pm.well)}{sec("What didn&#39;t", pm.bad)}{sec("Takeaways / actions", pm.actions)}'
            "</div></details>"
        )
    return f"""
<details class="card {'done' if d['status'] == 'Done' else ''}" id="ds-{d['number']}">
  <summary>
    <header>
      <h3>{attn}<a href="{esc(d['url'])}" target="_blank">{esc(d['title'])}</a> <small class="muted">#{d['number']}</small> {eye}{mail}{'<span class="newtag">new</span>' if d['new'] else ''}</h3>
      {timeline(d, start)}
    </header>
    <div class="meta"><span><b>Owner</b> {esc(d['owner'])} · {esc(d['collab'])}</span><span><b>Assignee</b> {esc(', '.join(d['assignees']) or '—')}</span>
      <span><b>Last activity</b> {fmt(d['last_activity'])} <small class="muted">({(last - d['last_activity']).days}d ago)</small></span><span class="chev">▸</span></div>
  </summary>
  <div class="cols"><section><h4>Progress</h4><ul>{prog}</ul></section>{f'<section class="blockers"><h4>Open questions / blockers</h4><ul>{blk}</ul></section>' if blk else ''}</div>
  {pm_html}
  <details class="act-wrap"><summary>Activity ({len(d['in_window'])} comments · {who})</summary>{activity(d)}</details>
  <template class="mail">{mail_card(d, start, last)}</template><template class="mail-text">{esc(mail_text(d, digest))}</template><template class="mail-act">{esc(mail_activity(d, start, last))}</template>
</details>"""


# --------------------------------------------------------------------------- #
# E-mail version of a card: the collapsed header (title, timeline, owner line) is
# rasterised to PNG in the browser (see JS); links + bullets and the raw activity are
# separate plain-text templates. Click / shift-click / alt-click, see the button title.
# --------------------------------------------------------------------------- #

STAGE_COLOURS = {"Imaging": "#0ea5e9", "Assembly": "#8b5cf6", "Review": "#14b8a6", "Advanced Processing": "#f59e0b", "Done": "#22c55e"}


def _mail_timeline(d: dict[str, Any], start: datetime) -> str:
    cells = []
    for stage, short, state, date in stages(d):
        c = STAGE_COLOURS[stage]
        pill = "border:1.5px solid #d1d5db;color:#9ca3af"
        if state == "reached":
            pill = f"border:1.5px solid {c};color:{c}"
        elif state == "current":
            pill = f"border:1.5px solid {c};color:#fff;background:{c}"
        label = "&nbsp;"
        if date:
            label = f'<b style="color:#2563eb">{fmt(date)}</b>' if date > start else fmt(date)
        cells.append(
            f'<td align="center" style="padding:0 2px;white-space:nowrap"><span style="display:inline-block;padding:1px 8px;border-radius:999px;'
            f'font-weight:700;font-size:10px;{pill}">{short}</span><br><span style="color:#6b7280;font-size:10px">{label}</span></td>'
        )
    arrow = '<td style="color:#9ca3af;vertical-align:top;padding-top:1px">→</td>'
    return f'<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:6px 0"><tr>{arrow.join(cells)}</tr></table>'


def mail_card(d: dict[str, Any], start: datetime, last: datetime) -> str:
    """Inline styles only, so the SVG foreignObject renders it standalone."""
    return f"""<div style="font:14px/1.45 Helvetica,Arial,sans-serif;color:#1c1c1c;width:640px">
<h2 style="margin:0;font-size:17px">{esc(d['title'])} <span style="color:#6b7280;font-weight:400;font-size:13px">#{d['number']}</span></h2>
{_mail_timeline(d, start)}
<p style="margin:4px 0 0;font-size:13px;color:#374151"><span style="color:#6b7280">Status</span> {esc(d['status'])} ·
<span style="color:#6b7280">Owner</span> {esc(d['owner'])} · {esc(d['collab'])} · <span style="color:#6b7280">Assignee</span> {esc(', '.join(d['assignees']) or '—')} ·
<span style="color:#6b7280">Last activity</span> {fmt(d['last_activity'])} ({(last - d['last_activity']).days}d ago)</p>
</div>"""


def mail_text(d: dict[str, Any], digest: Digest) -> str:
    n = digest.datasets.get(d["number"])
    sec = lambda t, items: [t.upper()] + [f"- {i.replace('**', '')}" for i in items] + [""] if items else []  # noqa: E731
    lines = [f"Issue: {d['url']}"] + ([f"Preview: {d['preview']}"] if d["preview"] else []) + [""]
    lines += sec("Progress", n.progress if n else []) or ["No progress reported.", ""]
    lines += sec("Open questions / blockers", n.blockers if n else [])
    if pm := digest.postmortem.get(d["number"]):
        lines += sec("What went well", pm.well) + sec("What didn't", pm.bad) + sec("Takeaways / actions", pm.actions)
    return "\n".join(lines).rstrip()


def mail_activity(d: dict[str, Any], start: datetime, last: datetime) -> str:
    lines = [f"Raw GitHub activity ({len(d['in_window'])} comments, {fmt(start)} → {fmt(last)})"]
    for c in d["in_window"]:
        author = (c.get("author") or {}).get("login", "?")
        lines.append(f"{c['createdAt'][:10]}  {author}: {clean_text(c['body'] or '')[:400]}")
    return "\n".join(lines)


def board(ds: list[dict[str, Any]], flagged: set[int]) -> str:
    counts = Counter(d["status"] for d in ds)
    cols = []
    for c in COLUMNS:
        chips = "".join(
            f'<a class="chip {"flagged" if d["number"] in flagged else ""}" href="#ds-{d["number"]}" title="{esc(d["title"])}">'
            f'{"<b>!</b> " if d["number"] in flagged else ""}{esc(d["title"].removeprefix("jrc_"))}{" <em>new</em>" if d["new"] else ""}</a>'
            for d in ds
            if d["status"] == c
        )
        cols.append(f'<div class="col {_css(c)}"><h5>{esc(c)} <span class="n">{counts.get(c, 0)}</span></h5>{chips}</div>')
    return "".join(cols)


def attention(digest: Digest, by_num: dict[int, dict[str, Any]]) -> str:
    items = []
    for a in digest.attention:
        d = by_num.get(a.number)
        if d is None:
            continue
        items.append(
            f'<li><a href="#ds-{d["number"]}"><b>{esc(d["title"])}</b></a> '
            f'<span class="muted">{esc(d["status"])} · {esc(", ".join(d["assignees"]) or "unassigned")}</span>'
            f"<div>{inline_md(a.text)}</div></li>"
        )
    return "".join(items) or '<li class="muted">Nothing blocking.</li>'


CSS = """
:root{--fg:#1c1c1c;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;--warn:#b45309;--warnbg:#fff7ed;--attn:#fef3c7;
 --imaging:#0ea5e9;--assembly:#8b5cf6;--review:#14b8a6;--advanced-processing:#f59e0b;--done:#22c55e}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:var(--fg);background:#f6f7f9}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
main{max-width:64em;margin:0 auto;padding:1.5em 1em 4em}
h1{font-size:1.5em;margin:0}h2{font-size:1.1em;margin:1.8em 0 .6em;padding-bottom:.25em;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:1em}
h2 .tools{margin-left:auto;font-size:.75em;font-weight:400}h2 .tools button{font:inherit;background:none;border:0;color:var(--accent);cursor:pointer;padding:0 .3em}
.muted{color:var(--muted)}small{font-size:.85em}
.top{display:flex;align-items:baseline;gap:1em;flex-wrap:wrap}
.board{display:grid;grid-template-columns:repeat(5,1fr);gap:.6em}
.col{background:#fff;border:1px solid var(--line);border-top:3px solid var(--c);border-radius:8px;padding:.5em .6em;min-height:4.5em}
.col.imaging{--c:var(--imaging)}.col.assembly{--c:var(--assembly)}.col.review{--c:var(--review)}.col.advanced-processing{--c:var(--advanced-processing)}.col.done{--c:var(--done)}
.col h5{margin:0 0 .4em;font-size:.75em;text-transform:uppercase;letter-spacing:.04em;color:var(--c)}.col .n{float:right;color:var(--fg)}
.chip{display:block;font-size:.82em;padding:.2em .45em;margin:.2em 0;border-radius:5px;background:color-mix(in srgb,var(--c) 12%,#fff);color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip.flagged{background:var(--attn)}.chip b{color:var(--warn)}.chip em{font-style:normal;color:var(--accent);font-size:.85em}
.attention{background:var(--warnbg);border:1px solid #fdba74;border-radius:8px;padding:.5em 1em .5em 1.3em;margin:0}
.attention li{margin:.45em 0}.attention li div{font-size:.95em}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:.6em 1.1em;margin:.5em 0;font-size:1em}
.card>summary{list-style:none;display:block;color:inherit;font-size:1em}.card>summary::-webkit-details-marker{display:none}
.chev{margin-left:auto;color:#9ca3af;transition:transform .15s}.card[open] .chev{transform:rotate(90deg)}
.card[open]>summary{border-bottom:1px solid var(--line);padding-bottom:.5em;margin-bottom:.5em}
.card.done{border-color:#bbf7d0}
.card header{display:flex;align-items:center;gap:1em;flex-wrap:wrap}
.card h3{margin:0;font-size:1.05em;flex:1;min-width:16em;display:flex;align-items:center;gap:.4em}
.attn{display:inline-block;width:1.25em;height:1.25em;border-radius:50%;background:var(--warn);color:#fff;text-align:center;font-weight:700;font-size:.75em;line-height:1.25em}
.eye,.mail{color:#9ca3af;display:inline-flex;align-items:center}.eye:hover,.mail:hover{color:var(--accent)}
.mail{background:none;border:0;padding:0;cursor:pointer}.mail.ok{color:#16a34a}
.newtag{font-size:.7em;font-weight:400;color:var(--accent);border:1px solid var(--accent);border-radius:999px;padding:0 .5em}
.tl{display:flex;align-items:flex-start;gap:.15em;font-size:.72em}
.stage{display:flex;flex-direction:column;align-items:center;width:4.6em}
.stage i{font-style:normal;font-weight:700;letter-spacing:.03em;padding:.1em .5em;border-radius:999px;border:1.5px solid #d1d5db;color:#9ca3af}
.stage.reached i{border-color:var(--c);color:var(--c)}.stage.current i{background:var(--c);border-color:var(--c);color:#fff}
.stage small{color:var(--muted);font-size:.9em;white-space:nowrap;margin-top:.15em}.stage small b{color:var(--accent)}
.stage.imaging{--c:var(--imaging)}.stage.assembly{--c:var(--assembly)}.stage.review{--c:var(--review)}.stage.advanced-processing{--c:var(--advanced-processing)}.stage.done{--c:var(--done)}
.arrow{color:#9ca3af;margin-top:.05em}
.meta{display:flex;gap:1.4em;flex-wrap:wrap;font-size:.86em;color:#374151;margin:.3em 0 0}.meta b{color:var(--muted);font-weight:500;margin-right:.2em}
details{margin-top:.4em;font-size:.93em}summary{cursor:pointer;color:var(--muted);font-size:.9em}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:1em;margin-top:.5em}.cols3{display:grid;grid-template-columns:repeat(3,1fr);gap:1em;margin-top:.3em}
.cols section,.cols3 section{min-width:0}.cols ul,.cols3 ul{margin:0;padding-left:1.2em}.cols li,.cols3 li{margin:.2em 0}
.cols h4,.cols3 h4{margin:0 0 .3em;font-size:.78em;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.blockers{background:var(--warnbg);border-radius:8px;padding:.5em .9em}.blockers h4{color:var(--warn)}
.pm{background:#f0fdf4;border-radius:8px;padding:.4em .8em}.pm summary{color:#166534;font-weight:600}
.who{font-size:.85em;background:#f1f5f9;border-radius:4px;padding:0 .4em;white-space:nowrap}
.act{width:100%;border-collapse:collapse;margin-top:.4em}.act td{padding:.25em .5em;border-top:1px solid var(--line);vertical-align:top}
.act td:first-child{white-space:nowrap;width:6.5em}.act td:nth-child(2){width:8em}
@media(max-width:800px){.board{grid-template-columns:repeat(2,1fr)}.cols,.cols3{grid-template-columns:1fr}}
@media print{.tools{display:none}.card{break-inside:avoid}.act-wrap{display:none}}
"""

JS = """
const set=o=>document.querySelectorAll('.card').forEach(d=>d.open=o);
document.getElementById('exp').onclick=()=>set(true);document.getElementById('col').onclick=()=>set(false);
const openHash=()=>{const t=document.querySelector(location.hash||'#none');if(t&&t.classList.contains('card'))t.open=true};
addEventListener('hashchange',openHash);openHash();
const toPng=async html=>{
  const wrap=document.body.appendChild(document.createElement('div'));
  wrap.style.cssText='position:fixed;left:-9999px;top:0;width:664px;background:#fff;padding:12px';wrap.innerHTML=html;
  const h=wrap.offsetHeight,clone=wrap.cloneNode(true);wrap.remove();clone.style.cssText='width:664px;background:#fff;padding:12px';
  const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="664" height="${h}"><foreignObject width="100%" height="100%">${new XMLSerializer().serializeToString(clone)}</foreignObject></svg>`;
  const img=new Image();img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg);await img.decode();
  const c=document.createElement('canvas');c.width=664*2;c.height=h*2;const g=c.getContext('2d');g.scale(2,2);g.drawImage(img,0,0);
  return new Promise(r=>c.toBlob(r,'image/png'));
};
document.querySelectorAll('button.mail').forEach(b=>b.onclick=async e=>{
  e.preventDefault();
  const card=b.closest('.card');
  try{
    if(e.shiftKey)await navigator.clipboard.write([new ClipboardItem({'image/png':toPng(card.querySelector('template.mail').innerHTML)})]);
    else await navigator.clipboard.writeText(card.querySelector(e.altKey?'template.mail-act':'template.mail-text').content.textContent);
    b.classList.add('ok');setTimeout(()=>b.classList.remove('ok'),1500);
  }catch(err){alert('Copy failed: '+err)}
});
"""


def render(fetched: dict[str, Any], digest: Digest) -> str:
    """`fetched` is the board.json dict written by fetch."""
    start, last = dt(fetched["since"]), dt(fetched["fetched_at"])
    issues_url = f"https://github.com/{fetched['datasets'][0]['issue']['repository']}/issues"
    ds = sorted((facts(s, start) for s in fetched["datasets"]), key=lambda d: (COLUMNS.index(d["status"]), d["number"]))
    by_num = {d["number"]: d for d in ds}
    flagged = {a.number for a in digest.attention}
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FIB-SEM digest · {last:%Y-%m-%d}</title><style>{CSS}</style></head><body><main>
<div class="top"><h1>FIB-SEM reconstruction digest</h1>
<span class="muted">{fmt(start)} → {last:%b %-d, %Y} · <a href="{esc(issues_url)}">issues</a></span></div>

<h2>Board</h2>
<div class="board">{board(ds, flagged)}</div>

<h2>Needs attention this week</h2>
<ul class="attention">{attention(digest, by_num)}</ul>

<h2>Datasets <span class="tools"><button id="exp">expand all</button>·<button id="col">collapse all</button></span></h2>
{"".join(card(d, digest, start, last, flagged) for d in ds)}
</main><script>{JS}</script></body></html>"""
