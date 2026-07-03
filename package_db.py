#!/usr/bin/env python3
"""生成したSQLite3ファイルをリリース用に加工する。

1. gzip圧縮する（`<path>.gz`）。
2. 圧縮後のファイルが指定サイズ（既定50MB）を超える場合、`<path>.gz.00`, `<path>.gz.01`, ...
   のように連番で分割する（桁数は分割数に応じて自動的に揃えるため、`cat <path>.gz.*` の
   ような文字列ソートでも正しい順序になる）。
3. 元のDB本体と、実際に配布するファイル（圧縮後 or 分割後）すべてのSHA256を `SHA256SUMS`
   に書き出す。

ダウンロード側は、分割されている場合は結合してから展開する。

    cat houjin_bangou.db.gz.* > houjin_bangou.db.gz   # 分割されていない場合は不要
    gunzip houjin_bangou.db.gz

SHA256SUMSには、ダウンロードした個々のファイルの検証用の行に加えて、
展開後の元のDB本体（houjin_bangou.db）の検証用の行も含まれる。
"""

import argparse
import gzip
import hashlib
import math
import os
import shutil

DEFAULT_CHUNK_SIZE = 50_000_000  # 50MB


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compress(path, compresslevel=6):
    gz_path = f"{path}.gz"
    with open(path, "rb") as src, gzip.open(gz_path, "wb", compresslevel=compresslevel) as dst:
        shutil.copyfileobj(src, dst)
    return gz_path


def split_file(path, chunk_size):
    size = os.path.getsize(path)
    if size <= chunk_size:
        return [path]

    num_parts = math.ceil(size / chunk_size)
    width = max(1, len(str(num_parts - 1)))

    parts = []
    with open(path, "rb") as f:
        index = 0
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            part_path = f"{path}.{index:0{width}d}"
            with open(part_path, "wb") as out:
                out.write(chunk)
            parts.append(part_path)
            index += 1

    os.remove(path)
    return parts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", help="対象のSQLite3ファイル")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="分割サイズ(バイト、既定50MB)")
    args = parser.parse_args()

    checksums = [(sha256_of(args.path), os.path.basename(args.path))]

    gz_path = compress(args.path)
    parts = split_file(gz_path, args.chunk_size)
    for part in parts:
        checksums.append((sha256_of(part), os.path.basename(part)))

    sums_path = os.path.join(os.path.dirname(args.path) or ".", "SHA256SUMS")
    with open(sums_path, "w") as f:
        for digest, name in checksums:
            f.write(f"{digest}  {name}\n")

    for part in parts:
        print(part)
    print(sums_path)


if __name__ == "__main__":
    main()
