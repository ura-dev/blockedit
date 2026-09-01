"""1リクエストに何行詰めるかを振って、速度と品質の両方を見る。

linebatch は1行につき1リクエストを投げる。実測ではこの仕事のコストは
プロンプトの長さではなくリクエストの本数に比例していた(プロンプト処理量を
15分の1にしても速くならなかった一方、本数を1/4にすると2.1倍速くなった)。
つまり高速化の余地はリクエストを減らす方向にしかない。

ただし詰めると品質が落ちうる。落ちる原因は2つ考えられ、切り分けが要る:

  chunking  複数行をまとめて判断させること自体
  fewshot   複数行の形を見せるためにFEWSHOTを組み替えたこと

--fewshot single / merged で後者だけを入れ替えられるようにしてある。
single はFEWSHOTを本番と同じ単発5往復のまま残し、最後のuserターンだけ複数行にする。

使い方:
    uv run python chunk_probe.py                     # 1 / 4 / 8 行、両fewshot形式
    uv run python chunk_probe.py --sizes 1 4         # 詰める行数を指定
    uv run python chunk_probe.py --fewshot single    # 片方だけ
"""
import argparse
import sys
import time

import _common as C
import client
from blocks import clean

from _targets import TASK  # 負荷に使うタスクは targets.json で決める
BASELINE = "baseline_1line"


def build_chunk_messages(task, bodies, fewshot_mode):
    header = task.INSTRUCTION.strip()
    pitfall = getattr(task, "PITFALL", None)
    if pitfall:
        src, wrong, right = pitfall
        header += f"\n\nよくある誤り:\n  入力: {src}\n  誤り: {wrong}\n  正解: {right}\n"
    header += (
        "\n\n入力は複数行ある。各行にラベルを1つ、入力と同じ順序で、1行につき1語だけ出力しろ。"
        "\n出力の行数は入力の行数と必ず一致させろ。番号・説明・引用符・コードブロックを付けない。"
    )

    msgs = [{"role": "system", "content": header}]
    if fewshot_mode == "merged":
        # 複数行の形を見せる。ただし本番のFEWSHOTとは別物になる
        msgs.append({"role": "user", "content": "\n".join(s for s, _ in task.FEWSHOT)})
        msgs.append({"role": "assistant", "content": "\n".join(d for _, d in task.FEWSHOT)})
    else:
        # 本番と同じ単発5往復のまま残す
        for src, dst in task.FEWSHOT:
            msgs.append({"role": "user", "content": src})
            msgs.append({"role": "assistant", "content": dst})
    msgs.append({"role": "user", "content": "\n".join(bodies)})
    return msgs


def call_chunk(task, bodies, fewshot_mode):
    """返り値は行のリスト、または対応付けが崩れたときの None。"""
    joined = "\n".join(bodies)
    raw, finish = client.call(
        C.BASE_URL, build_chunk_messages(task, bodies, fewshot_mode), joined
    )
    if finish == "length":
        return None
    lines = [l.strip() for l in clean(raw, joined).splitlines() if l.strip()]
    return lines if len(lines) == len(bodies) else None


def run(task, bodies, size, fewshot_mode):
    start = time.monotonic()
    out, failed, reqs = [], 0, 0
    for i in range(0, len(bodies), size):
        group = bodies[i:i + size]
        reqs += 1
        if size == 1:
            # 1行のときは本番の経路をそのまま使う(比較の基準になるので替えない)
            raw, _ = client.call(C.BASE_URL, client.build_messages(task, group[0]), group[0])
            got = [clean(raw, group[0])]
        else:
            got = call_chunk(task, group, fewshot_mode)
        if got is None:
            failed += 1
            got = ["?"] * len(group)
        out.extend(got)
    return out, time.monotonic() - start, failed, reqs


def main():
    ap = argparse.ArgumentParser(description="1リクエストあたりの行数を振る")
    ap.add_argument("--sizes", type=int, nargs="*", default=[1, 4, 8])
    ap.add_argument("--fewshot", choices=["single", "merged", "both"], default="both")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    name = client.model_name(C.BASE_URL)
    if name is None:
        sys.exit(f"KoboldCppに接続できません: {C.BASE_URL}(未起動か、Forgeが常駐中)")
    task = C.load_task(TASK)
    bodies = C.collect_bodies(task)
    print(f"model: {name}   lines: {len(bodies)}")

    modes = ["single", "merged"] if args.fewshot == "both" else [args.fewshot]
    ref = C.load_reference(BASELINE)

    print(f"{'fewshot':>8} {'chunk':>5} {'wall':>8} {'lines/s':>8} {'reqs':>5} {'失敗':>4}  vs_1行")
    for size in args.sizes:
        for mode in (["single"] if size == 1 else modes):
            out, sec, failed, reqs = run(task, bodies, size, mode)
            if size == 1:
                # 1行版はこの実行の基準として保存する(モデルを替えたら取り直す)
                C.save(BASELINE, out)
                ref = out
                cmp = "基準"
            else:
                ok = C.agreement(ref, out)
                cmp = "-" if ok is None else f"{ok}/{len(ref)} {ok/len(ref)*100:.0f}%"
            C.save(f"chunk_{mode}_{size}", out)
            label = "-" if size == 1 else mode
            print(f"{label:>8} {size:>5} {sec:>7.1f}s {len(bodies)/sec:>8.2f} "
                  f"{reqs:>5} {failed:>4}  {cmp}")


if __name__ == "__main__":
    main()
