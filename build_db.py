#!/usr/bin/env python3
"""国税庁 法人番号公表サイトのデータをダウンロードし、SQLite3データベースを生成・更新する。

公表サイトの「全件データ」（月末時点の全国データ、大きい）と「差分データ」
（日次、小さい）の2種類を組み合わせて、毎回DBをフルリビルドする。

- 現在公開されている全件データをダウンロードして取り込む。
- その作成日より後の差分データを、公開されている分すべて日付順にダウンロードして適用する。

差分だけを既存DBに継ぎ足していく方式ではなく、毎回ゼロから作り直すことで、
不整合の蓄積を防いでいる（同期状態はDB内の meta テーブルに記録するが、
次回実行時に読み直して再利用することはしない）。

ダウンロードは通常のリンクではなく、隠しフォームのPOST送信（CSRFトークン＋
selDlFileNoパラメータ）で実現されているため、GETでトークン・セッションcookieを
取得してからPOSTするフローを自前で再現している。

テーブル構成:

- prefectures   : 都道府県コード -> 都道府県名
- cities        : 市区町村コード(都道府県コード+3桁) -> 市区町村名
- kinds         : 法人種別コード -> 法人種別名（固定10種、公式仕様書より）
- close_causes  : 登記記録の閉鎖等の事由コード -> 事由名（固定4種、公式仕様書より）
- corporations  : 法人番号(BIGINT) -> 商号名称, 都道府県コード, 市区町村コード,
  住所詳細(丁目番地等), 法人種別, 登記記録の閉鎖等の事由コード(close_cause)
  - 廃止・清算結了・合併等により無効になった法人番号も、close_causeに事由コードを
    設定した上で取り込む（空文字列 = 有効）。close_causeにインデックスを張っているため、
    「有効なものだけ」の絞り込み（`WHERE close_cause = ''`）は高速に行える。
  - 処理区分が「99:削除」の法人番号（指定そのものが撤回されたもの）は取り込み対象から削除する。
- meta          : 同期状態（全件データの作成日、最終適用済み差分日）を記録する内部テーブル

商号又は名称は、検索・比較しやすいよう以下の正規化を行う。

- Unicode正規化(NFKC)で半角カナ等を全角に統一。
- 各種ダッシュ類（半角ハイフン、長音記号の半角、各種ダッシュ記号）を長音記号「ー」に統一。
- 各種中黒類（半角中黒、中点等）を「・」に統一。
- 空白（半角スペース・全角スペース等の連続）を全角スペース1つに統一。
- 上記の結果、残った半角英数記号は全角に変換する（全て全角にする）。
"""

import argparse
import csv
import http.cookiejar
import io
import re
import sqlite3
import sys
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from datetime import date

BASE_URL = "https://www.houjin-bangou.nta.go.jp"
ZENKEN_INFO_PAGE = "/download/zenken/"
ZENKEN_DOWNLOAD_PAGE = "/download/zenken/index.html"
SABUN_INFO_PAGE = "/download/sabun/"
SABUN_DOWNLOAD_PAGE = "/download/sabun/index.html"

TOKEN_FIELD = "jp.go.nta.houjin_bangou.framework.web.common.CNSFWTokenProcessor.request.token"
TOKEN_RE = re.compile(re.escape(TOKEN_FIELD) + r'"\s+value="([^"]+)"')

USER_AGENT = "Mozilla/5.0 (compatible; houjin-bangou-sqlite3/1.0)"

# 法人種別（リソース定義書「項番15 法人種別」より、固定の10種）
KIND_LABELS = {
    "101": "国の機関",
    "201": "地方公共団体",
    "301": "株式会社",
    "302": "有限会社",
    "303": "合名会社",
    "304": "合資会社",
    "305": "合同会社",
    "399": "その他の設立登記法人",
    "401": "外国会社等",
    "499": "その他",
}

# 登記記録の閉鎖等の事由（リソース定義書「項番26 登記記録の閉鎖等の事由」より、固定の4種）
# 空文字列は「閉鎖等なし（有効）」を表す。
CLOSE_CAUSE_LABELS = {
    "01": "清算の結了等",
    "11": "合併による解散等",
    "21": "登記官による閉鎖",
    "31": "その他の清算の結了等",
}

# 処理区分「99」は、法人番号の指定が撤回されたことを表す（全項目がブランクになる）
PROCESS_DELETE = "99"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prefectures (
    pref_code TEXT PRIMARY KEY,
    name      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cities (
    city_code TEXT PRIMARY KEY,
    pref_code TEXT NOT NULL REFERENCES prefectures (pref_code),
    name      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cities_pref_code ON cities (pref_code);

CREATE TABLE IF NOT EXISTS kinds (
    kind_code TEXT PRIMARY KEY,
    name      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS close_causes (
    close_cause_code TEXT PRIMARY KEY,
    name             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corporations (
    corporate_number INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    pref_code        TEXT NOT NULL REFERENCES prefectures (pref_code),
    city_code        TEXT NOT NULL REFERENCES cities (city_code),
    address          TEXT NOT NULL,
    kind             TEXT NOT NULL REFERENCES kinds (kind_code),
    close_cause      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_corporations_name ON corporations (name);
CREATE INDEX IF NOT EXISTS idx_corporations_city_code ON corporations (city_code);
CREATE INDEX IF NOT EXISTS idx_corporations_close_cause ON corporations (close_cause);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# --- 商号又は名称の正規化 -------------------------------------------------

_DASH_TABLE = {ord(c): "ー" for c in "-‐‑‒–—―−ｰ"}
_NAKAGURO_TABLE = {ord(c): "・" for c in "·•"}
_WHITESPACE_RE = re.compile(r"[ \t　]+")


def normalize_name(raw):
    s = unicodedata.normalize("NFKC", raw)
    s = s.translate(_DASH_TABLE)
    s = s.translate(_NAKAGURO_TABLE)
    s = _WHITESPACE_RE.sub("　", s).strip("　 \t")
    s = "".join(chr(ord(c) + 0xFEE0) if 0x21 <= ord(c) <= 0x7E else c for c in s)
    return s


# --- HTTP（セッションcookie + CSRFトークン + フォームPOST） ----------------


def make_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def fetch_page(opener, path):
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent": USER_AGENT})
    with opener.open(req, timeout=60) as res:
        return res.read().decode("utf-8", errors="ignore")


def download_file(opener, download_page, file_no):
    html = fetch_page(opener, download_page)
    token_match = TOKEN_RE.search(html)
    if not token_match:
        raise RuntimeError(f"CSRFトークンが見つかりません: {download_page}")
    data = urllib.parse.urlencode(
        {
            TOKEN_FIELD: token_match.group(1),
            "event": "download",
            "selDlFileNo": str(file_no),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + download_page, data=data, headers={"User-Agent": USER_AGENT}, method="POST"
    )
    with opener.open(req, timeout=1800) as res:
        return res.read()


def extract_csv_text(zip_bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_name = next(name for name in zf.namelist() if name.lower().endswith(".csv"))
        with zf.open(csv_name) as f:
            return f.read().decode("utf-8-sig")


# --- ダウンロードページの解析 ---------------------------------------------


def _extract_section(html, section_id):
    pattern = re.escape(f'id="{section_id}"') + r'.*?(?=<h2 class="title" id="|\Z)'
    match = re.search(pattern, html, re.DOTALL)
    if not match:
        raise RuntimeError(f"セクションが見つかりません: {section_id}")
    return match.group(0)


def _reiwa_to_date(year, month, day):
    return date(2018 + int(year), int(month), int(day))


def get_baseline_info(opener):
    """全件データ（CSV・Unicode、全国）の作成日とファイル番号を取得する。"""
    html = fetch_page(opener, ZENKEN_INFO_PAGE)
    section = _extract_section(html, "csv-unicode")

    date_match = re.search(r"令和(\d+)年(\d+)月(\d+)日更新", section)
    file_match = re.search(r"全国</dt>\s*<dd>.*?doDownload\((\d+)\)", section, re.DOTALL)
    if not date_match or not file_match:
        raise RuntimeError("全件データの情報が見つかりません")

    baseline_date = _reiwa_to_date(*date_match.groups())
    return baseline_date, int(file_match.group(1))


def get_diff_file_list(opener):
    """差分データ（CSV・Unicode）の (日付, ファイル番号のリスト) を日付昇順で取得する。"""
    html = fetch_page(opener, SABUN_INFO_PAGE)
    section = _extract_section(html, "csv-unicode")

    pattern = re.compile(r"令和(\d+)年(\d+)月(\d+)日\s*</th>\s*<td>(.*?)</td>", re.DOTALL)
    entries = []
    for year, month, day, block in pattern.findall(section):
        entry_date = _reiwa_to_date(year, month, day)
        file_nos = [int(n) for n in re.findall(r"doDownload\((\d+)\)", block)]
        if file_nos:
            entries.append((entry_date, file_nos))
    entries.sort(key=lambda e: e[0])
    return entries


# --- CSV行の適用 -----------------------------------------------------------


def iter_csv_rows(csv_text):
    yield from csv.reader(io.StringIO(csv_text))


def apply_rows(conn, rows):
    prefectures = {}
    cities = {}
    upserts = []
    deletes = []

    for row in rows:
        corporate_number = row[1]
        process = row[2]

        if process == PROCESS_DELETE:
            # 法人番号の指定そのものが撤回されたもの。名称・住所等の項目値も無いため、
            # 有効/廃止を問わず対象から削除する。
            deletes.append(int(corporate_number))
            continue

        pref_code, city_local_code = row[13], row[14]
        if not pref_code or not city_local_code:
            # 国外所在地の法人など、国内の都道府県・市区町村コードが無いものは対象外
            continue

        pref_name, city_name = row[9], row[10]
        city_code = pref_code + city_local_code
        prefectures[pref_code] = pref_name
        cities[city_code] = (pref_code, city_name)

        name = normalize_name(row[6])
        address = row[11].strip()
        kind = row[8]
        close_cause = row[19]  # 空文字列 = 有効（閉鎖等なし）
        upserts.append((int(corporate_number), name, pref_code, city_code, address, kind, close_cause))

    if prefectures:
        conn.executemany(
            "INSERT INTO prefectures (pref_code, name) VALUES (?, ?) "
            "ON CONFLICT(pref_code) DO UPDATE SET name = excluded.name",
            list(prefectures.items()),
        )
    if cities:
        conn.executemany(
            "INSERT INTO cities (city_code, pref_code, name) VALUES (?, ?, ?) "
            "ON CONFLICT(city_code) DO UPDATE SET pref_code = excluded.pref_code, name = excluded.name",
            [(code, pref_code, name) for code, (pref_code, name) in cities.items()],
        )
    if upserts:
        conn.executemany(
            "INSERT INTO corporations (corporate_number, name, pref_code, city_code, address, kind, close_cause) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(corporate_number) DO UPDATE SET "
            "name = excluded.name, pref_code = excluded.pref_code, city_code = excluded.city_code, "
            "address = excluded.address, kind = excluded.kind, close_cause = excluded.close_cause",
            upserts,
        )
    if deletes:
        conn.executemany(
            "DELETE FROM corporations WHERE corporate_number = ?", [(n,) for n in deletes]
        )

    return len(upserts), len(deletes)


def ensure_kinds(conn):
    conn.executemany(
        "INSERT INTO kinds (kind_code, name) VALUES (?, ?) "
        "ON CONFLICT(kind_code) DO UPDATE SET name = excluded.name",
        list(KIND_LABELS.items()),
    )
    conn.executemany(
        "INSERT INTO close_causes (close_cause_code, name) VALUES (?, ?) "
        "ON CONFLICT(close_cause_code) DO UPDATE SET name = excluded.name",
        list(CLOSE_CAUSE_LABELS.items()),
    )


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --- 同期処理 ---------------------------------------------------------------


def get_latest_available_date(opener):
    """全件データの作成日と、その後の差分データの最終日のうち、より新しい方を返す。

    重いファイルは一切ダウンロードせず、ダウンロードページのHTML取得のみで判定できる。
    """
    baseline_date, _ = get_baseline_info(opener)
    diff_entries = get_diff_file_list(opener)
    dates = [baseline_date] + [entry_date for entry_date, _ in diff_entries]
    return max(dates)


def build_database(db_path):
    """全件データ + それ以降の差分データから、DBを毎回フルリビルドする。

    差分だけを既存DBに適用していく方式ではなく、常に全件データから作り直すことで、
    不整合の蓄積を防ぎ、DBを毎回クリーンな状態に保つ。
    """
    opener = make_opener()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM corporations")
        conn.execute("DELETE FROM cities")
        conn.execute("DELETE FROM prefectures")
        ensure_kinds(conn)

        baseline_date, baseline_file_no = get_baseline_info(opener)
        print(f"全件データ({baseline_date})を取得します。", file=sys.stderr)
        zip_bytes = download_file(opener, ZENKEN_DOWNLOAD_PAGE, baseline_file_no)
        csv_text = extract_csv_text(zip_bytes)
        upserts, deletes = apply_rows(conn, iter_csv_rows(csv_text))
        print(f"  有効: {upserts} 件, 除外(廃止等): {deletes} 件", file=sys.stderr)
        set_meta(conn, "baseline_date", baseline_date.isoformat())

        latest_date = baseline_date
        diff_entries = get_diff_file_list(opener)
        pending = [(d, file_nos) for d, file_nos in diff_entries if d > baseline_date]

        for entry_date, file_nos in pending:
            total_upserts = total_deletes = 0
            for file_no in file_nos:
                zip_bytes = download_file(opener, SABUN_DOWNLOAD_PAGE, file_no)
                csv_text = extract_csv_text(zip_bytes)
                upserts, deletes = apply_rows(conn, iter_csv_rows(csv_text))
                total_upserts += upserts
                total_deletes += deletes
            latest_date = entry_date
            print(
                f"差分 {entry_date} を適用しました（反映: {total_upserts} 件, 除外: {total_deletes} 件）。",
                file=sys.stderr,
            )

        set_meta(conn, "last_diff_date", latest_date.isoformat())
        conn.commit()
        conn.execute("VACUUM")

        count = conn.execute("SELECT COUNT(*) FROM corporations").fetchone()[0]
        print(f"corporations: {count} 件（最終適用日: {latest_date}）", file=sys.stderr)
        return latest_date
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", default="houjin_bangou.db", help="出力先のSQLite3ファイルパス")
    parser.add_argument(
        "--check-latest-date",
        action="store_true",
        help="ダウンロードは行わず、公表サイト上の最新データの日付だけを表示して終了する",
    )
    args = parser.parse_args()

    if args.check_latest_date:
        print(get_latest_available_date(make_opener()).isoformat())
        return

    latest_date = build_database(args.output)
    print(f"latest_date={latest_date.isoformat()}")


if __name__ == "__main__":
    main()
