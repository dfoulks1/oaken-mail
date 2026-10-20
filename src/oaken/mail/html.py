# SPDX-License-Identifier: Apache-2.0
"""HTML to readable text, conservatively.

Dropped: script, style, head, hidden elements (preheaders), tracking pixels and images with no
alt text. Kept: links as ``text (url)`` only when the text differs from the URL, lists as
``- `` and ``1. ``, table rows as ``a | b``, and quotes as ``> `` lines, so the cleaner sees the
same quote markers it sees in plain text. Uses selectolax (lexbor); ``beautifulsoup4`` + ``lxml``
is the fallback if it ever needs replacing, and only this module would change.
"""

import re

from selectolax.lexbor import LexborHTMLParser, LexborNode

_DROP = frozenset(
    "script style head title noscript template svg meta link object embed iframe canvas "
    "video audio map math".split()
)
_BLOCK = frozenset(
    "p div h1 h2 h3 h4 h5 h6 ul ol li table tr blockquote pre hr section article header footer "
    "nav aside main address form fieldset center dl dt dd figure figcaption tbody thead tfoot "
    "details summary".split()
)
# Elements a mail client wraps around the quoted part of a reply.
_QUOTE_CLASSES = frozenset({"gmail_quote", "yahoo_quoted", "protonmail_quote", "moz-cite-prefix"})
_HIDDEN = re.compile(
    r"display:none|visibility:hidden|mso-hide:all|opacity:0(?![.\d])"
    r"|font-size:0(?:px|pt|em)?(?![.\d%a-z])|max-height:0(?:px|pt|em)?(?![.\d%a-z])"
)
_PIXEL = re.compile(r"(?:width|height):\s*[012]px")
_MAX_DEPTH = 120


def html_to_text(html: str) -> str:
    """Never raises; returns '' for input it cannot make sense of."""
    try:
        root = LexborHTMLParser(html).body
        if root is None:
            return ""
        text = _render(root, 0)
    except (RecursionError, ValueError):
        return ""
    return _tidy(text)


def _tidy(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+(?=\n)", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _hidden(node: LexborNode) -> bool:
    attrs = node.attributes
    if "hidden" in attrs:
        return True
    style = (attrs.get("style") or "").lower().replace(" ", "")
    return bool(style and _HIDDEN.search(style))


def _is_pixel(node: LexborNode) -> bool:
    attrs = node.attributes
    for key in ("width", "height"):
        value = (attrs.get(key) or "").strip().lower().removesuffix("px")
        if value.isdigit() and int(value) <= 2:
            return True
    return bool(_PIXEL.search((attrs.get("style") or "").lower()))


def _children(node: LexborNode) -> list[LexborNode]:
    out = []
    child = node.child
    while child is not None:
        out.append(child)
        child = child.next
    return out


def _render(node: LexborNode, depth: int) -> str:
    tag = node.tag
    if tag == "-text":
        return re.sub(r"[ \t\r\n\f]+", " ", node.text_content or "")
    if tag in ("-comment", "-undef") or tag in _DROP:
        return ""
    if depth > _MAX_DEPTH:
        return " ".join((node.text(deep=True) or "").split())
    if _hidden(node):
        return ""
    if tag == "br":
        return "\n"
    if tag == "img":
        alt = " ".join((node.attributes.get("alt") or "").split())
        return f"[image: {alt}]" if alt and not _is_pixel(node) else ""
    if tag == "pre":
        return f"\n\n{node.text(deep=True)}\n\n"
    if tag == "a":
        return _link(node, depth)
    if tag in ("ul", "ol"):
        return _list(node, depth, ordered=tag == "ol")
    if tag == "table":
        return _table(node, depth)

    inner = "".join(_render(c, depth + 1) for c in _children(node))
    classes = set((node.attributes.get("class") or "").split())
    if tag == "blockquote" or classes & _QUOTE_CLASSES:
        return "\n\n" + _quote(inner) + "\n\n"
    if node.attributes.get("id") in ("divRplyFwdMsg", "appendonsend"):
        return f"\n\n-----Original Message-----\n{inner}\n\n"
    if tag == "hr":
        return "\n\n"
    if tag in _BLOCK:
        return (
            f"\n\n{inner}\n\n"
            if tag in ("p", "div", "section", "article", "address") or (tag.startswith("h"))
            else f"\n{inner}\n"
        )
    return inner


def _quote(inner: str) -> str:
    lines = _tidy(inner).split("\n")
    return "\n".join("> " + line if line.strip() else ">" for line in lines)


def _norm_url(value: str) -> str:
    value = value.strip().lower()
    for scheme in ("https://", "http://", "mailto:"):
        value = value.removeprefix(scheme)
    return value.rstrip("/")


def _link(node: LexborNode, depth: int) -> str:
    text = " ".join("".join(_render(c, depth + 1) for c in _children(node)).split())
    href = (node.attributes.get("href") or "").strip()
    if not href or href.startswith(("#", "javascript:", "tel:")) or " " in href:
        return text
    if not text:
        return href
    if _norm_url(text) == _norm_url(href):
        return text
    return f"{text} ({href.removeprefix('mailto:')})"


def _list(node: LexborNode, depth: int, *, ordered: bool) -> str:
    lines: list[str] = []
    n = 0
    for child in _children(node):
        if child.tag != "li":
            text = _tidy(_render(child, depth + 1))
            if text:
                lines.append(text)
            continue
        n += 1
        body = _tidy("".join(_render(c, depth + 1) for c in _children(child)))
        body = re.sub(r"\n\n(?=\s*(?:- |\d+\. ))", "\n", body)  # a nested list hugs its item
        if not body:
            continue
        marker = f"{n}. " if ordered else "- "
        first, *rest = body.split("\n")
        lines.append(marker + first)
        lines.extend("  " + line if line else "" for line in rest)
    return "\n\n" + "\n".join(lines) + "\n\n" if lines else ""


def _table(node: LexborNode, depth: int) -> str:
    rows: list[str] = []
    for row in node.css("tr"):
        if _nearest_table(row) != node:
            continue  # a row of a nested table; its own table renders it
        cells = [c for c in _children(row) if c.tag in ("td", "th")]
        texts = [_tidy("".join(_render(x, depth + 1) for x in _children(c))) for c in cells]
        texts = [t for t in texts if t]
        if not texts:
            continue
        layout = len(texts) == 1 or any(c.css_first("table") is not None for c in cells)
        if layout:
            rows.append("\n".join(texts))
        else:
            rows.append(" | ".join(" ".join(t.split()) for t in texts))
    return "\n\n" + "\n".join(rows) + "\n\n" if rows else ""


def _nearest_table(node: LexborNode) -> LexborNode | None:
    parent = node.parent
    while parent is not None and parent.tag != "table":
        parent = parent.parent
    return parent
