#!/usr/bin/env python3
"""ファイルが指定サイズを超える場合、末尾に連番を振って分割する（既定50MB）。

分割されたファイルは `path.00`, `path.01`, ... という名前になる（桁数は分割数に応じて
自動的に揃えるため、`ls`や`cat path.*`のような文字列ソートでも正しい順序になる）。
ダウンロード側は次のように連結すれば元のファイルに戻せる。

    cat path.* > path

分割が発生した場合は元ファイルを削除し、分割後のパスを標準出力に1行ずつ出力する。
分割が不要な場合は、元のパスをそのまま1行出力する。
"""

import argparse
import math
import os

DEFAULT_CHUNK_SIZE = 50_000_000  # 50MB


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
    parser.add_argument("path", help="分割対象のファイル")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="分割サイズ(バイト、既定50MB)")
    args = parser.parse_args()

    for part in split_file(args.path, args.chunk_size):
        print(part)


if __name__ == "__main__":
    main()
