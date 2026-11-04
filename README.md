# easyscrape

URL と取り出したい項目を指定するだけで、Web ページや公開 API から情報を CSV / JSON に取り出すツール。
ブラウザで使う画面（`easyscrape-ui`）と、コマンドライン（`easyscrape`）の 2 通り。

## かんたんに使う（ブラウザ画面）

```sh
uv run easyscrape-ui          # http://127.0.0.1:8790/ が開く
uv run easyscrape-ui 8791     # ポートを変える場合
```

1. 画面上の「サンプルを入れる」を押すか、URL と項目を入力する
   - 「ページ上でクリックして選ぶ」: URL を入れて「読み込む」→ 取り出したい場所をクリックすると、項目欄に自動で入る。「1 件分の要素」「文字」「リンク先」を選べる
2. 「取得する」を押す。結果が表で表示される
3. 「CSV で保存」または「JSON で保存」を押す

画面は自分のパソコンの中（127.0.0.1）だけで動き、外部からは見えない。
クリック選択で表示するページは、スクリプトを外し、サンドボックス付きの枠の中で表示する。

## コマンドラインで使う

```sh
# 一覧ページから 1 件ごとに 1 行（項目は CSS セレクタ。@属性 で属性値）
uv run easyscrape https://www.env.go.jp/press/index.html \
  --item 'li.c-news-link__item' \
  --field 'dept=span.c-tag' --field 'title=a.c-news-link__link' --field 'link=a.c-news-link__link@href' \
  -o press.csv

# JSON の API（POST と配列の位置を指定）
uv run easyscrape https://www.kadokawa.co.jp/product/search/ \
  --header 'X-Requested-With: XMLHttpRequest' \
  --data mgenre=51 --data releaseDate=1 --data size=20 --data pageno=1 --data pageno_book=1 \
  --records result.all --field title=title --field person=person \
  -o shinkan.csv

# 対話モード
uv run easyscrape -i
```

- 出力先の拡張子が `.json` なら JSON、それ以外は CSV。`--format` で明示も可
- 出力先が既にあれば上書きしない。上書きするときは `--force`
- 複数 URL を渡すと、各行に `url` 列が付く
- 相対リンク（`/a/b.html`）は、取得元のページを基準に絶対 URL に直す

## 安全のための仕組み

- `robots.txt` を確認し、禁止された URL は取得しない（取得できない場合は制限なしとして扱う）
- 同じホストへの連続アクセスは `--delay`（既定 1 秒、画面では 1 秒未満にできない）以上の間隔。`robots.txt` の `Crawl-delay` があればそちらを使う
- User-Agent は `easyscrape/0.1 (personal scraping tool)`
- ログイン突破・CAPTCHA 回避・アクセス制限の迂回は実装しない
- メールアドレスや電話番号らしき値を検出したら警告を出す（取得は止めない）
- 文字コードは Content-Type や `<meta charset>` から判定（Shift_JIS のページに対応）

取得先の利用規約と、取得データの取り扱い（個人情報を含む場合は特に）は利用者の責任で確認してください。

## テスト結果（2026-11-04）

公開の行政情報の一覧だけを使用。

| 対象 | 形式 | 結果 |
|---|---|---|
| 総務省 報道資料一覧（https://www.soumu.go.jp/menu_news/s-news/index.html） | HTML（Shift_JIS、表形式） | 16 行。発表日・タイトル・リンク（絶対 URL）・担当部局を取得 |
| 環境省 報道発表一覧（https://www.env.go.jp/press/index.html） | HTML（リスト形式） | 122 行。担当部局・タイトル・リンクを取得 |

確認できたこと: 文字化けなし、相対リンクの絶対化、取得失敗（存在しないホスト）時はその URL だけスキップして結果を表示。

## 開発

```sh
uv sync
uv run pytest
```

動的に描画されるページ（JavaScript 実行が必要なもの）は対象外。
