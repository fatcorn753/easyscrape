# easyscrape

URL と CSS セレクタを指定するだけで、Web ページから情報を CSV / JSON に取り出す CLI。

## 使い方

```sh
# 記事一覧を取得（li.article ごとに 1 行）
uv run easyscrape https://example.com/news --item 'li.article' \
  --field 'title=h2' --field 'link=a@href' -o news.csv

# ページ全体から 1 行だけ取得（JSON で保存）
uv run easyscrape https://example.com --field 'title=title' -o page.json

# 対話モード
uv run easyscrape -i
```

- `名前=セレクタ` で項目を指定。末尾に `@属性名` を付けると属性値（例: `a@href`）、無ければテキスト。
- `--item` を指定すると、そのセレクタに一致した要素ごとに 1 行。省略するとページ全体で 1 行。
- 出力先の拡張子が `.json` なら JSON、それ以外は CSV。`--format` で明示も可。
- 出力先が既にあれば上書きしない。上書きするときは `--force`。
- 複数 URL を渡すと、各行に `url` 列が付く。

## 安全のための仕組み

- `robots.txt` を確認し、禁止された URL は取得しない（取得できない場合は制限なしとして扱う）。
- 同じホストへの連続アクセスは `--delay`（既定 1 秒）以上の間隔を空ける。
- User-Agent は `easyscrape/0.1 (personal scraping tool)`。
- ログイン突破・CAPTCHA 回避・アクセス制限の迂回は実装しない。
- メールアドレスや電話番号らしき値を検出したら警告を出す（取得は止めない）。

取得先の利用規約と、取得データの取り扱い（個人情報を含む場合は特に）は利用者の責任で確認してください。

## 開発

```sh
uv sync
uv run pytest
```

動的に描画されるページ（JavaScript 実行が必要なもの）は対象外。
