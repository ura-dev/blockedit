"""ゲートと分割が既知の壊れ方を実際に落とすかを、モデルを呼ばずに検査する。

bench/gate_replay.py は保存済みの生出力にゲートを後付けで当てるが、対象が
**生きているdocsとの比較**なのでdocsを編集すると成績が動く。こちらは入力と期待値を
ファイル内に持つので決定的で、0トークン・0.1秒で回る。

検査するのは実運用の経路そのもの — タスクの PATTERN / RESIDUE / validate と、
共通ゲート(範囲・完了・局所性)、それに分割の可逆性と終端文字の復元。

ケースは原文を組み立てて作る。期待値を手で二重に書くと、全角/半角のような
**1文字だけ違うケースが書き分けられているのか判別できなくなる**ため。

  uv run python blockedit_selftest.py
  uv run python blockedit_selftest.py tasks/foo.py
"""

import re
import sys
from pathlib import Path

import task as taskmod
from blocks import UNITS, join_blocks, relines, split, split_trailing_blanks
from gates import check

HERE = Path(__file__).resolve().parent

# 残すべき文 / 消すべき参照文。段落はこの2文でできている
KEEP = ("本ドキュメントは、開発基盤（エンジン/言語/ツール）の検討結果と"
        "次のアクションをまとめたハンズオフ資料です。")
REF = "企画内容は [docs/design/](../design/) 配下を参照。"
BEFORE = KEEP + REF

# (ラベル, 出力, finish_reason, 受理されるべきか)
MATCH_CASES = [
    ("参照文を丸ごと削除(正解)", KEEP, "stop", True),

    # 完了 — 仕事をしていない穴
    ("何もしない(no-op)", BEFORE, "stop", False),
    ("リンク記法だけ外して参照文を残す",
     KEEP + "企画内容は docs/design/ 配下を参照。", "stop", False),
    ("開き括弧だけ落とす(半端な破壊)",
     KEEP + "企画内容は docs/design/](../design/) 配下を参照。", "stop", False),

    # 局所性 — やりすぎの穴
    ("残す文を言い換える",
     "本ドキュメントは、開発基盤の検討結果と次のアクションをまとめた資料です。",
     "stop", False),
    ("残す文の約物を半角に化かす",
     KEEP.replace("（", "(").replace("）", ")"), "stop", False),
    ("削除したうえで見出しを捏造して足す", KEEP + "\n\n## 概要", "stop", False),
    ("削除したうえで一文を足す", KEEP + "詳しくは本文を読んでください。", "stop", False),
    ("段落を全部消す", "", "stop", False),

    # ランナー側の停止条件
    ("打ち切り(上限不足)", KEEP[:30], "length", False),
]


class LineTask:
    """行スコープ(UNIT='line' / SCOPE='unit')の最小タスク。

    実タスク3本はいずれも validate が常に非Noneを返す分類タスクで、受理される
    ケースを持てない。scope='unit' 側のゲートを検査するにはこれが要る。
    """
    PATTERN = re.compile(r"しない")
    RESIDUE = PATTERN


LINE_BEFORE = "この方式は再訪しない。"

LINE_CASES = [
    ("否定形を肯定形に書き換える(正解)", "この方式は見送る。", "stop", True),

    # 仕事をしていない側
    ("何もしない(no-op)", LINE_BEFORE, "stop", False),
    ("対象パターンが残ったまま", "この方式は採用しない。", "stop", False),

    # やりすぎ側 — 行スコープでは局所性が使えないので、形で縛る
    ("行を増やす(ブロックの外へはみ出す)", "この方式は見送る。\n補足を足す。", "stop", False),
    ("異常に長い出力", "見送る。" * 40, "stop", False),
    ("空の出力", "   ", "stop", False),
    ("打ち切り(上限不足)", "この方式は", "length", False),
]

# 分割の可逆性。CRLF・LF混在・EOFに改行なしを1本に混ぜてある
SPLIT_SAMPLE = (
    "# 見出し\r\n\r\n段落1の1行目\r\n段落1の2行目\r\n\r\n"
    "## 節\r\n\r\n```\r\nコード内の\r\n\r\n空行\r\n```\r\n\r\n最終段落(改行なしで終わる)"
)


def run_cases(label, task, before, cases, scope):
    bad = 0
    for name, after, finish, want in cases:
        reason = check(task, before, after, finish, 8000, scope)
        got = reason is None
        if got != want:
            bad += 1
            print(f"NG   [{label}] {name}")
            print(f"     期待={'受理' if want else '棄却'} "
                  f"実際={'受理' if got else '棄却'}  {reason or ''}")
    return bad


def run_split_cases():
    """割ったものが1バイトも変わらず戻ること、終端文字が復元されること。"""
    bad = 0
    for unit in UNITS:
        blocks = split(SPLIT_SAMPLE, unit, 2)          # 可逆性は split 内でassert
        if join_blocks(blocks) != SPLIT_SAMPLE:
            bad += 1
            print(f"NG   [split] unit={unit} で貼り戻しが原文と違う")

    # CRLFブロックに、モデルが返した(=LFしか持たない)本文を戻す
    core, _tail = split_trailing_blanks(split(SPLIT_SAMPLE, "para", 2)[1])
    restored = relines("段落1の1行目\n差し替えた2行目", core)
    if restored is None or "".join(restored) != "段落1の1行目\r\n差し替えた2行目\r\n":
        bad += 1
        print(f"NG   [relines] CRLFが復元されていない: {restored!r}")

    # EOFに改行が無い最終ブロックへ足さない
    last_core, _ = split_trailing_blanks(split(SPLIT_SAMPLE, "para", 2)[-1])
    tail_restored = relines("最終段落(書き換え済み)", last_core)
    if tail_restored is None or "".join(tail_restored).endswith(("\n", "\r")):
        bad += 1
        print(f"NG   [relines] EOFに無かった改行を足した: {tail_restored!r}")

    # 混在ブロックは推測せず None を返す(呼び出し側が棄却する)
    if relines("x\ny", ["a\r\n", "b\n"]) is not None:
        bad += 1
        print("NG   [relines] 改行コード混在を通した")
    return bad


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    task = taskmod.load(sys.argv[1] if len(sys.argv) > 1
                        else HERE / "tasks" / "example_reference_links.py")

    m = task.PATTERN.search(BEFORE)
    if not m or m.group(0) != REF:
        sys.exit(f"PATTERN が参照文と一致しない({m and m.group(0)!r})。ケース表が古い")

    bad = run_cases("match", task, BEFORE, MATCH_CASES, "match")
    bad += run_cases("unit", LineTask, LINE_BEFORE, LINE_CASES, "unit")
    bad += run_split_cases()

    total = len(MATCH_CASES) + len(LINE_CASES) + 5
    print(f"[selftest] {total}件中 {total - bad}件が期待どおり"
          f"{'' if not bad else f' — 不一致 {bad}件'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
