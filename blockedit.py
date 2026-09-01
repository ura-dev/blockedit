"""ブロック単位の機械編集ランナー。

多数ファイルにまたがる機械的な編集を、Claudeが1ファイルずつ読み書きせずに実行する。
Claudeが書くのはタスクファイル1本だけで、対象の抽出・モデルとの往復・機械検証・適用・
レポートはこのランナーが持つ。骨格は4段:

  1. 選ぶ    — 正規表現で対象を決める。探索をモデルにさせない
  2. 送る    — 当たった塊だけ。周りは送らない
  3. 所有する — ランナーが周りを持ち、モデルの出力は塊の位置にしか入らない
  4. 棄却する — 機械検証に落ちたら適用しない

安全性の根拠は「モデルが正しいこと」ではなく構造にある。ランナーはブロックの外側と
行の終端文字を所有するので、**変更範囲外はバイト同一**が保証される。

行スコープの編集(旧 linebatch)と段落スワップ(旧 blockswap)はこの骨格の上の
タスク型2つで、違いはタスクファイルの UNIT / SCOPE 宣言だけ。

  uv run python blockedit.py tasks/foo.py --select-only  # 選択だけ見る(0トークン)
  uv run python blockedit.py tasks/foo.py                # dry-run(既定)
  uv run python blockedit.py tasks/foo.py --limit 3      # 先頭3ブロックだけ
  uv run python blockedit.py tasks/foo.py --apply        # 書き込む

タスクファイルの書き方は README.md を参照。
"""

import argparse
import http.client
import sys
import time
import urllib.error
from pathlib import Path

import client
import gates
import report
import task as taskmod
from blocks import (block_text, clean, join_blocks, relines, split,
                    split_trailing_blanks)
from task import root

# ブロック1個の送信サイズ上限。測定で通した最大ブロックは 7,980B(見出し分割)。
# これを超えるブロックはモデルに送らず落として報告する — 上限不足による打ち切りを
# 「変換が下手」と混同しないため。
DEFAULT_MAX_BLOCK_BYTES = 8000


def where(unit, i):
    """レポートに出す位置。行スコープなら行番号、それ以外はブロック番号。"""
    return f"L{i + 1}" if unit == "line" else f"block={i}"


def scan(task, files, unit, level, limit):
    """モデルを呼ばずに選択だけ行う。ファイルごとの (blocks, hot indices) を返す。"""
    out, hit = [], 0
    for path in files:
        text = path.open(encoding="utf-8", newline="").read()
        blocks = split(text, unit, level)
        hot = [i for i, b in enumerate(blocks) if task.PATTERN.search(block_text(b))]
        if limit:
            hot = hot[:max(0, limit - hit)]
        hit += len(hot)
        if hot:
            out.append((path, text, blocks, hot))
        if limit and hit >= limit:
            break
    return out, hit


def produce(task, frag, use_llm, base_url, sampler):
    """1ブロック分の出力を得る。返り値は (本文, finish_reason)。

    ゲートに落ちた出力を温度0で投げ直さない。貪欲デコードは同条件なら同じバイトを
    返すので、リトライが1文字も変えないことが実測で分かっている。振り直すには温度を
    上げるしかなく、それでも直るのはMTPが反転させた僅差の1点だけ(12B+MTP+1回
    リトライで 6/8 が上限)。既定は1回で棄却してレポートへ送り、判断を呼び出し元に返す。
    """
    if not use_llm:
        return task.transform(frag), "stop"
    raw, finish = client.call(base_url, client.build_messages(task, frag), frag, sampler)
    return clean(raw, frag), finish


def run(task, selection, unit, scope, use_llm, base_url, apply_, max_bytes):
    stats = {"hit": 0, "applied": 0, "rejected": 0}
    rejects, accepted, llm_seconds, touched = [], [], 0.0, []
    sampler = getattr(task, "SAMPLER", None)
    retry_temp = getattr(task, "RETRY_TEMPERATURE", None)

    for path, text, blocks, hot in selection:
        mtime = path.stat().st_mtime_ns
        out_blocks = [list(b) for b in blocks]
        accepted_idx = []

        for i in hot:
            core, tail = split_trailing_blanks(blocks[i])
            frag = block_text(core)
            pos = where(unit, i)
            stats["hit"] += 1

            if len(frag.encode("utf-8")) > max_bytes:
                stats["rejected"] += 1
                rejects.append((path, pos, frag, "",
                                f"ブロックが上限超過({len(frag.encode('utf-8'))}B "
                                f"> {max_bytes}B)— 送信せず"))
                continue

            start = time.monotonic()
            try:
                new, finish = produce(task, frag, use_llm, base_url, sampler)
                reason = gates.check(task, frag, new, finish, max_bytes, scope)
                if reason and use_llm and retry_temp:
                    # 温度を上げた振り直しは任意。既定では走らない(produce の注記)
                    s = dict(sampler or client.SAMPLER, temperature=retry_temp)
                    new, finish = produce(task, frag, use_llm, base_url, s)
                    reason = gates.check(task, frag, new, finish, max_bytes, scope)
            except (urllib.error.URLError, KeyError, TimeoutError,
                    http.client.IncompleteRead) as e:
                stats["rejected"] += 1
                rejects.append((path, pos, frag, "", f"API失敗: {e}"))
                continue
            finally:
                llm_seconds += time.monotonic() - start

            if reason:
                stats["rejected"] += 1
                rejects.append((path, pos, frag, new, reason))
                continue

            # 終端文字はランナーが所有する。CRLFの `\r` を本文と一緒にモデルへ渡すと
            # 返ってこず、ゲートは splitlines() 判定でCRに盲目なので素通りし、
            # ブロック全行の改行コードが黙ってLFに変わる。混在ブロックは貼らない
            new_lines = relines(new, core)
            if new_lines is None:
                stats["rejected"] += 1
                rejects.append((path, pos, frag, new, "ブロック内で改行コードが混在している"))
                continue

            out_blocks[i] = new_lines + tail
            accepted_idx.append(i)
            accepted.append((path, pos, frag, new))
            stats["applied"] += 1

        if not accepted_idx:
            continue

        assembled = join_blocks(out_blocks)
        # 構造的な保証をassertで固定する。受理していないブロックが1バイトでも
        # 動いていたら、モデルではなくランナーが壊れている
        for j, b in enumerate(blocks):
            if j not in accepted_idx and out_blocks[j] != b:
                raise AssertionError(f"{path}: 未受理ブロック {j} が変化した")
        if assembled == text:
            raise AssertionError(f"{path}: 受理したのに全体が無変更")

        touched.append(path)
        if apply_:
            if path.stat().st_mtime_ns != mtime:
                stats["rejected"] += len(accepted_idx)
                stats["applied"] -= len(accepted_idx)
                rejects.append((path, "-", "", "", "処理中にファイルが変更された — 書き込み中止"))
                touched.pop()
                continue
            with path.open("w", encoding="utf-8", newline="") as fh:
                fh.write(assembled)

    return stats, rejects, accepted, llm_seconds, touched


def main():
    # Windowsの既定コードページだと日本語の出力が化ける。argparse のヘルプも
    # 起動時の sys.exit も日本語なので、引数を読む前に揃える
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="ブロック単位の機械編集")
    ap.add_argument("task", help="タスクファイルのパス")
    ap.add_argument("--apply", action="store_true", help="実際に書き込む(既定はdry-run)")
    ap.add_argument("--limit", type=int, default=0, help="処理するブロック数の上限")
    ap.add_argument("--files", nargs="*", default=None, help="FILES を上書きするglob")
    ap.add_argument("--select-only", action="store_true",
                    help="選択だけ出してモデルを呼ばない(0トークン)")
    ap.add_argument("--base-url", default=client.DEFAULT_BASE_URL)
    ap.add_argument("--root", default=None,
                    help="編集対象リポジトリのルート。既定はカレントディレクトリの"
                         "gitルート(環境変数 BLOCKEDIT_ROOT でも指定できる)")
    args = ap.parse_args()

    if args.root:
        taskmod.set_root(args.root)

    task = taskmod.load(args.task)
    unit, scope = task.UNIT, task.SCOPE
    level = getattr(task, "LEVEL", 2)
    max_bytes = getattr(task, "MAX_BLOCK_BYTES", DEFAULT_MAX_BLOCK_BYTES)
    use_llm = not hasattr(task, "transform")

    files = taskmod.target_files(task, args.files)
    if not files:
        sys.exit("対象ファイルが0件です。FILES のglobを確認してください")

    selection, hit = scan(task, files, unit, level, args.limit)
    if args.select_only:
        for path, _text, blocks, hot in selection:
            rel = path.relative_to(root()).as_posix()
            for i in hot:
                frag = block_text(split_trailing_blanks(blocks[i])[0])
                print(f"{rel}  {where(unit, i)}/{len(blocks)}  "
                      f"{len(frag.encode('utf-8'))}B")
        print(f"[SELECT] files={len(files)} matched={len(selection)} hit={hit} "
              f"unit={unit} scope={scope}")
        return 0
    if not hit:
        print(f"[DRY-RUN] files={len(files)} hit=0 applied=0 rejected=0")
        return 0

    model = "(transform)"
    if use_llm:
        model = client.model_name(args.base_url)
        if model is None:
            sys.exit(f"LLMサーバに接続できません: {args.base_url}"
                     f"(未起動か、別プロセスがGPUを掴んでいる)")
        print(f"model: {model}")

    stats, rejects, accepted, llm_seconds, touched = run(
        task, selection, unit, scope, use_llm, args.base_url, args.apply, max_bytes)

    # 標準出力は予算である。1件ずつログを吐かない
    mode = "APPLY" if args.apply else "DRY-RUN"
    line = (f"[{mode}] files={len(files)} hit={stats['hit']} "
            f"applied={stats['applied']} rejected={stats['rejected']} "
            f"touched={len(touched)}")
    if use_llm:
        line += f" llm={llm_seconds:.1f}s"
    print(line)

    if rejects or (accepted and not args.apply):
        # レポートはランナー側の reports/ に出る。対象ルートの外にあるので
        # ROOT からの相対にはできない(切り出し前は同じリポジトリ内だった)
        out = report.write(args.task, stats, rejects, accepted, model, mode)
        try:
            shown = out.relative_to(Path.cwd()).as_posix()
        except ValueError:
            shown = out.as_posix()
        print("report: " + shown)

    if args.apply:
        stat = report.git_stat()
        # 無音のno-op検出。適用したと言いながらdiffが空なら壊れている
        # (未追跡ファイルだけを触った場合は diff に出なくて正常)
        if stats["applied"] and not stat and report.any_tracked(touched):
            print("警告: applied>0 なのに git diff が空です")
        print(stat or "(diff なし)")

    return 1 if stats["rejected"] else 0


if __name__ == "__main__":
    sys.exit(main())
