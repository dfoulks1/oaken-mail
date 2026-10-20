# SPDX-License-Identifier: Apache-2.0
from oaken.mail import html_to_text


def test_script_style_and_head_are_dropped() -> None:
    html = (
        "<html><head><title>T</title><style>p{}</style></head>"
        "<body><script>x()</script><p>Hi</p></body></html>"
    )
    assert html_to_text(html) == "Hi"


def test_hidden_preheaders_are_dropped() -> None:
    for style in (
        "display:none",
        "display: none; max-height:0",
        "visibility:hidden",
        "mso-hide:all",
        "font-size:0",
        "opacity:0",
    ):
        assert html_to_text(f'<div style="{style}">secret</div><p>shown</p>') == "shown", style
    assert html_to_text("<p hidden>secret</p><p>shown</p>") == "shown"
    assert html_to_text('<div style="font-size:0.9em">kept</div>') == "kept"


def test_tracking_pixels_go_and_alt_text_stays() -> None:
    assert html_to_text('<p>a</p><img src="x" width="1" height="1">') == "a"
    assert html_to_text('<img src="x" style="width:1px;height:1px">') == ""
    assert html_to_text('<img src="logo.png" alt="Example Store">') == "[image: Example Store]"
    assert html_to_text('<img src="logo.png">') == ""


def test_links_keep_their_url_only_when_the_text_differs() -> None:
    assert html_to_text('<a href="https://e.com/x">the page</a>') == "the page (https://e.com/x)"
    assert html_to_text('<a href="https://e.com/x">https://e.com/x</a>') == "https://e.com/x"
    assert html_to_text('<a href="https://e.com/x/">e.com/x</a>') == "e.com/x"
    assert html_to_text('<a href="mailto:a@e.com">Alex</a>') == "Alex (a@e.com)"
    assert html_to_text('<a href="#top">up</a>') == "up"
    assert html_to_text('<a href="javascript:x()">click</a>') == "click"


def test_lists_keep_their_structure() -> None:
    assert html_to_text("<ul><li>a</li><li>b<ul><li>c</li></ul></li></ul>") == "- a\n- b\n  - c"
    assert html_to_text("<ol><li>x</li><li>y</li></ol>") == "1. x\n2. y"


def test_tables_become_rows_and_layout_tables_become_text() -> None:
    assert (
        html_to_text("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>")
        == "A | B\n1 | 2"
    )
    assert (
        html_to_text("<table><tr><td><table><tr><td>inner</td></tr></table></td></tr></table>")
        == "inner"
    )


def test_quotes_get_markers_so_the_cleaner_sees_them() -> None:
    assert html_to_text("<p>new</p><blockquote>old<br>more</blockquote>") == "new\n\n> old\n> more"
    assert html_to_text('<div class="gmail_quote"><blockquote>x</blockquote></div>').startswith(
        "> "
    )


def test_entities_and_nbsp() -> None:
    assert html_to_text("<p>a&nbsp;&amp;&nbsp;b &lt;c&gt;</p>") == "a & b <c>"


def test_paragraph_and_line_breaks() -> None:
    assert html_to_text("<p>one</p><p>two<br>three</p>") == "one\n\ntwo\nthree"


def test_preformatted_text_keeps_its_layout() -> None:
    assert html_to_text("<pre>a\n  b</pre>") == "a\n  b"


def test_broken_and_hostile_html_never_raises() -> None:
    assert html_to_text("") == ""
    assert html_to_text("<p>unclosed <b>bold <i>x") == "unclosed bold x"
    assert "deep" in html_to_text("<div>" * 5000 + "deep" + "</div>" * 5000)
    assert html_to_text("<<<>>> &#xFFFFFFFF; <a href=") is not None
