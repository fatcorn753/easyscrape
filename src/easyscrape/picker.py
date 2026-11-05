"""ページ上でクリックして、取り出す範囲（セレクタ）を選ぶための表示部品。

取得したページは信用しない前提で扱う:
- <script> など実行される要素と on* 属性はすべて取り除く
- 表示は sandbox 付き iframe（スクリプトは許可するが、同一オリジンは許可しない）で行う
- 本来のスクリプトを消したうえで、選択用のスクリプトだけを差し込む
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup, Tag

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


# ---- 自動提案: ページから一覧（繰り返し要素）と取り出せる項目を推定する ----

SUGGEST_MAX_FIELDS = 8
MIN_TEXT_LEN = 3
_TEXT_TAGS = {"span", "p", "td", "th", "strong", "em", "time", "dd", "dt", "li", "div", "small", "label", "b"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_CLASS_OK = re.compile(r"^[A-Za-z_][\w-]*$")


_CHROME_TAGS = {"nav", "header", "footer", "aside"}
_CHROME_CLASS = re.compile(r"nav|menu|footer|header|sidebar|breadcrumb|gnav|lnav|share", re.IGNORECASE)


def _in_chrome(el: Tag) -> bool:
    """ナビゲーション・ヘッダー・フッター等の中か（一覧の本体ではないことが多い）。"""
    for node in [el, *el.parents]:
        if not isinstance(node, Tag):
            continue
        if node.name in _CHROME_TAGS:
            return True
        ident = " ".join(node.get("class") or []) + " " + (node.get("id") or "")
        if _CHROME_CLASS.search(ident):
            return True
    return False


def _sig(el: Tag) -> str:
    classes = [c for c in (el.get("class") or []) if _CLASS_OK.match(c) and not c.startswith("easyscrape-")][:2]
    return el.name + "".join("." + c for c in classes)


def _value(node: Tag | None, attr: str | None) -> str:
    if node is None:
        return ""
    if attr is None:
        return node.get_text(" ", strip=True)
    value = node.get(attr, "")
    if isinstance(value, list):
        value = " ".join(value)
    return str(value).strip()


def _position_path(root: Tag, el: Tag) -> str:
    """root から el までを tag:nth-of-type で表す（位置ベースの最後の手段）。"""
    parts = []
    node = el
    while node is not None and node is not root and isinstance(node, Tag):
        parent = node.parent
        seg = node.name
        if isinstance(parent, Tag):
            same = [c for c in parent.find_all(node.name, recursive=False)]
            if len(same) > 1:
                seg += f":nth-of-type({same.index(node) + 1})"
        parts.insert(0, seg)
        node = parent if isinstance(parent, Tag) else None
    return " > ".join(parts)


def _coverage(members: list[Tag], selector: str, attr: str | None) -> tuple[int, str]:
    hits = 0
    preview = ""
    for member in members:
        try:
            node = member.select_one(selector)
        except Exception:
            return 0, ""
        value = _value(node, attr)
        if value:
            hits += 1
            if not preview:
                preview = value[:40]
    return hits, preview


def _field_candidates(first: Tag) -> list[tuple[str, str | None, Tag]]:
    """先頭の 1 件から、取り出せそうな要素を (種類, 属性, 要素) で列挙する。"""
    found = []
    for el in first.find_all(True)[:300]:
        if el.name in _HEADING_TAGS and el.get_text(strip=True):
            found.append(("title", None, el))
        elif el.name == "a" and el.get("href") and el.get_text(strip=True):
            found.append(("link", "href", el))
            found.append(("title", None, el))
        elif el.name == "img" and el.get("src"):
            found.append(("image", "src", el))
        elif el.name in _TEXT_TAGS and el.get_text(strip=True):
            # 文字を直接持つ葉の要素だけ（入れ子の要素が文字を持つなら重複するので除く）
            if not any(c.get_text(strip=True) for c in el.find_all(True, recursive=False)):
                found.append(("text", None, el))
    return found


SUGGEST_MAX_CANDIDATES = 3
RANK_MIN_COUNT_RATIO = 0.25


def _kinds(candidate: dict) -> int:
    """候補の項目の種類数（title / link / image / text）。"""
    return len({re.sub(r"\d+$", "", f["name"]) for f in candidate["fields"]})


def _fields_for(members: list[Tag]) -> list[dict]:
    """一覧の見本 1 件から、全件で取れる項目を候補として返す。"""
    # 見出し行のように中身が少ない件は避け、文字が最も多い件を見本にする
    first = max(members, key=lambda m: len(m.get_text(strip=True)))
    fields: list[dict] = []
    used: set[tuple[str, str | None]] = set()
    shown: set[tuple[int, str]] = set()
    counters: dict[str, int] = {}
    for kind, attr, el in _field_candidates(first):
        selector = None
        for cand in (_sig(el), el.name, _position_path(first, el)):
            if not cand or (cand, attr) in used:
                continue
            hits, _ = _coverage(members, cand, attr)
            if hits >= max(1, int(len(members) * 0.8)):
                selector = cand
                break
        if selector is None:
            continue
        used.add((selector, attr))
        hits, preview = _coverage(members, selector, attr)
        # 同じ値を取る別の候補（親子で同じ文字を指すもの）は 1 つにまとめる
        if (hits, preview) in shown:
            continue
        shown.add((hits, preview))
        counters[kind] = counters.get(kind, 0) + 1
        name = kind if counters[kind] == 1 and kind != "text" else f"{kind}{counters[kind]}"
        fields.append({
            "name": name,
            "selector": selector,
            "attr": attr,
            "preview": preview,
            "coverage": hits,
        })
        if len(fields) >= SUGGEST_MAX_FIELDS:
            break
    return fields


EMBEDDED_MAX_BYTES = 5_000_000
EMBEDDED_MAX_FIELDS = 12
_KEY_OK = re.compile(r"^[^=@\s.]+$")  # FieldSpec や JSON のパス区切りと衝突する文字を含むキーは使わない


def _scalar(value: object) -> bool:
    return isinstance(value, (str, int, float, bool))


def _record_lists(data: object, path: str = "", depth: int = 0, budget: list[int] | None = None):
    """JSON の中の「オブジェクトが並ぶ配列」を (パス, 配列) で列挙する。"""
    if budget is None:
        budget = [20000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 8:
        return
    if isinstance(data, list):
        if len(data) >= 2 and all(isinstance(x, dict) for x in data[:50]):
            yield path, data
        for index, item in enumerate(data[:5]):
            yield from _record_lists(item, f"{path}.{index}" if path else str(index), depth + 1, budget)
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str) and "." not in key:
                yield from _record_lists(value, f"{path}.{key}" if path else key, depth + 1, budget)


def _json_fields(records: list[dict]) -> list[dict]:
    """配列の各要素から、全体の 8 割以上で値がある項目（1 段下までの入れ子を含む）を返す。"""
    order: list[str] = []
    for record in records[:50]:
        for key, value in record.items():
            if not isinstance(key, str) or not _KEY_OK.match(key):
                continue
            paths = [key] if _scalar(value) else (
                [f"{key}.{k}" for k, v in value.items() if isinstance(k, str) and _KEY_OK.match(k) and _scalar(v)]
                if isinstance(value, dict) else []
            )
            for path in paths:
                if path not in order:
                    order.append(path)
    fields: list[dict] = []
    names: set[str] = set()
    sample = records[:200]
    for path in order:
        hits, preview = 0, ""
        for record in sample:
            value = record
            for part in path.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if _scalar(value) and str(value) != "":
                hits += 1
                if not preview:
                    preview = str(value)[:40]
        if hits < max(1, int(len(sample) * 0.8)):
            continue
        name = path.rsplit(".", 1)[-1]
        if name in names:
            name = path.replace(".", "_")
        if name in names:
            continue
        names.add(name)
        fields.append({"name": name, "selector": path, "attr": None, "preview": preview, "coverage": hits})
        if len(fields) >= EMBEDDED_MAX_FIELDS:
            break
    return fields


def _embedded_analysis(node: Tag) -> dict | None:
    """埋め込み JSON の中から、一覧に使えそうな配列（場所と項目）を見つける。"""
    text = node.string or node.get_text()
    if not text or len(text) > EMBEDDED_MAX_BYTES:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    best = None
    for path, records in _record_lists(data):
        fields = _json_fields(records)
        if len(fields) < 2:
            continue
        # 件数と項目数が多い配列ほど、本体の一覧である可能性が高い
        score = (len(fields), len(records))
        if best is None or score > best[0]:
            best = (score, path, records, fields)
    if best is None:
        return None
    _, path, records, fields = best
    return {"records": path, "count": len(records), "fields": fields}


def _embedded_candidates(soup: BeautifulSoup) -> list[dict]:
    """埋め込み JSON の候補。場所（script のセレクタ）、配列の場所、項目をまとめて返す。"""
    found: list[dict] = []
    seen: set[str] = set()
    for script in soup.find_all("script"):
        script_id = script.get("id")
        if script_id and _CLASS_OK.match(script_id):
            sel = f"script#{script_id}"
        elif (script.get("type") or "").lower() == "application/ld+json":
            sel = 'script[type="application/ld+json"]'
        else:
            continue
        if sel in seen:
            continue
        seen.add(sel)
        # 取得時は select_one で最初の 1 つを読むので、解析も同じ要素で行う
        node = soup.select_one(sel)
        analysis = _embedded_analysis(node) if node is not None else None
        if analysis is None:
            continue  # 一覧に使える配列が無い埋め込みデータは出さない
        found.append({"selector": sel, **analysis})
        if len(found) >= 3:
            break
    return found


def suggest(html: str) -> dict:
    """一覧の候補（繰り返し要素）と、各候補で取り出せる項目を返す。

    一覧の候補: 同じ親の下に 3 件以上並ぶ要素のうち、件名らしい文字がある件数が多いもの。
    ナビゲーション・ヘッダー・フッターの中は対象外。上位 SUGGEST_MAX_CANDIDATES 件を返す。
    """
    soup = BeautifulSoup(html, "html.parser")
    root = soup.body or soup

    scored: list[tuple[int, str, list[Tag]]] = []
    seen: set[str] = set()
    for parent in [root, *root.find_all(True)]:
        if _in_chrome(parent):
            continue
        groups: dict[str, list[Tag]] = {}
        for child in parent.find_all(recursive=False):
            groups.setdefault(_sig(child), []).append(child)
        for sig, members in groups.items():
            if len(members) < 3 or sig in seen:
                continue
            seen.add(sig)
            # 件数はナビゲーション等の外側にある件だけで数える
            total = [m for m in soup.select(sig) if not _in_chrome(m)]
            doc_filled = sum(1 for m in total if len(m.get_text(strip=True)) >= MIN_TEXT_LEN)
            if doc_filled >= 3:
                scored.append((doc_filled, sig, total))

    scored.sort(key=lambda x: x[0], reverse=True)
    embedded = _embedded_candidates(soup)
    ranked = []
    for _, sig, members in scored[:SUGGEST_MAX_CANDIDATES * 3]:
        fields = _fields_for(members)
        if fields:  # 取り出せる項目が無い候補は出さない
            ranked.append({"item": sig, "count": len(members), "fields": fields})
    # 件数が多くても項目が少ないもの（ナビや補助リスト）は後ろへ。
    # 項目の種類（見出し・リンク・文字・画像）が多い候補を先に出す。件数が極端に少ないものは除く。
    if ranked:
        floor = max(ranked, key=lambda c: c["count"])["count"] * RANK_MIN_COUNT_RATIO
        ranked.sort(key=lambda c: (c["count"] >= floor, _kinds(c)), reverse=True)
    return {"candidates": ranked[:SUGGEST_MAX_CANDIDATES], "embedded": embedded}
