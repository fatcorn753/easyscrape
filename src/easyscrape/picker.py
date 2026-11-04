"""ページ上でクリックして、取り出す範囲（セレクタ）を選ぶための表示部品。

取得したページは信用しない前提で扱う:
- <script> など実行される要素と on* 属性はすべて取り除く
- 表示は sandbox 付き iframe（スクリプトは許可するが、同一オリジンは許可しない）で行う
- 本来のスクリプトを消したうえで、選択用のスクリプトだけを差し込む
"""

from __future__ import annotations

from bs4 import BeautifulSoup

# 実行・埋め込み・画面遷移につながる要素
REMOVE_TAGS = ["script", "noscript", "iframe", "frame", "frameset", "object", "embed", "applet", "base"]
URL_ATTRS = ("href", "src", "action", "formaction", "xlink:href")

# 差し込むスクリプトとスタイルは、取得したページの要素と区別できるよう固定の id を付ける
FRAME_STYLE = """
.easyscrape-item{outline:2px dashed #0a66c2 !important;outline-offset:-2px !important}
.easyscrape-match{outline:2px solid #34c759 !important;outline-offset:-2px !important}
#easyscrape-hover{position:fixed;pointer-events:none;border:2px solid #ff9500;background:rgba(255,149,0,.12);z-index:2147483647;display:none}
"""

# 親画面（フォーム）とのやり取りは postMessage のみ。
# 送るのはセレクタと短い文字列だけで、ページの内容を外へ出すことはない。
PICKER_JS = r"""
(function () {
  "use strict";
  var item = null;

  var overlay = document.createElement("div");
  overlay.id = "easyscrape-hover";

  function send(msg) {
    msg.source = "easyscrape";
    window.parent.postMessage(msg, "*");
  }

  // 取得したページのクラスだけを使う（easyscrape-* は選択用の印なので除く）
  function goodClass(c) {
    return /^[A-Za-z_][\w-]*$/.test(c) && c.indexOf("easyscrape-") !== 0;
  }

  // 要素の種類とクラス（最大 2 つ）。例: span.c-tag
  function tagSel(el) {
    var sel = el.tagName.toLowerCase();
    var classes = Array.prototype.filter.call(el.classList, goodClass).slice(0, 2);
    return sel + classes.map(function (c) { return "." + c; }).join("");
  }

  // root 配下での位置を nth-of-type で表す（最後の手段）
  function pathFrom(root, el) {
    var parts = [];
    var node = el;
    while (node && node !== root && node.nodeType === 1) {
      var seg = node.tagName.toLowerCase();
      var parent = node.parentElement;
      if (parent) {
        var same = Array.prototype.filter.call(parent.children, function (c) {
          return c.tagName === node.tagName;
        });
        if (same.length > 1) seg += ":nth-of-type(" + (same.indexOf(node) + 1) + ")";
      }
      parts.unshift(seg);
      node = parent;
    }
    return parts.join(" > ");
  }

  // root.querySelector(sel) が el を指すような、いちばん短いセレクタを返す
  function selectorFor(root, el) {
    var cands = [];
    if (el.id && goodClass(el.id)) cands.push("#" + el.id);
    cands.push(tagSel(el));
    for (var i = 0; i < cands.length; i++) {
      if (root.querySelector(cands[i]) === el) return cands[i];
    }
    var path = pathFrom(root === document ? null : root, el);
    if (path && root.querySelector(path) === el) return path;
    return null;
  }

  // 繰り返し要素（一覧の 1 件分）を推定する。
  // 近い祖先（6 段まで）のうち、ページ内で繰り返されていて、
  // 各要素の中にクリックした種類の要素がちょうど 1 つだけ入るものを候補にする。
  // 候補が複数あれば最も外側（一覧の 1 件分として大きい方）を選ぶ。
  // 例: a → 2 列の div → li → ul（li が 6 件入る）→ 外側の 1 件分は li。
  function itemFor(el) {
    var key = tagSel(el);
    var best = null;
    var n = el;
    for (var depth = 0; depth < 6 && n && n !== document.body && n !== document.documentElement; depth++) {
      var cand = tagSel(n);
      var matches = document.querySelectorAll(cand);
      if (matches.length >= 2) {
        var good = Array.prototype.filter.call(matches, function (m) {
          var inside = m.querySelectorAll(key).length + (m.matches(key) ? 1 : 0);
          return inside === 1;
        }).length;
        // 見出しだけ等の例外が数件あっても候補にする（8 割以上が条件を満たすこと）
        if (good / matches.length >= 0.8) best = { sel: cand, count: matches.length };
      }
      n = n.parentElement;
    }
    return best;
  }

  function scopeOf(el) {
    if (!item) return document;
    return el.closest(item) || null;
  }

  function fieldCount(sel) {
    if (!item) return document.querySelectorAll(sel).length;
    var items = document.querySelectorAll(item);
    return Array.prototype.filter.call(items, function (it) {
      return it.querySelector(sel) !== null;
    }).length;
  }

  function preview(el) {
    return (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40);
  }

  function clearMatches() {
    Array.prototype.forEach.call(document.querySelectorAll(".easyscrape-match"), function (n) {
      n.classList.remove("easyscrape-match");
    });
  }

  function highlightItems() {
    Array.prototype.forEach.call(document.querySelectorAll(".easyscrape-item"), function (n) {
      n.classList.remove("easyscrape-item");
    });
    if (!item) return;
    Array.prototype.forEach.call(document.querySelectorAll(item), function (n) {
      n.classList.add("easyscrape-item");
    });
  }

  function onMove(e) {
    var el = e.target;
    if (!(el instanceof Element) || el === overlay) return;
    var r = el.getBoundingClientRect();
    overlay.style.display = "block";
    overlay.style.left = r.left + "px";
    overlay.style.top = r.top + "px";
    overlay.style.width = r.width + "px";
    overlay.style.height = r.height + "px";
  }

  function onClick(e) {
    e.preventDefault();
    e.stopPropagation();
    var el = e.target;
    if (!(el instanceof Element) || el === overlay) return;

    clearMatches();
    var msg = { type: "pick", text: preview(el) };

    var owner = scopeOf(el);
    if (item && !owner) {
      msg.outside = true;
    } else {
      var root = owner || document;
      msg.field = selectorFor(root, el);
      if (msg.field) msg.count = fieldCount(msg.field);
      var anchor = el.closest("a[href]");
      if (anchor && root.contains(anchor)) {
        msg.anchor = selectorFor(root, anchor);
      }
      if (msg.field) {
        var first = root.querySelector(msg.field);
        if (first) first.classList.add("easyscrape-match");
      }
    }

    var found = itemFor(el);
    if (found) msg.item = { sel: found.sel, count: found.count };

    send(msg);
  }

  function setItem(sel) {
    item = sel || null;
    try {
      highlightItems();
    } catch (err) {
      // 入力中の不完全なセレクタでは、ハイライトだけ止めて動作を続ける
      item = null;
      send({ type: "error", message: "1 件分の要素のセレクタが正しくありません" });
    }
  }

  function init() {
    document.body.appendChild(overlay);
    document.addEventListener("mousemove", onMove, true);
    document.addEventListener("mouseleave", function () { overlay.style.display = "none"; }, true);
    document.addEventListener("click", onClick, true);
    window.addEventListener("message", function (e) {
      if (e.source !== window.parent) return;
      var d = e.data;
      if (!d || d.source !== "easyscrape-parent") return;
      if (d.type === "init") setItem(d.item);
    });
    send({ type: "ready" });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
"""


def sanitize(html: str, base_url: str) -> str:
    """取得したページを、表示専用の安全な形に変換する。"""
    soup = BeautifulSoup(html, "html.parser")

    for name in REMOVE_TAGS:
        for tag in soup.find_all(name):
            tag.decompose()

    # 外部 CSS は残す（見た目の確認のため）。それ以外の <link>（preload など）は除く
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel", []) or []).lower()
        if "stylesheet" not in rel:
            link.decompose()
    for meta in soup.find_all("meta"):
        meta.decompose()

    # フォームの送信で画面が遷移するのを防ぐため、タグだけ残して中身を残す
    for form in soup.find_all("form"):
        form.unwrap()

    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            lowered = attr.lower()
            if lowered.startswith("on"):
                del tag.attrs[attr]
                continue
            if lowered in URL_ATTRS:
                value = str(tag.attrs[attr]).strip().lower()
                if value.startswith("javascript:") or value.startswith("data:text/html"):
                    del tag.attrs[attr]

    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        soup.insert(0, head)
    base = soup.new_tag("base", href=base_url)
    head.insert(0, base)

    style = soup.new_tag("style")
    style.string = FRAME_STYLE
    head.append(style)

    script = soup.new_tag("script")
    script.string = PICKER_JS
    (soup.body or soup).append(script)

    return str(soup)


# 表示用の iframe に付ける CSP。スクリプトは選択用のものだけ、通信・フレームは不可
FRAME_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src * 'unsafe-inline'; "
    "img-src * data: blob:; font-src *; media-src *"
)
