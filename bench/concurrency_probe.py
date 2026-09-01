"""同時実行数とエンドポイントを振って、スループットがスケールするかを見る。

KoboldCpp の `--parallelrequests`(連続バッチング)がこの負荷で効くかの検証用。
実測では効かなかったが、モデルやビルドを替えたときに再確認できるよう残してある。

対照群を必ず取ること。`--parallelrequests 1` のサーバに対して同じ同時実行をかけ、
そちらでも同じ数字が出るならフラグは効いていない。これが無いと、たまたま速かった
1回を「効いた」と読む余地が残る。

速度より先に同一性を見る。KVキャッシュ絡みで出力が静かに変わる前例があるため、
1スレッドの結果と一致しない条件は速度を問わず捨てる。

使い方:
    uv run python concurrency_probe.py                        # chat経路、1/2/4/8
    uv run python concurrency_probe.py --endpoint both        # native経路も
    uv run python concurrency_probe.py --workers 1 4
"""
import argparse
import concurrent.futures as cf
import json
import sys
import time
import urllib.request

import _common as C
import client
from blocks import clean

from _targets import TASK  # 負荷に使うタスクは targets.json で決める
HOST = C.BASE_URL.rstrip("/").removesuffix("/v1")


def flatten(messages):
    """chat の messages を素のテキストに畳む。

    トークン数をおおよそ揃えるのが目的で、チャットテンプレートの再現ではない。
    よって2つの経路の絶対時間は比較しない(スケール率だけを見る)。
    """
    return "\n".join(f"{m['role']}: {m['content']}" for m in messages) + "\nassistant:"


def call_chat(lb, task, body):
    raw, _ = client.call(C.BASE_URL, client.build_messages(task, body), body)
    return clean(raw, body)


def call_native(lb, task, body):
    payload = {
        "prompt": flatten(client.build_messages(task, body)),
        "max_length": 8,
        "temperature": 0,
        "top_p": 1,
        "rep_pen": 1.03,
    }
    req = urllib.request.Request(
        HOST + "/api/v1/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["results"][0]["text"].strip()


def run(fn, bodies, workers):
    start = time.monotonic()
    if workers == 1:
        out = [fn(b) for b in bodies]
    else:
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            out = list(ex.map(fn, bodies))
    return out, time.monotonic() - start


def main():
    ap = argparse.ArgumentParser(description="同時実行数を振ってスケールを見る")
    ap.add_argument("--workers", type=int, nargs="*", default=[1, 2, 4, 8])
    ap.add_argument("--endpoint", choices=["chat", "native", "both"], default="chat")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    name = client.model_name(C.BASE_URL)
    if name is None:
        sys.exit(f"KoboldCppに接続できません: {C.BASE_URL}(未起動か、Forgeが常駐中)")
    task = C.load_task(TASK)
    bodies = C.collect_bodies(task)
    print(f"model: {name}   lines: {len(bodies)}")

    routes = []
    if args.endpoint in ("chat", "both"):
        routes.append(("/v1/chat/completions", lambda b: call_chat(lb, task, b)))
    if args.endpoint in ("native", "both"):
        routes.append(("/api/v1/generate", lambda b: call_native(lb, task, b)))

    for label, fn in routes:
        print(f"\n--- {label} ---")
        base_sec = base_out = None
        for w in args.workers:
            out, sec = run(fn, bodies, w)
            if base_sec is None:
                base_sec, base_out = sec, out
                print(f"  workers={w:<2} wall={sec:6.1f}s  {len(bodies)/sec:5.2f} lines/s")
                continue
            diff = sum(1 for a, b in zip(base_out, out) if a != b)
            same = "一致" if diff == 0 else f"差分={diff}/{len(out)}"
            print(f"  workers={w:<2} wall={sec:6.1f}s  {len(bodies)/sec:5.2f} lines/s"
                  f"  x{base_sec/sec:.2f}  {same}")


if __name__ == "__main__":
    main()
