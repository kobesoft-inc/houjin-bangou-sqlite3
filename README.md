# houjin-bangou-sqlite3

国税庁 [法人番号公表サイト](https://www.houjin-bangou.nta.go.jp/) が公開している法人番号データを
ダウンロードし、SQLite3データベースに変換・毎日同期するツールです。

## 使い方

### 1. データをダウンロードする

最新のデータが [GitHub Releases](https://github.com/kobesoft-inc/houjin-bangou-sqlite3/releases) に
公開されています。`releases/latest` から常に最新版を取得できます。

```
https://github.com/kobesoft-inc/houjin-bangou-sqlite3/releases/latest
```

DBファイルが50MBを超える場合、`houjin_bangou.db.00`, `houjin_bangou.db.01`, ... のように
分割されたファイルとしてリリースされます（GitHub Releasesの1アセットあたりのサイズ制約とは無関係に、
ダウンロード・配布のしやすさのために分割しています）。分割されている場合は、ダウンロード後に
連結してください。

```bash
cat houjin_bangou.db.* > houjin_bangou.db
```

分割されていない場合は `houjin_bangou.db` 単体がそのままリリースされています。

### 2. テーブル構造

| テーブル | 内容 |
| --- | --- |
| `prefectures` | 都道府県コード(2桁) → 都道府県名 |
| `cities` | 市区町村コード(5桁、都道府県コード+3桁) → 市区町村名 |
| `kinds` | 法人種別コード(3桁) → 法人種別名（固定10種、後述） |
| `corporations` | 法人番号 → 商号名称・都道府県コード・市区町村コード・法人種別 |

#### corporations（法人）

| カラム | 内容 |
| --- | --- |
| corporate_number | 法人番号（13桁、INTEGER PRIMARY KEY） |
| name | 商号又は名称（正規化済み。後述） |
| pref_code | 都道府県コード（`prefectures.pref_code` を参照） |
| city_code | 市区町村コード（`cities.city_code` を参照） |
| kind | 法人種別コード（`kinds.kind_code` を参照） |

**登記記録の閉鎖等年月日が設定されている法人（廃止・清算結了・合併等）は取り込んでいません。**
本テーブルには常に有効な法人のみが格納されます。

```sql
SELECT c.corporate_number, c.name, pr.name AS pref, ci.name AS city, k.name AS kind
FROM corporations c
JOIN prefectures pr ON pr.pref_code = c.pref_code
JOIN cities ci ON ci.city_code = c.city_code
JOIN kinds k ON k.kind_code = c.kind
WHERE c.corporate_number = 1010001093652;
```

#### kinds（法人種別、固定値）

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

### 3. 前方一致検索について

`name` にインデックスを作成していますが、**SQLiteでは `LIKE 'プレフィックス%'` はデフォルトの
接続設定ではインデックスが使われません**（`PRAGMA case_sensitive_like = ON;` を明示的に
実行する必要があります）。日本語を含む文字列の前方一致検索では、代わりに `GLOB` を使うと
追加設定なしでインデックスが使われます。

```sql
-- インデックスが使われる
SELECT * FROM corporations WHERE name GLOB '株式会社サンプル*';

-- インデックスを使うにはPRAGMAが必要
PRAGMA case_sensitive_like = ON;
SELECT * FROM corporations WHERE name LIKE '株式会社サンプル%';
```

### 商号又は名称の正規化ルール

法人番号データの商号又は名称は全角文字での提供が原則ですが、検索・比較をしやすくするため
以下のルールでさらに正規化しています。

- Unicode正規化(NFKC)により、半角カタカナ等を全角に変換。
- 半角ハイフン「-」、各種ダッシュ記号（‐ ‑ ‒ – — ― − など）、半角カナの長音記号は、
  すべて長音記号「ー」に統一。
- 半角中黒「･」、中点「·」、ビュレット「•」は、すべて「・」に統一。
- 半角スペース・全角スペースの連続は、全角スペース1つに統一（前後の空白は除去）。
- 上記の結果として残る半角の英数字・記号は、すべて全角に変換（全て全角にする）。

## 自動更新（GitHub Actions）

`.github/workflows/update-db.yml` により、毎日 09:00 JST（`cron: "0 0 * * *"` UTC）に
以下を自動実行します。`workflow_dispatch` にも対応しているため、GitHubのActionsタブから
手動実行も可能です。

1. 公表サイトの「全件データの作成日」と「差分データの最終日」を確認する（軽量、HTML取得のみ）。
2. そのうち最も新しい日付が、既存の最新リリースのタグ（`db-YYYY-MM-DD`）と同じであれば、
   ここで終了する（ビルドもリリース作成も行わない）。
3. 日付が異なる場合のみ、`build_db.py` でDBをフルリビルドする（詳細は次節）。
4. DBが50MBを超えていれば分割し、`db-<最新データの日付>` というタグで新しいリリースを作成する。

### 同期の仕組み（全件データ + 差分データ、常にフルリビルド）

法人番号公表サイトには「全件データ」（月末更新、全国で1ファイル・数百MB）と
「差分データ」（日次更新、当日分のみ・数百KB）の2種類があります。

`build_db.py` は実行するたびに、既存DBを引き継がず、常に次の手順でゼロから作り直します。

1. 現在公開されている全件データをダウンロードして取り込む。
2. その作成日より後の差分データを、公開されている分すべて日付順にダウンロードして適用する
   （法人番号ごとにUPSERT、廃止・削除は該当行を削除）。

差分だけを既存DBに継ぎ足していく方式ではなくこの方式にしているのは、実行のたびに
毎回同じ入力から同じ結果を再現でき、不整合が蓄積しないためです（実測では全件データの取得から
差分適用までフルリビルドしても2分程度で完了するため、日次実行でも問題ありません）。
ビルド自体は毎回フルリビルドですが、その前段の「新しいデータがあるかどうかの確認」は
軽量なため、変化が無い日にビルドやリリース作成が走ることはありません。

### ダウンロードの仕組みについて（隠しフォームのPOST送信）

法人番号公表サイトのダウンロードボタンは通常のリンクではなく、JavaScriptの
`doDownload(fileNo)` が隠しフォーム（CSRFトークン付き）にファイル番号をセットして
POST送信する仕組みになっています。`build_db.py` では、ダウンロードページをGETして
セッションCookieとCSRFトークンを取得し、`selDlFileNo` を指定してPOSTすることで、
この画面操作をスクリプトから再現しています。

## データソース

- 全件データ・差分データのダウンロード: https://www.houjin-bangou.nta.go.jp/download/
- CSVの列構成・仕様: リソース定義書（ダウンロードページからリンクされているPDF）

## ライセンス

このリポジトリのコードは MIT License です。

法人番号データ自体は国税庁「公共データ利用規約（第1.0版）」に基づき提供されています。
利用の際は出典の記載が必要です（例:「出典：国税庁法人番号公表サイト（国税庁）」）。
このデータを加工・編集して利用する場合は、その旨も併せて記載する必要があります。
詳細は https://www.houjin-bangou.nta.go.jp/riyokiyaku/ を参照してください。
