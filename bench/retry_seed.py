"""ゲートに弾かれたdocを、温度とシードだけ変えて投げ直したら回復するかを測る。

狙いは「12Bのままリトライで直せるか」。直せるならサーバ再起動(12B->26B、実測35秒)も
カスケードのオーケストレーションも要らず、同じサーバへペイロードを変えて投げるだけになる。

測る順に2段:

  1. シードは効くのか。温度0では貪欲法なので構造的に効かないはず(乱数を参照しない)。
     温度>0で「同じシードを2回=一致 / 別シード=不一致」が出れば、シードは尊重されている。
     温度>0でも全部一致するなら、シードを振っても独立サンプルは取れない。
     multidoc_swap.py の冒頭コメントはこれを「一致する」と書いているが、
     その観測はKVキャッシュ汚染時代のもので、--nofastforward 後に測り直していない。

  2. 回復するのか。シードが効いたとして、exactを引けるのか、
     それとも既に正しく出ている2900トークン側を別の場所で壊すだけなのか。

対象は 12B+MTP がゲートに弾かれた3本。実docは読み取り専用。

  uv run python retry_seed.py
"""

import argparse
import hashlib
import json
import sys
import time
import urllib.error
from datetime import datetime
from pathlib import Path

from multidoc_swap import pick_injections
from rewrite_swap import (PROMPT_A, ROOT, SAMPLERS, call, classify, similarity,
                          strip_fence)
import _parent  # noqa: F401  親(ランナー本体)を import path に載せる
from gates import LINK_RE, RESIDUE_RE, gate

OUT_DIR = Path(__file__).resolve().parent / "results"

# 計測対象は targets.json で宣言する。実際には multidoc の結果 json を見て
# ゲートに弾かれた文書だけを --targets で絞り込んで再試行する
from _targets import TARGETS

# (temp, seed)。同じ組を2回並べているのは「シードが尊重されているか」を見るため。
#
# 温度0.6は打ち切った。hidden_lore と sabotage_route の計12試行で、0.6 の出力md5が
# 同じシードの 0.3 と1つ残らず一致したため。逐語コピーは分布が極端に尖っていて、
# 温度をこの範囲で動かしてもargmaxが変わらない。動くのは僅差の点だけで、
# そこを決めているのはシードのほう。温度は情報を持たないノブだった。
MATRIX = [
    (0.0, 1),      # 対照。温度0でシードを振っても変わらないはず
    (0.0, 999),
    (0.3, 1),      # 同一シードの反復 -> 一致すればシードは尊重されている
    (0.3, 1),
    (0.3, 2),      # 別シード -> 変われば独立サンプルが取れる
    (0.3, 3),
]


def build(rel):
    pristine = (ROOT / rel).read_text(encoding="utf-8")
    pairs = pick_injections(pristine)
    if len(pairs) < 3:
        return None, None
    injected = pristine
    for old, new in pairs:
        injected = injected.replace(old, new, 1)
    return pristine, injected


def run_trial(rel, pristine, injected, temp, seed, idx, sampler, raw_dir):
    # call() は **sampler をペイロードに展開するので、call 自体を触らずに
    # シードを差し込める。KoboldCpp は OAI 側 seed / ネイティブ側 sampler_seed の
    # 両方を持つので両方載せる(未知キーは無視される)
    sam = dict(SAMPLERS[sampler])
    sam["seed"] = seed
    sam["sampler_seed"] = seed

    msgs = [{"role": "user", "content": PROMPT_A + "\n\n---\n" + injected}]
    t0 = time.monotonic()
    raw, finish, usage = call(msgs, max_tokens=12000, temperature=temp, sampler=sam)
    dt = time.monotonic() - t0

    result = strip_fence(raw)
    kind, hunks = classify(result, pristine)
    accepted, reasons, _ = gate(injected, result, LINK_RE, RESIDUE_RE)
    name = rel.rsplit("/", 1)[-1].replace(".md", "")
    (raw_dir / f"retry_{name}_t{temp}_s{seed}_{idx}.md").write_text(raw, encoding="utf-8")

    return {
        "doc": rel, "temp": temp, "seed": seed, "idx": idx,
        "kind": kind, "ok": kind == "exact", "gate_accepted": accepted,
        "gate_reasons": reasons[:2], "seconds": round(dt, 1),
        "finish_reason": finish, "out_tokens": usage.get("completion_tokens"),
        "similarity": round(similarity(result, pristine), 5),
        "bytes": len(result.encode("utf-8")),
        "bytes_expected": len(pristine.rstrip("\n").encode("utf-8")),
        "md5": hashlib.md5(result.encode("utf-8")).hexdigest()[:12],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sampler", choices=list(SAMPLERS), default="nodry")
    ap.add_argument("--only", default="",
                    help="docパスの部分一致で対象を絞る(中断した分の再開用)")
    ap.add_argument("--temps", default="",
                    help="温度をカンマ区切りで絞る 例: 0.3")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    targets = [t for t in TARGETS if args.only in t]
    if args.temps:
        want = {float(x) for x in args.temps.split(",")}
        matrix = [(t, s) for t, s in MATRIX if t in want]
    else:
        matrix = MATRIX
    if not targets or not matrix:
        sys.exit(f"対象が空: docs={len(targets)} trials={len(matrix)}")

    raw_dir = OUT_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"retryseed_{args.sampler}_{ts}.json"

    records = []
    for rel in targets:
        pristine, injected = build(rel)
        if pristine is None:
            print(f"SKIP (アンカー不足): {rel}", flush=True)
            continue
        print(f"\n=== {rel} ===", flush=True)
        for idx, (temp, seed) in enumerate(matrix):
            try:
                rec = run_trial(rel, pristine, injected, temp, seed, idx,
                                args.sampler, raw_dir)
            except (urllib.error.URLError, TimeoutError, KeyError) as e:
                rec = {"doc": rel, "temp": temp, "seed": seed, "idx": idx,
                       "kind": "error", "ok": False, "error": repr(e)}
            records.append(rec)
            out.write_text(json.dumps(records, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            print(f"  t={rec['temp']:<4} seed={rec['seed']:<4} "
                  f"{rec['kind']:14} gate={str(rec.get('gate_accepted')):5} "
                  f"md5={rec.get('md5')} bytes={rec.get('bytes')}/"
                  f"{rec.get('bytes_expected')} tok={rec.get('out_tokens')} "
                  f"sim={rec.get('similarity')} {rec.get('seconds')}s", flush=True)
            for r in rec.get("gate_reasons") or []:
                print(f"      ! {r[:100]}", flush=True)

    print("\n" + "=" * 62)
    print("シードが効いているか(md5の異なり数 / 試行数)")
    for rel in targets:
        rs = [r for r in records if r["doc"] == rel and r.get("md5")]
        name = rel.rsplit("/", 1)[-1]
        for temp in sorted({r["temp"] for r in rs}):
            g = [r for r in rs if r["temp"] == temp]
            print(f"  {name:26} t={temp:<4} 異なるmd5 {len({r['md5'] for r in g})}/{len(g)}")

    print("\n回復したか(exact / 試行数)")
    for rel in targets:
        rs = [r for r in records if r["doc"] == rel]
        n_ok = sum(1 for r in rs if r.get("ok"))
        n_gate = sum(1 for r in rs if r.get("gate_accepted"))
        print(f"  {rel.rsplit('/', 1)[-1]:26} exact {n_ok}/{len(rs)}   "
              f"ゲート受理 {n_gate}/{len(rs)}")
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
