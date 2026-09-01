"""DRYを切って行スコープの速度と品質が動くかを見る。

DRYを入れたのは「temperature 0 で繰り返しに落ちるとトークン上限まで焼き切る」を
防ぐためで、ブロックスワップ側は無音破損の容疑をかけて切ってあった(真犯人はKV
キャッシュ再利用だった)。2本のランナーを1本に畳むにあたり、サンプラーが分かれて
いる理由が残っているかを実測で決めるために書いた。

結果は **DRY有無で出力が md5 まで完全一致、速度差も測定ノイズ**(14.8s 対 14.6s、
48件、12B+MTP)。よって共通の既定は DRY 無効(client.SAMPLER)を取り、DRYは
暴走が実際に出たタスクだけが `SAMPLER` で上書きする形にした。

温度0・`--nofastforward` 前提なので、同条件は同じバイトを返す。よって条件間の差は
サンプラーだけに帰せる。

    uv run python sampler_probe.py
"""
import argparse
import hashlib
import sys
import time

import _common as C
import client
from blocks import clean

from _targets import TASK  # 負荷に使うタスクは targets.json で決める
BASELINE = "baseline_1line"


def run(task, bodies):
    """本番の経路(1行1リクエスト)をそのまま通す。"""
    start = time.monotonic()
    out, failed = [], 0
    for body in bodies:
        raw, finish = client.call(C.BASE_URL, client.build_messages(task, body), body)
        if finish == "length":
            failed += 1
            out.append("?")
            continue
        out.append(clean(raw, body))
    return out, time.monotonic() - start, failed


def digest(values):
    return hashlib.md5("\n".join(values).encode("utf-8")).hexdigest()[:8]


def main():
    ap = argparse.ArgumentParser(description="DRYの有無を振る")
    ap.add_argument("--conditions", nargs="*", default=["dry", "nodry"])
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    name = client.model_name(C.BASE_URL)
    if name is None:
        sys.exit(f"KoboldCppに接続できません: {C.BASE_URL}(未起動か、Forgeが常駐中)")
    task = C.load_task(TASK)
    bodies = C.collect_bodies(task)
    print(f"model: {name}   lines: {len(bodies)}")

    default = dict(client.SAMPLER)
    stored = C.load_reference(BASELINE)
    results = {}

    print(f"{'条件':>6} {'wall':>8} {'lines/s':>8} {'切れ':>4} {'md5':>9}  vs_dry  vs_保存基準")
    for cond in args.conditions:
        # client.call は呼び出しのたびにモジュール変数を引くので、ここの差し替えが効く
        client.SAMPLER = client.DRY_SAMPLER if cond == "dry" else default
        out, sec, failed = run(task, bodies)
        results[cond] = out
        C.save(f"sampler_{cond}", out)

        ref = results.get("dry")
        ok = None if ref is None or cond == "dry" else C.agreement(ref, out)
        vs_dry = "基準" if cond == "dry" else (
            "-" if ok is None else f"{ok}/{len(ref)} {ok / len(ref) * 100:.0f}%")
        ok2 = C.agreement(stored, out)
        vs_stored = "-" if ok2 is None else f"{ok2}/{len(stored)} {ok2 / len(stored) * 100:.0f}%"

        print(f"{cond:>6} {sec:>7.1f}s {len(bodies) / sec:>8.2f} {failed:>4} "
              f"{digest(out):>9}  {vs_dry:>6}  {vs_stored}")

    client.SAMPLER = default
    if "dry" in results and "nodry" in results:
        same = digest(results["dry"]) == digest(results["nodry"])
        print("出力は完全一致" if same else "出力が違う(差分を out/sampler_*.txt で確認)")


if __name__ == "__main__":
    main()
