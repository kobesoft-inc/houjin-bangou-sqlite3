# houjin-bangou-sqlite3

国税庁「[法人番号公表サイト](https://www.houjin-bangou.nta.go.jp/)」が公開している法人番号データを、
そのまま使えるSQLite3データベースにして配布しています。毎日自動チェックし、新しいデータが
公開されていればDBを作り直します（[GitHub Actions](.github/workflows/update-db.yml)）。

廃止・清算結了・合併等により無効になった法人番号も、`close_cause`にその事由を入れた上で
収録しています（現存する法人だけが欲しい場合は`close_cause = ''`で絞り込んでください。
インデックスがあるため高速です）。

## ダウンロード

最新版は [Releases](https://github.com/kobesoft-inc/houjin-bangou-sqlite3/releases/latest) から
取得できます。配布ファイルはgzip圧縮した上で、`houjin_bangou.db.gz.0`, `houjin_bangou.db.gz.1`, ...
のように50MBごとに分割されています（法人数が数百万件規模のため、圧縮後も50MBは確実に超えます）。
ダウンロード後、次のように連結してから展開してください。

```bash
cat houjin_bangou.db.gz.* > houjin_bangou.db.gz
gunzip houjin_bangou.db.gz
```

リリースには `SHA256SUMS` も添付されています。ファイルが全部揃っているか・壊れていないかは
次のコマンドで確認できます（`houjin_bangou.db` の行は展開後のファイルの検証用のため、
展開前は "No such file" と表示されますが問題ありません）。

```bash
shasum -a 256 -c SHA256SUMS
```

## テーブル構成

| テーブル | 内容 |
| --- | --- |
| `corporations` | 法人番号 → 商号名称・住所・法人種別・閉鎖等の事由 |
| `prefectures` | 都道府県コード(2桁) → 都道府県名 |
| `cities` | 市区町村コード(5桁) → 市区町村名 |
| `kinds` | 法人種別コード(3桁) → 法人種別名（固定10種） |
| `close_causes` | 登記記録の閉鎖等の事由コード(2桁) → 事由名（固定4種） |

### corporations（法人）

| カラム | 内容 |
| --- | --- |
| corporate_number | 法人番号（13桁の整数） |
| name | 商号又は名称（正規化済み。下記「検索のヒント」を参照） |
| pref_code | 都道府県コード（`prefectures.pref_code` を参照） |
| city_code | 市区町村コード（`cities.city_code` を参照） |
| address | 住所の詳細（丁目・番地・建物名等。市区町村より後ろの部分。元データのまま） |
| kind | 法人種別コード（`kinds.kind_code` を参照） |
| close_cause | 登記記録の閉鎖等の事由コード（`close_causes.close_cause_code` を参照。**空文字列 = 有効**） |

`pref_code`・`city_code`から得られる都道府県名・市区町村名と`address`をつなげれば、
実際の住所文字列になります。`address`の「－」「‐」等のハイフンは番地の区切りとして意味が
あるため、`name`のような正規化（ダッシュの統一等）は行っていません。

現存する法人だけが欲しい場合は`close_cause = ''`で絞り込んでください。`close_cause`には
インデックスがあるため、この絞り込みは高速です。

```sql
-- 現存する法人だけを検索（インデックスが使われる）
SELECT c.corporate_number, c.name, pr.name AS pref, ci.name AS city, c.address, k.name AS kind
FROM corporations c
JOIN prefectures pr ON pr.pref_code = c.pref_code
JOIN cities ci ON ci.city_code = c.city_code
JOIN kinds k ON k.kind_code = c.kind
WHERE c.corporate_number = 1010001093652
  AND c.close_cause = '';

-- 廃止・清算結了・合併等の理由も含めて確認する
SELECT c.corporate_number, c.name, cc.name AS close_cause
FROM corporations c
LEFT JOIN close_causes cc ON cc.close_cause_code = c.close_cause
WHERE c.corporate_number = 1000020328642;
```

### kinds（法人種別、固定10種）

| コード | 種別 |
| --- | --- |
| 101 | 国の機関 |
| 201 | 地方公共団体 |
| 301 | 株式会社 |
| 302 | 有限会社 |
| 303 | 合名会社 |
| 304 | 合資会社 |
| 305 | 合同会社 |
| 399 | その他の設立登記法人 |
| 401 | 外国会社等 |
| 499 | その他 |

### close_causes（登記記録の閉鎖等の事由、固定4種）

| コード | 事由 |
| --- | --- |
| （空文字列） | 有効（閉鎖等なし） |
| 01 | 清算の結了等 |
| 11 | 合併による解散等 |
| 21 | 登記官による閉鎖 |
| 31 | その他の清算の結了等 |

## 検索のヒント

### 前方一致検索は GLOB を使う

`name`にインデックスがありますが、SQLiteの`LIKE 'プレフィックス%'`は既定の接続設定では
インデックスが使われません（`PRAGMA case_sensitive_like = ON;`の実行が必要）。代わりに
`GLOB`を使うと、追加設定なしでインデックスが使われます。

```sql
-- こちらを推奨（追加設定不要でインデックスが使われる）
SELECT * FROM corporations WHERE name GLOB '株式会社サンプル*';

-- LIKEでインデックスを使うには事前にPRAGMAが必要
PRAGMA case_sensitive_like = ON;
SELECT * FROM corporations WHERE name LIKE '株式会社サンプル%';
```

### 商号又は名称の正規化ルール

`name`は元データのまま格納すると表記ゆれ（半角/全角、ダッシュの種類など）が多いため、
検索・比較しやすいよう以下のルールで正規化しています。

- Unicode正規化(NFKC)により、半角カタカナ等を全角に変換。
- 半角ハイフン「-」や各種ダッシュ記号（‐ ‑ ‒ – — ― − など）は、すべて長音記号「ー」に統一。
- 半角中黒「･」、中点「·」、ビュレット「•」は、すべて「・」に統一。
- 半角スペース・全角スペースの連続は、全角スペース1つに統一（前後の空白は除去）。
- 上記の結果として残る半角の英数字・記号は、すべて全角に変換。

## 更新頻度

毎日09:00 JSTに、公表サイト側の最新データの日付をチェックし、新しいデータが公開されていた
場合のみDBをフルリビルドして最新版をリリースします（更新が無い日は何もしません）。
過去のリリースは残さず、常に最新版のみを公開しています。

## ライセンス

このリポジトリのコード（`build_db.py`等）は MIT License です。

法人番号データ自体は国税庁「公共データ利用規約（第1.0版）」に基づき提供されています。
利用の際は出典の記載が必要です（例:「出典：国税庁法人番号公表サイト（国税庁）」）。加工・編集
して利用する場合は、その旨も併せて記載してください。詳細は
[利用規約](https://www.houjin-bangou.nta.go.jp/riyokiyaku/) を参照してください。

## 自分でビルドする場合

Python 3系のみで動作します（追加の依存ライブラリは不要）。

```bash
python3 build_db.py -o houjin_bangou.db
```

公表サイトから最新の「全件データ」と、それより新しい「差分データ」をすべてダウンロードして
取り込み、カレントディレクトリに `houjin_bangou.db` を生成します
（データソース: [ダウンロードページ](https://www.houjin-bangou.nta.go.jp/download/)）。

公表サイトのダウンロードボタンは通常のリンクではなく、隠しフォームのPOST送信（CSRFトークン付き）
で実現されています。`build_db.py`は、ダウンロードページをGETしてセッションCookieとトークンを
取得し、ファイル番号を指定してPOSTすることで、この画面操作を再現しています。

自動更新の詳細（差分の追いつき方・DB圧縮など）は
[`.github/workflows/update-db.yml`](.github/workflows/update-db.yml) と
[`build_db.py`](build_db.py) を参照してください。
