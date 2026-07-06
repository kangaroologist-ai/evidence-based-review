#!/usr/bin/env python3
"""md_to_html.py — render a review.md into a self-contained, nicely-styled HTML.

Why this exists: several Markdown viewers render block math (``$$…$$``) but NOT
inline math (``$…$``), and don't resolve relative image paths — so a review full
of inline ``$\\delta$`` and an ``![](figures/x.svg)`` shows raw ``$…$`` text and a
broken image. This tool produces one standalone .html that:

  * renders BOTH inline ``$…$`` and display ``$$…$$`` via MathJax 3 (CJK-safe);
  * inlines local figures (SVG inlined directly; raster images base64-embedded)
    so the file is portable and the image always loads;
  * turns inline ``[@key]`` markers into APA in-text citations ``(Author, Year)``
    that hyperlink to the matching entry in the References section (anchors are
    injected by exact DOI match, read from ``<topic>/references/*.json``);
  * ships a floating reading panel (font family / size / line-height / width),
    persisted to localStorage;
  * applies a modern, minimal stylesheet (web fonts + CJK fallback, narrow
    reading column, styled tables / code / blockquotes / figures, dark mode).

Usage:
    python scripts/md_to_html.py <review.md> [-o out.html] [--title "..."]
    # default output: <same dir>/<same stem>.html

Network note: MathJax and the default web fonts load from CDN (cached after first
open); offline, math still typesets from cache and fonts fall back to the system
stack. The math is protected with placeholder tokens BEFORE the Markdown pass (so
that ``_ * \\`` inside formulae survive), then restored verbatim afterwards.
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import re
import sys

import markdown as md  # python-markdown (3.x)

# Private-use code points: Markdown passes them through untouched and they carry
# no HTML meaning, so they make collision-free placeholders.
_PH_OPEN, _PH_CLOSE = "", ""


# ───────────────────────── math protection ─────────────────────────
def _protect_math(text: str) -> tuple[str, list[str]]:
    spans: list[str] = []

    def stash(match: re.Match) -> str:
        spans.append(match.group(0))
        return f"{_PH_OPEN}{len(spans) - 1}{_PH_CLOSE}"

    text = re.sub(r"\$\$.+?\$\$", stash, text, flags=re.DOTALL)   # display first
    text = re.sub(r"\$(?!\$)[^$\n]+?\$", stash, text)             # then inline
    return text, spans


def _restore_math(html: str, spans: list[str]) -> str:
    return re.sub(rf"{_PH_OPEN}(\d+){_PH_CLOSE}",
                  lambda m: spans[int(m.group(1))], html)


# ───────────────────────── citations ─────────────────────────
def _load_refs(base_dir: pathlib.Path) -> dict[str, dict]:
    """key -> {surnames:[...], year:str, doi:str} from <topic>/references/*.json."""
    refs: dict[str, dict] = {}
    rdir = base_dir / "references"
    if not rdir.is_dir():
        return refs
    for f in sorted(rdir.glob("*.json")):
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        key = e.get("citation_key")
        if not key:
            continue
        authors = e.get("authors") or []
        if isinstance(authors, str):
            authors = re.split(r"\s*;\s*", authors)
        surnames = []
        for a in authors:
            a = (a or "").strip()
            if not a:
                continue
            surnames.append(a.split(",")[0].strip() if "," in a else a.split()[-1])
        refs[key] = {
            "surnames": surnames,
            "year": str(e.get("year") or "").strip(),
            "doi": (e.get("doi") or "").strip().lower(),
        }
    return refs


def _intext_authors(surnames: list[str]) -> str:
    if not surnames:
        return "Anon."
    if len(surnames) == 1:
        return surnames[0]
    if len(surnames) == 2:
        return f"{surnames[0]} & {surnames[1]}"
    return f"{surnames[0]} et al."


def _render_citations(html: str, refs: dict[str, dict]) -> str:
    """Replace runs of [@key]([@key2]…) with ONE APA parenthetical whose
    author-year segments each link to #ref-<key>.

    The source prose was written as ``…（May 1976）[@may1976eee2]`` — a manual
    author-year parenthetical immediately followed by the marker (to satisfy the
    lint's author/year-near-citation rule). To avoid a doubled ``(May 1976)(May,
    1976)`` we ABSORB that immediately-preceding parenthetical and replace the
    whole thing with a single canonical APA in-text link."""
    def repl(match: re.Match) -> str:
        keys = re.findall(r"\[@([^\]]+)\]", match.group(0))
        segs = []
        for k in keys:
            r = refs.get(k)
            if not r:
                segs.append(k)  # unknown key: show raw-ish, no link
                continue
            label = f"{_intext_authors(r['surnames'])}, {r['year']}".strip().rstrip(",")
            segs.append(f'<a class="cite" href="#ref-{k}">{label}</a>')
        return "(" + "; ".join(segs) + ")"

    # optional preceding (…)/（…） citation parenthetical + the [@key] run
    return re.sub(r"(?:[（(][^（）()]{0,80}?[）)])?\s*(?:\[@[^\]]+\])+", repl, html)


def _anchor_refs(html: str, refs: dict[str, dict]) -> str:
    """Give each References <p> an id=ref-<key>, matched by exact DOI substring."""
    for key, r in refs.items():
        doi = r.get("doi")
        if not doi:
            continue
        pat = re.compile(
            r"(<p)((?:(?!</p>).)*?doi\.org/" + re.escape(doi) + r")",
            re.DOTALL | re.IGNORECASE,
        )
        html, n = pat.subn(rf'\1 id="ref-{key}"\2', html, count=1)
    return html


# ───────────────────────── figures ─────────────────────────
def _inline_images(html: str, base_dir: pathlib.Path) -> str:
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp"}

    def repl(match: re.Match) -> str:
        tag, src = match.group(0), match.group("src")
        if src.startswith(("http://", "https://", "data:")):
            return tag
        path = (base_dir / src).resolve()
        if not path.is_file():
            return tag
        if path.suffix.lower() == ".svg":
            svg = path.read_text(encoding="utf-8")
            svg = re.sub(r"<\?xml.*?\?>", "", svg, flags=re.DOTALL)
            svg = re.sub(r"<!DOCTYPE.*?>", "", svg, flags=re.DOTALL)
            return f'<span class="fig">{svg.strip()}</span>'
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        m = mime.get(path.suffix.lower(), "application/octet-stream")
        alt = re.search(r'alt="([^"]*)"', tag)
        return (f'<span class="fig"><img alt="{alt.group(1) if alt else ""}" '
                f'src="data:{m};base64,{data}"></span>')

    return re.sub(r'<img[^>]*\bsrc="(?P<src>[^"]+)"[^>]*>', repl, html)


# ───────────────────────── presentation ─────────────────────────
_CSS = r"""
:root{
  --font-body:'Inter','MiSans','Noto Sans SC',-apple-system,BlinkMacSystemFont,'Segoe UI',
    'PingFang SC','Microsoft YaHei','Noto Sans CJK SC',sans-serif;
  --font-size:17px; --leading:1.8; --measure:50rem;
  --bg:#fbfbfc; --fg:#23272f; --muted:#727a86; --rule:#e8eaef;
  --accent:#0d7d74; --accent-soft:#e6f3f1; --code-bg:#f1f3f6; --th-bg:#f5f7f9;
  --shadow:0 1px 2px rgba(20,30,50,.05),0 10px 34px rgba(20,30,50,.07);
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#15171c; --fg:#d9dce3; --muted:#8b94a3; --rule:#272b33;
    --accent:#5ad4c8; --accent-soft:#163029; --code-bg:#1d2027; --th-bg:#1b1e25;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 12px 34px rgba(0,0,0,.4);
  }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--bg); color:var(--fg);
  font-family:var(--font-body); font-size:var(--font-size); line-height:var(--leading);
  letter-spacing:.05px; font-feature-settings:"kern" 1,"liga" 1; -webkit-font-smoothing:antialiased;
}
.wrap{max-width:var(--measure); margin:0 auto; padding:4.5rem 1.5rem 7rem; transition:max-width .15s ease}
.doc>*:first-child{margin-top:0}
h1,h2,h3,h4{line-height:1.32; font-weight:680; margin:2.4em 0 .7em; text-wrap:balance}
h1{font-size:1.95rem; font-weight:760; margin:0 0 1.1em; letter-spacing:-.01em}
h2{font-size:1.35rem; padding-bottom:.34em; border-bottom:1px solid var(--rule); margin-top:2.7em}
h3{font-size:1.1rem; color:var(--accent)}
p{margin:1.05em 0}
a{color:var(--accent); text-decoration:none; border-bottom:1px solid var(--accent-soft)}
a:hover{border-bottom-color:var(--accent)}
strong{font-weight:680}
hr{border:0; border-top:1px solid var(--rule); margin:2.6em 0}
ul,ol{padding-left:1.35em; margin:1.05em 0}
li{margin:.34em 0} li::marker{color:var(--accent)}
blockquote{margin:1.4em 0; padding:.25em 1.15em; color:var(--muted);
  border-left:3px solid var(--accent); background:var(--accent-soft); border-radius:0 8px 8px 0}
code{font-family:'SF Mono',ui-monospace,'JetBrains Mono',Menlo,Consolas,monospace;
  font-size:.85em; background:var(--code-bg); padding:.12em .4em; border-radius:5px}
pre{background:var(--code-bg); padding:1em 1.15em; border-radius:10px; overflow-x:auto; line-height:1.55}
pre code{background:none; padding:0; font-size:.84em}
table{border-collapse:collapse; width:100%; margin:1.6em 0; font-size:.94rem;
  box-shadow:var(--shadow); border-radius:10px; overflow:hidden}
th,td{padding:.62em .85em; text-align:left; border-bottom:1px solid var(--rule); vertical-align:top}
thead th{background:var(--th-bg); font-weight:660; border-bottom:1.5px solid var(--rule)}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--accent-soft)}
.fig{display:block; margin:1.9em auto; text-align:center}
.fig svg,.fig img{max-width:100%; height:auto; border-radius:10px}
.fig + p{font-size:.9rem; color:var(--muted); text-align:center; margin-top:-.4em; line-height:1.6}
.cite{white-space:nowrap; font-size:.95em; border-bottom:1px dotted var(--accent-soft)}
.cite:hover{border-bottom-style:solid}
:target{scroll-margin-top:1.4rem; background:var(--accent-soft); border-radius:7px;
  box-shadow:0 0 0 .45em var(--accent-soft)}
mjx-container[display="true"]{overflow-x:auto; overflow-y:hidden; padding:.35em 0; max-width:100%}
mjx-container{font-size:1.01em}
.footer{margin-top:5rem; padding-top:1.4rem; border-top:1px solid var(--rule);
  color:var(--muted); font-size:.82rem; text-align:center}
/* floating reading panel */
.rp{position:fixed; top:1rem; right:1rem; z-index:60; font-size:13px;
  font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif}
.rp-toggle{width:40px; height:40px; border-radius:11px; border:1px solid var(--rule);
  background:var(--bg); color:var(--fg); cursor:pointer; box-shadow:var(--shadow);
  font-weight:700; font-size:15px}
.rp-body{margin-top:.5rem; width:216px; padding:.55rem .85rem; border:1px solid var(--rule);
  border-radius:13px; background:color-mix(in srgb,var(--bg) 86%,transparent);
  box-shadow:var(--shadow); -webkit-backdrop-filter:blur(10px); backdrop-filter:blur(10px)}
.rp-row{display:flex; align-items:center; justify-content:space-between; margin:.55rem 0}
.rp-label{color:var(--muted)}
.rp-fonts{display:flex; gap:4px}
.rp-fonts button,.rp-step button{border:1px solid var(--rule); background:var(--bg); color:var(--fg);
  border-radius:8px; padding:.24em .55em; cursor:pointer; font-size:12px; line-height:1.3}
.rp-fonts button:hover,.rp-step button:hover{border-color:var(--accent)}
.rp-fonts button.on{background:var(--accent); color:#fff; border-color:var(--accent)}
.rp-step{display:flex; align-items:center; gap:9px}
.rp-step span{min-width:2.4em; text-align:center; color:var(--fg)}
@media print{.rp{display:none}}
"""

_PANEL = """
<div class="rp">
  <button class="rp-toggle" id="rpToggle" title="阅读设置" aria-label="阅读设置">Aa</button>
  <div class="rp-body" id="rpBody" hidden>
    <div class="rp-row"><span class="rp-label">字体</span>
      <div class="rp-fonts">
        <button data-font="sans">黑体</button><button data-font="serif">衬线</button>
        <button data-font="kai">文楷</button><button data-font="system">系统</button>
      </div></div>
    <div class="rp-row"><span class="rp-label">字号</span>
      <div class="rp-step"><button data-size="-1">A−</button><span id="rpSize">17</span><button data-size="1">A+</button></div></div>
    <div class="rp-row"><span class="rp-label">行距</span>
      <div class="rp-step"><button data-lh="-1">−</button><span id="rpLh">1.8</span><button data-lh="1">+</button></div></div>
    <div class="rp-row"><span class="rp-label">宽度</span>
      <div class="rp-step"><button data-w="-1">−</button><span id="rpW">50</span><button data-w="1">+</button></div></div>
  </div>
</div>
"""

_PANEL_JS = r"""
<script>
(function(){
  var F={
    sans:{s:"'Inter','MiSans','Noto Sans SC',-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif",
      css:null},
    serif:{s:"'Source Serif 4','Noto Serif SC',Georgia,'Songti SC',serif",
      css:"https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Noto+Serif+SC:wght@400;600;700&display=swap"},
    kai:{s:"'LXGW WenKai','Noto Serif SC',Georgia,serif",
      css:"https://cdn.jsdelivr.net/npm/lxgw-wenkai-webfont@1.7.0/style.css"},
    system:{s:"-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif",css:null}
  };
  var done={}, R=document.documentElement;
  function css(u){ if(!u||done[u])return; done[u]=1;
    var l=document.createElement('link'); l.rel='stylesheet'; l.href=u; document.head.appendChild(l); }
  var st={}; try{ st=JSON.parse(localStorage.getItem('rp')||'{}'); }catch(e){}
  var size=st.size||17, lh=st.lh||1.8, w=st.w||50, font=st.font||'sans';
  function txt(id,v){ var el=document.getElementById(id); if(el)el.textContent=v; }
  function apply(){
    var f=F[font]||F.sans; css(f.css);
    R.style.setProperty('--font-body',f.s);
    R.style.setProperty('--font-size',size+'px');
    R.style.setProperty('--leading',lh);
    R.style.setProperty('--measure',w+'rem');
    txt('rpSize',size); txt('rpLh',lh.toFixed(2)); txt('rpW',w);
    var bs=document.querySelectorAll('.rp-fonts button');
    for(var i=0;i<bs.length;i++) bs[i].classList.toggle('on',bs[i].getAttribute('data-font')===font);
    try{ localStorage.setItem('rp',JSON.stringify({size:size,lh:lh,w:w,font:font})); }catch(e){}
  }
  function wire(){
    var t=document.getElementById('rpToggle'), b=document.getElementById('rpBody');
    if(t) t.onclick=function(){ b.hidden=!b.hidden; };
    function each(sel,fn){ var n=document.querySelectorAll(sel); for(var i=0;i<n.length;i++) fn(n[i]); }
    each('.rp-fonts button',function(el){ el.onclick=function(){ font=el.getAttribute('data-font'); apply(); }; });
    each('[data-size]',function(el){ el.onclick=function(){ size=Math.max(14,Math.min(24,size+ +el.getAttribute('data-size'))); apply(); }; });
    each('[data-lh]',function(el){ el.onclick=function(){ lh=Math.max(1.4,Math.min(2.4,+(lh+0.1*+el.getAttribute('data-lh')).toFixed(2))); apply(); }; });
    each('[data-w]',function(el){ el.onclick=function(){ w=Math.max(36,Math.min(72,w+2*+el.getAttribute('data-w'))); apply(); }; });
    apply();
  }
  if(document.readyState!=='loading') wire();
  else document.addEventListener('DOMContentLoaded',wire);
})();
</script>
"""

_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/misans@4.1.0/lib/Normal/MiSans-Regular.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/misans@4.1.0/lib/Normal/MiSans-Medium.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/misans@4.1.0/lib/Normal/MiSans-Bold.min.css">
<style>{css}</style>
<script>
window.MathJax = {{
  tex: {{ inlineMath: [['$','$'], ['\\\\(','\\\\)']],
          displayMath: [['$$','$$'], ['\\\\[','\\\\]']],
          processEscapes: true, tags: 'none' }},
  options: {{ skipHtmlTags: ['script','noscript','style','textarea','pre','code'] }}
}};
</script>
<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
</head>
<body>
{panel}
<main class="wrap"><article class="doc">
{body}
</article>
<div class="footer">{footer}</div></main>
{panel_js}
</body>
</html>
"""


def convert(md_text: str, base_dir: pathlib.Path, title: str) -> str:
    protected, spans = _protect_math(md_text)
    body = md.markdown(protected,
                       extensions=["tables", "fenced_code", "sane_lists", "attr_list"],
                       output_format="html5")
    body = _restore_math(body, spans)
    refs = _load_refs(base_dir)
    if refs:
        body = _render_citations(body, refs)
        body = _anchor_refs(body, refs)
    body = _inline_images(body, base_dir)
    footer = "由 tools/md_to_html.py 生成 · MathJax 公式 · 内联图 · 可调阅读设置"
    return _TEMPLATE.format(title=title, css=_CSS, body=body,
                            footer=footer, panel=_PANEL, panel_js=_PANEL_JS)


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a review.md to styled, math-rendering HTML.")
    ap.add_argument("md", help="path to the source .md (e.g. reviews/<topic>/review.md)")
    ap.add_argument("-o", "--out", help="output .html path (default: alongside the .md)")
    ap.add_argument("--title", help="page <title> (default: first H1, else file stem)")
    args = ap.parse_args()

    src = pathlib.Path(args.md).resolve()
    if not src.is_file():
        sys.exit(f"not a file: {src}")
    text = src.read_text(encoding="utf-8")

    title = args.title
    if not title:
        m = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
        title = (m.group(1).strip() if m else src.stem)

    out = pathlib.Path(args.out).resolve() if args.out else src.with_suffix(".html")
    out.write_text(convert(text, src.parent, title), encoding="utf-8")
    kb = out.stat().st_size / 1024
    print(f"[OK] wrote {out}  ({kb:.0f} KB)")
    print(f"     open: file://{out}")


if __name__ == "__main__":
    main()
