"""例: docs間の `[表示](相対パス.md)` リンクを表示テキストだけに畳む。

判断ラダー1段目(純正規表現)の見本。`transform` を定義したのでLLMは呼ばれない。
決定的・即時・0トークン。
"""

import re

FILES = ["docs/**/*.md"]

UNIT = "line"     # 行を見せる
SCOPE = "unit"    # 行全体が改変可

# 画像 `![alt](...)` と外部URLは対象外。相対パスの .md リンクだけを狙う
PATTERN = re.compile(r"(?<!!)\[([^\]\n]+)\]\((?!https?://)[^)\n]*\.md[^)\n]*\)")


def transform(body):
    return PATTERN.sub(r"\1", body)


def preserve(body):
    """表示テキストは1文字も落ちてはならない。"""
    return PATTERN.findall(body)
