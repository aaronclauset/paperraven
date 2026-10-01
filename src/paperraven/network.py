"""The citation network among the corpus papers, for the export.

Nodes are the greenlist papers. An edge is one corpus paper citing another, read from the
OpenAlex reference lists the records keep (`references.reference_lists`). The page is a single
self-contained HTML file: its data and its code are inline, so it opens offline from the export
folder, and every text in it is set as text, never parsed as markup.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .models import DiscoveredBy
from .normalize import identifier_url

if TYPE_CHECKING:
    from .corpus import Corpus

#: How a paper came into the corpus, in legend order, with its colour (Okabe–Ito, which stays
#: distinguishable with the common colour-vision deficiencies).
FOUND = {"seed": "#E69F00", "recall": "#0072B2", "search": "#009E73",
         "references": "#CC79A7", "not stated": "#8C8C8C"}


#: The most papers the page draws. Every step of its layout compares every pair of papers, so
#: a larger network is exported as `citations.csv` alone, for a tool built for that size.
DRAW_LIMIT = 500


def _found(rec) -> str:
    if rec.provenance.discovered_by is DiscoveredBy.SEED:
        return "seed"
    return rec.provenance.proposer_source or "not stated"


def _authors(rec) -> str:
    names = rec.authors
    return ", ".join(names[:3]) + (" et al." if len(names) > 3 else "")


def build_network(corpus: "Corpus", found: dict[str, Any]) -> dict[str, Any]:
    """Nodes, edges and counts for the greenlist, from `reference_lists`' result."""
    from .export import _citation_keys

    keys = _citation_keys(corpus)
    records = sorted(corpus.greenlist.values(), key=lambda r: keys[r.doi])
    index = {r.doi: i for i, r in enumerate(records)}
    by_work = {wid: ident for ident, wid in found["ids"].items()}
    edges = sorted({(index[citing], index[by_work[w]])
                    for citing, refs in found["lists"].items()
                    for w in refs if w in by_work and by_work[w] != citing})
    cited_by = [0] * len(records)
    for _s, t in edges:
        cited_by[t] += 1
    nodes = [{"id": i, "key": keys[r.doi], "doi": r.doi, "title": r.title,
              "authors": _authors(r), "year": r.year, "venue": r.venue,
              "url": identifier_url(r.doi), "found": _found(r),
              "has_list": r.doi in found["lists"], "cited_by": cited_by[i]}
             for i, r in enumerate(records)]
    return {"nodes": nodes, "edges": [{"source": s, "target": t} for s, t in edges],
            "without_list": sum(1 for n in nodes if not n["has_list"]),
            "found": FOUND}


def citation_rows(net: dict[str, Any]) -> list[list[str]]:
    """The edge list as (citing key, cited key)."""
    key = [n["key"] for n in net["nodes"]]
    return [[key[e["source"]], key[e["target"]]] for e in net["edges"]]


def _script_json(data: Any) -> str:
    """JSON that cannot end its `<script>` element or open a comment: no `<` survives."""
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def network_html(net: dict[str, Any], *, title: str) -> str:
    """The self-contained page. Its text is written here, escaped; the script only draws.

    A dragged paper pulls its neighbours along, and drifts back into the layout when released.
    """
    nodes = net["nodes"]
    how = ("Hover for details, click to open, drag a paper to move it. Scroll to zoom, drag "
           "the background to pan.")
    sub = (f"{_plural(len(nodes), 'paper', 'papers')}, "
           f"{_plural(len(net['edges']), 'citation', 'citations')} among them. Size shows "
           f"citations from other corpus papers; colour, how each paper was found. {how}")
    if net["without_list"]:
        sub += (f" {_plural(net['without_list'], 'paper has', 'papers have')} no reference "
                f"list in OpenAlex, so what they cite is unknown.")
    present = {n["found"] for n in nodes}
    legend = "".join(
        f'<span><i class="dot" style="background:{colour}"></i>'
        f'{"seed (yours)" if name == "seed" else name}</span>'
        for name, colour in FOUND.items() if name in present)
    legend += '<span><i class="ring"></i>no reference list in OpenAlex</span>'
    return (_PAGE.replace("__DATA__", _script_json(net))
            .replace("__TITLE__", _escape(title))
            .replace("__SUB__", _escape(sub))
            .replace("__LEGEND__", legend))


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Citation network — __TITLE__</title>
<style>
  :root { --bg: #ffffff; --fg: #1d1d1f; --muted: #6e6e73; --edge: rgba(60, 60, 67, 0.16);
          --hi: rgba(0, 0, 0, 0.75); --panel: rgba(255, 255, 255, 0.94); --line: #d2d2d7; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #111113; --fg: #f2f2f7; --muted: #a1a1a6; --edge: rgba(235, 235, 245, 0.18);
            --hi: rgba(255, 255, 255, 0.85); --panel: rgba(28, 28, 30, 0.94); --line: #3a3a3c; }
  }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--fg);
               font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               Helvetica, Arial, sans-serif; }
  header { padding: 14px 16px 6px; }
  h1 { font-size: 17px; margin: 0 0 2px; font-weight: 600; }
  .sub { color: var(--muted); margin: 0; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 16px; padding: 6px 16px 8px;
            color: var(--muted); font-size: 13px; border-bottom: 1px solid var(--line); }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .dot { width: 11px; height: 11px; border-radius: 50%; display: inline-block; }
  .ring { width: 9px; height: 9px; border-radius: 50%; display: inline-block;
          border: 1.5px dashed var(--muted); }
  main { position: relative; height: calc(100% - 96px); min-height: 360px; }
  svg { width: 100%; height: 100%; display: block; cursor: grab; touch-action: none;
        user-select: none; -webkit-user-select: none; }
  svg.dragging { cursor: grabbing; }
  .tip { position: absolute; pointer-events: none; max-width: 340px; padding: 8px 10px;
         background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
         box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12); font-size: 13px; display: none; }
  .tip .t { font-weight: 600; margin-bottom: 3px; }
  .tip .m { color: var(--muted); }
  .strip { font-size: 12px; fill: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>Citation network — __TITLE__</h1>
  <p class="sub">__SUB__</p>
</header>
<div class="legend">__LEGEND__</div>
<main>
  <svg id="net" role="img" aria-label="Citation network among the corpus papers"></svg>
  <div class="tip" id="tip"></div>
</main>
<script id="network-data" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  var data = JSON.parse(document.getElementById("network-data").textContent);
  var nodes = data.nodes, edges = data.edges, found = data.found;
  var NS = "http://www.w3.org/2000/svg";
  function el(name, attrs, parent) {
    var x = document.createElementNS(NS, name);
    for (var k in attrs) { x.setAttribute(k, attrs[k]); }
    if (parent) { parent.appendChild(x); }
    return x;
  }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }


  // Layout: a damped spring simulation. Citations are springs with a natural length, papers
  // repel one another and are held loosely towards the centre, and every velocity is damped
  // while a cooling factor brings the whole picture to rest. It starts from a fixed spiral, so
  // the same corpus always draws the same picture, and it is the same simulation that runs
  // while a paper is dragged.
  var degree = nodes.map(function () { return 0; });
  edges.forEach(function (e) { degree[e.source]++; degree[e.target]++; });
  var linked = nodes.filter(function (n) { return degree[n.id] > 0; });
  var alone = nodes.filter(function (n) { return degree[n.id] === 0; });
  function radius(n) { return 3 + 1.6 * Math.sqrt(n.cited_by); }
  var LINK = 46, CHARGE = -110, CENTRE = 0.05, DAMPING = 0.4, COLLIDE = 0.7;
  var ALPHA_MIN = 0.001, COOLING = 1 - Math.pow(ALPHA_MIN, 1 / 300), DRAG_ALPHA = 0.2;
  linked.forEach(function (n, i) {           // a sunflower spiral: even, deterministic
    var r = 12 * Math.sqrt(0.5 + i), a = i * Math.PI * (3 - Math.sqrt(5));
    n.x = r * Math.cos(a); n.y = r * Math.sin(a); n.vx = 0; n.vy = 0; n.fx = null;
  });
  var springs = edges.map(function (e) {
    var ds = degree[e.source], dt = degree[e.target];
    return {s: nodes[e.source], t: nodes[e.target], strength: 1 / Math.min(ds, dt),
            bias: ds / (ds + dt)};
  });
  var alpha = 1, alphaTarget = 0;
  function tick() {
    alpha += (alphaTarget - alpha) * COOLING;
    springs.forEach(function (l) {
      var dx = l.t.x + l.t.vx - l.s.x - l.s.vx, dy = l.t.y + l.t.vy - l.s.y - l.s.vy;
      var d = Math.sqrt(dx * dx + dy * dy) || 1e-6, f = (d - LINK) / d * alpha * l.strength;
      dx *= f; dy *= f;
      l.t.vx -= dx * l.bias; l.t.vy -= dy * l.bias;
      l.s.vx += dx * (1 - l.bias); l.s.vy += dy * (1 - l.bias);
    });
    for (var i = 0; i < linked.length; i++) {
      var a = linked[i], ra = radius(a);
      for (var j = i + 1; j < linked.length; j++) {
        var b = linked[j], dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy;
        if (d2 < 1) { dx = 0.5; dy = 0.5; d2 = 1; }
        var w = CHARGE * alpha / d2;           // repulsion, softened at short range
        a.vx += dx * w; a.vy += dy * w; b.vx -= dx * w; b.vy -= dy * w;
        var rr = ra + radius(b) + 1.5;
        if (d2 < rr * rr) {                    // the circles overlap: push them apart
          var d = Math.sqrt(d2), push = (rr - d) / d * COLLIDE * 0.5;
          a.vx -= dx * push; a.vy -= dy * push; b.vx += dx * push; b.vy += dy * push;
        }
      }
    }
    var moved = 0;
    linked.forEach(function (n) {
      n.vx -= n.x * CENTRE * alpha; n.vy -= n.y * CENTRE * alpha;
      if (n.fx !== null) { n.x = n.fx; n.y = n.fy; n.vx = 0; n.vy = 0; return; }
      n.vx *= 1 - DAMPING; n.vy *= 1 - DAMPING;
      n.x += n.vx; n.y += n.vy;
      moved = Math.max(moved, Math.abs(n.vx) + Math.abs(n.vy));
    });
    return moved;
  }
  while (alpha > ALPHA_MIN) { tick(); }
  // Cooling alone stops short of rest, so run on at the drag's own warmth until nothing
  // moves: the collision force is not scaled by warmth, so rest at any other warmth would
  // shift as a drag begins. A drag then moves only what it pulls. The budget is a number of
  // steps, never a time, so every browser draws the same picture.
  var budget = Math.min(3000, Math.floor(2e8 / Math.max(1, linked.length * linked.length)));
  for (var more = 0; more < budget; more++) {
    alpha = DRAG_ALPHA;
    if (tick() < 0.002) { break; }
  }
  alpha = ALPHA_MIN;

  // Fit the view to the settled picture; papers with no citation to or from another corpus
  // paper sit in a strip below it.
  var minX = -300, maxX = 300, minY = -200, maxY = 200;
  if (linked.length) {
    minX = Math.min.apply(null, linked.map(function (n) { return n.x - radius(n); }));
    maxX = Math.max.apply(null, linked.map(function (n) { return n.x + radius(n); }));
    minY = Math.min.apply(null, linked.map(function (n) { return n.y - radius(n); }));
    maxY = Math.max.apply(null, linked.map(function (n) { return n.y + radius(n); }));
  }
  var width = Math.max(maxX - minX, 400), perRow = Math.max(1, Math.floor(width / 18));
  var stripTop = maxY + 34;
  alone.forEach(function (n, i) {
    n.x = minX + 9 + (i % perRow) * 18; n.y = stripTop + 22 + Math.floor(i / perRow) * 18;
  });
  var bottom = alone.length ? stripTop + 30 + Math.ceil(alone.length / perRow) * 18 : maxY;

  var svg = document.getElementById("net"), pad = 16;
  var view = [minX - pad, minY - pad, width + 2 * pad, bottom - minY + 2 * pad];
  function setView() { svg.setAttribute("viewBox", view.join(" ")); }
  setView();
  // Screen to network coordinates, through the SVG's own transform (which allows for the
  // letterboxing of a viewBox whose shape differs from the window's).
  function toWorld(evt) {
    var p = svg.createSVGPoint(); p.x = evt.clientX; p.y = evt.clientY;
    return p.matrixTransform(svg.getScreenCTM().inverse());
  }
  var world = el("g", {}, svg);
  var edgeLayer = el("g", {}, world), nodeLayer = el("g", {}, world);
  var lines = edges.map(function (e) {
    var a = nodes[e.source], b = nodes[e.target];
    return el("line", {x1: a.x, y1: a.y, x2: b.x, y2: b.y, stroke: "var(--edge)",
                       "stroke-width": 1}, edgeLayer);
  });
  if (alone.length) {
    var label = el("text", {x: minX, y: stripTop + 6, "class": "strip"}, world);
    label.textContent = "No citation to or from another corpus paper (" + alone.length + ")";
  }
  var neighbours = nodes.map(function () { return []; });
  edges.forEach(function (e, i) { neighbours[e.source].push(i); neighbours[e.target].push(i); });

  var tip = document.getElementById("tip");
  function line(cls, text) {
    var d = document.createElement("div"); d.className = cls; d.textContent = text;
    tip.appendChild(d);
  }
  function highlight(n, on) {
    neighbours[n.id].forEach(function (i) {
      lines[i].setAttribute("stroke", on ? "var(--hi)" : "var(--edge)");
      lines[i].setAttribute("stroke-width", on ? 1.6 : 1);
    });
  }
  function show(n, evt) {
    tip.textContent = "";
    line("t", n.title || "untitled");
    line("m", [n.authors, n.year, n.venue].filter(Boolean).join(" · "));
    line("m", "cited by " + plural(n.cited_by, "corpus paper", "corpus papers") +
              " · found by " + n.found + (n.has_list ? "" : " · no reference list"));
    line("m", n.key);
    tip.style.display = "block";
    move(evt);
    highlight(n, true);
  }
  function move(evt) {
    var box = svg.getBoundingClientRect(), x = evt.clientX - box.left + 14,
        y = evt.clientY - box.top + 14;
    if (x + 350 > box.width) { x = Math.max(4, x - 370); }
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  function hide(n) {
    tip.style.display = "none";
    if (!grab || grab.node !== n) { highlight(n, false); }
  }
  function open(n) {
    if (/^https:\\/\\//.test(n.url)) { window.open(n.url, "_blank", "noopener"); }
  }

  var circles = [];
  function redraw() {
    edges.forEach(function (e, i) {
      var a = nodes[e.source], b = nodes[e.target];
      lines[i].setAttribute("x1", a.x); lines[i].setAttribute("y1", a.y);
      lines[i].setAttribute("x2", b.x); lines[i].setAttribute("y2", b.y);
    });
    linked.forEach(function (n) {
      circles[n.id].setAttribute("cx", n.x); circles[n.id].setAttribute("cy", n.y);
    });
  }

  // While a paper is dragged the simulation runs warm, so its neighbours follow; released, it
  // cools and stops once nothing is moving.
  var running = false, grab = null, suppressClick = false;
  function frame() {
    var moved = tick();
    redraw();
    if (alpha > ALPHA_MIN && (alphaTarget > 0 || moved > 0.01)) { requestAnimationFrame(frame); }
    else { running = false; }
  }
  function wake() {
    if (!running) { running = true; requestAnimationFrame(frame); }
  }

  nodes.slice().sort(function (a, b) { return a.cited_by - b.cited_by; }).forEach(function (n) {
    var c = el("circle", {cx: n.x, cy: n.y, r: radius(n), fill: found[n.found] || "#8C8C8C",
                          stroke: n.has_list ? "var(--bg)" : "var(--fg)",
                          "stroke-width": n.has_list ? 1 : 1.2,
                          "stroke-dasharray": n.has_list ? "none" : "2 1.5"}, nodeLayer);
    circles[n.id] = c;
    var draggable = degree[n.id] > 0;
    c.style.cursor = draggable ? "grab" : "pointer";
    c.addEventListener("mouseenter", function (evt) { if (!grab) { show(n, evt); } });
    c.addEventListener("mousemove", function (evt) { if (!grab) { move(evt); } });
    c.addEventListener("mouseleave", function () { if (!grab) { hide(n); } });
    c.addEventListener("pointerdown", function (evt) {
      // A press is never the start of a selection: Safari would highlight, in blue, every
      // paper and line a drag crosses. The click that follows is unaffected.
      evt.preventDefault();
      suppressClick = false;
      if (!draggable) { return; }
      evt.stopPropagation();
      grab = {node: n, circle: c, x: evt.clientX, y: evt.clientY, moved: false};
      // A fast pointer soon leaves the circle, so the drag follows it through the window;
      // some browsers (Safari) do not hold its events to the circle that was pressed.
      window.addEventListener("pointermove", dragMove);
      window.addEventListener("pointerup", dragEnd);
      window.addEventListener("pointercancel", dragEnd);
    });
    c.addEventListener("click", function () {
      if (suppressClick) { suppressClick = false; return; }
      open(n);
    });
  });

  function dragMove(evt) {
    if (!grab) { return; }
    var n = grab.node;
    if (!grab.moved) {
      if (Math.abs(evt.clientX - grab.x) + Math.abs(evt.clientY - grab.y) < 5) { return; }
      grab.moved = true; grab.circle.style.cursor = "grabbing";
      tip.style.display = "none"; highlight(n, true);
      alphaTarget = DRAG_ALPHA; alpha = Math.max(alpha, DRAG_ALPHA);
    }
    var p = toWorld(evt);
    n.fx = p.x; n.fy = p.y;
    wake();
  }
  function dragEnd() {
    window.removeEventListener("pointermove", dragMove);
    window.removeEventListener("pointerup", dragEnd);
    window.removeEventListener("pointercancel", dragEnd);
    if (!grab) { return; }
    var n = grab.node;
    if (grab.moved) {
      suppressClick = true;               // the click that may follow ends a drag, not a visit
      grab.circle.style.cursor = "grab";
      n.fx = null; n.fy = null;           // released, it drifts back into the layout
      alphaTarget = 0;
      highlight(n, false);
      wake();
    }
    grab = null;
  }

  // Zoom with the wheel, pan by dragging the background.
  svg.addEventListener("wheel", function (evt) {
    evt.preventDefault();
    var s = evt.deltaY > 0 ? 1.12 : 1 / 1.12, p = toWorld(evt);
    view = [p.x - (p.x - view[0]) * s, p.y - (p.y - view[1]) * s, view[2] * s, view[3] * s];
    setView();
  }, {passive: false});
  var pan = null;
  function panMove(evt) {
    if (!pan) { return; }
    view[0] = pan.v[0] - (evt.clientX - pan.x) * pan.scale;
    view[1] = pan.v[1] - (evt.clientY - pan.y) * pan.scale;
    setView();
  }
  function panEnd() {
    pan = null; svg.classList.remove("dragging");
    window.removeEventListener("pointermove", panMove);
    window.removeEventListener("pointerup", panEnd);
    window.removeEventListener("pointercancel", panEnd);
  }
  svg.addEventListener("pointerdown", function (evt) {
    if (evt.target.tagName === "circle") { return; }
    evt.preventDefault();                                // no selection while panning either
    pan = {x: evt.clientX, y: evt.clientY, v: view.slice(), scale: 1 / svg.getScreenCTM().a};
    svg.classList.add("dragging");
    window.addEventListener("pointermove", panMove);    // as for a paper, through the window
    window.addEventListener("pointerup", panEnd);
    window.addEventListener("pointercancel", panEnd);
  });
})();
</script>
</body>
</html>
"""
