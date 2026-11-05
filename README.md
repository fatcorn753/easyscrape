# easyscrape

URL と取り出したい項目を指定するだけで、Web ページや公開 API から情報を CSV / JSON に取り出すツール。
ブラウザで使う画面（`easyscrape-ui`）と、コマンドライン（`easyscrape`）の 2 通り。

## かんたんに使う（ブラウザ画面）

```sh
uv run easyscrape-ui          # http://127.0.0.1:8790/ が開く
uv run easyscrape-ui 8791     # ポートを変える場合
```

1. 「サンプルで試す」を押すか、URL を入れて「1. ページを開く」→「読み込む」
2. 「2. 自動で候補を出す」で一覧と取り出せる項目の候補が出る。候補を選んで「3. 取り出す内容」に入れる（ページ上のクリックでも入れられる）
3. 「取得する」を押す。結果が表で表示される
4. 「CSV で保存」または「JSON で保存」を押す
5. 複数のページを順に取る場合は、URL に規則を書く（例: `https://example.com/list?page={1..10}`）。「URL の展開を確認」で、取得するページの一覧と時間の目安を確認できる
6. 一定期間、定期的に取得したい場合は、画面下の「繰り返し取得」で間隔と期間を選び、「繰り返し取得を始める」を押す。結果は `~/easyscrape-output/` のファイルに追記される。状況の確認と停止は、表示される状況画面から行う
7. 一覧のリンクから詳細ページも取る場合は、「詳細ページのリンクの項目名」に入力して取得する。結果画面で件数と時間の目安を確認してから「詳細ページを取得する」を押す（同じサイト・同じフォルダ内だけを対象にする）

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

```sh
# URL の規則: {1..10} 連番、{01..12} ゼロ埋め、{1..20..5} 刻み、{tokyo,osaka} 列挙（上限 200 件）
uv run easyscrape 'https://example.com/list?page={1..10}' --item 'li.article' --field 'title=h2' -o all.csv

# 30 分おきに 24 時間、結果を追記（間隔は 10 分以上、期間は最長 7 日。止めるには Ctrl-C）
uv run easyscrape https://example.com/news --item 'li.article' --field 'title=h2' --field 'link=a@href' \
  --every 30m --for 24h -o watch.csv

# 新しい行・内容が変わった行だけを保存（--key で同じ行を見分ける。url 列と組み合わせて判定）
uv run easyscrape https://example.com/news --item 'li.article' --field 'title=h2' --field 'link=a@href' \
  --every 1h --for 3d --key link --only-changes -o changes.csv
```

- 繰り返し取得は、各行に `fetched_at` 列が付き、`-o` のファイルに追記される（既にあるファイルでも `--force` は不要。列が違うと追記しない）。`--only-changes` では `change` 列（`new` / `changed`）も付き、保存済みのファイルを読んで再開後も同じ行を重ねて保存しない
- 繰り返しは、このプロセスが動いている間だけ続く。PC を閉じたり終了したりすると止まる。長期間は、OS の定期実行から 1 回ずつ呼ぶ方法が向く（例: cron で毎時 `easyscrape … --every 10m --times 1 --key link --only-changes -o changes.csv`。`--times 1` は 1 回だけ取得して終わる）
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
- HTML に埋め込まれた JSON（`<script id="__NEXT_DATA__">`、JSON-LD など）からも取得できる。CLI は `--embedded 'script#__NEXT_DATA__' --records 配列の場所`、画面は「埋め込みデータの場所」と詳細設定の「JSON の場合」に入力する

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
