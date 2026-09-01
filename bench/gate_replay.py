"""保存済みの生出力にゲートを後付けで当て、素通り/誤検出を数える。

ゲートを変えたら必ずこれを回す。**判定の反転が0件であること**が、既存の成績を
壊していない根拠になる。対象は results/raw/ に溜めた183件の生出力。

  uv run python gate_replay.py

生きたdocsとの比較なのでdocsを編集すると成績が動く。入力と期待値を自分で持つ
決定的な検査は本体側の blockedit_selftest.py にある(0トークン・0.1秒)。
"""

import json
import sys
from pathlib import Path

import _parent  # noqa: F401  親(ランナー本体)を import path に載せる
from gates import LINK_RE, RESIDUE_RE, gate
from rewrite_swap import build_corpus, strip_fence

RAW_DIR = Path(__file__).resolve().parent / "results" / "raw"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    pristine, injected = build_corpus()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted(RAW_DIR.glob("*.md")):
        cand = strip_fence(path.read_text(encoding="utf-8"))
        accept, reasons, stats = gate(injected, cand, LINK_RE, RESIDUE_RE)
        exact = cand.rstrip("\n") == pristine.rstrip("\n")
        # ゲートが「静かな破損」を実際に止めるか / 正解を誤って弾かないか
        verdict = ("PASS" if accept and exact else
                   "FALSE_REJECT" if not accept and exact else
                   "CAUGHT" if not accept else "LEAK")
        rows.append({"file": path.name, "verdict": verdict, "exact": exact,
                     "accept": accept, **stats, "reasons": reasons[:3]})
        print(f"{verdict:12} {path.name:28} changed={stats['changed_lines']:3} "
              f"out_of_scope={stats['out_of_scope_lines']:3} "
              f"del={stats['deleted']:3} ins={stats['inserted']:3}")
        for r in reasons[:3]:
            print(f"             ! {r}")

    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(f"\n{counts}")
    (RAW_DIR.parent / "gate_report.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
