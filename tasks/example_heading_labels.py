"""例: `## ` 見出しを関心事で分類する(分類のみ・ファイルは書き換えない)。

判断ラダー3段目(LLMに1行ずつ判断させる)の見本であり、bench の共通負荷でもある。
`transform` を持たないので、KoboldCpp が呼ばれる。

**書き込みは発生しない。**`validate` が常に非Noneを返すため全件が「棄却」となり、
結果は `reports/` に出るだけになる。ドキュメントの棚卸しのように、機械の判断を
一次スキャンとして受け取り、最終判断を人間(またはClaude)が行う使い方を想定している。

自分のリポジトリで使うときは FILES とラベル定義を書き換える。ラベルを抽象的に
しすぎると小型モデルの精度が落ちるので、**フォルダ構成のように既に存在する区分を
そのままラベルにする**のが当たりを取りやすい。
"""
import re

FILES = ["docs/**/*.md"]
UNIT = "line"     # 見出し行を見せる
SCOPE = "unit"    # 行全体が改変可(出力はラベルなので原文とは無関係になる)
PATTERN = re.compile(r"^## ")

LABELS = ("OVERVIEW", "GUIDE", "REFERENCE", "TROUBLESHOOTING")

INSTRUCTION = """次の1行(Markdownの見出し)を4択で分類しろ。出力はラベル1語だけ
(OVERVIEW / GUIDE / REFERENCE / TROUBLESHOOTING)。

- OVERVIEW: 目的・全体像・設計の背景など、読み手が最初に読む説明
- GUIDE: 手順。読み手が順に実行して何かを達成する話
- REFERENCE: 引数・設定項目・戻り値など、引くための一覧
- TROUBLESHOOTING: 失敗したときの症状と対処"""

# few-shot はラベル1つにつき最低1件。UNIT="line" の LLM 方式では必須で、
# 無いと起動時に落ちる — 素の指示だけでは系統的な誤分類が出るため
FEWSHOT = [
    ("## このツールが解く問題", "OVERVIEW"),
    ("## インストール手順", "GUIDE"),
    ("## コマンドライン引数", "REFERENCE"),
    ("## 接続できないとき", "TROUBLESHOOTING"),
]

# よくある誤りを1件だけ見せる。**増やしすぎると逆効果**で、例に含まれる語に
# 引っ張られた過学習が出る。ラベルの定義そのものを見直すほうが効くことが多い
PITFALL = ("## 設定ファイルの書き方", "REFERENCE", "GUIDE")


def validate(before, after):
    """常に非Noneを返し、全件を reports/ に送る。ファイルには書き込ませない。"""
    label = after.strip().upper()
    if label not in LABELS:
        return f"不正なラベル: {after!r}"
    return f"LABEL={label}"
