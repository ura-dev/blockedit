"""計測プローブの共通部分。

本体のロジックは複製せず、`blockedit.py` と同じモジュールをそのまま import する。
プロンプトの組み立て・サンプラ設定・出力の掃除が本番と1バイトでも違うと、計測が
本番を代表しなくなる。
"""
from pathlib import Path

import _parent  # noqa: F401  親(ランナー本体)を import path に載せる
import task as taskmod
from blocks import body, to_lines

BENCH = Path(__file__).resolve().parent
BATCH = BENCH.parent
OUT = BENCH / "out"
BASE_URL = "http://127.0.0.1:5001/v1"


def load_task(name):
    return taskmod.load(BATCH / "tasks" / name)


def collect_bodies(task):
    """PATTERN に当たる行の本体を、ファイル順・行順で集める。"""
    bodies = []
    for path in taskmod.target_files(task, None):
        for line in to_lines(path.open(encoding="utf-8", newline="").read()):
            if task.PATTERN.search(body(line)):
                bodies.append(body(line))
    return bodies


def save(tag, values):
    OUT.mkdir(exist_ok=True)
    (OUT / f"{tag}.txt").write_text("\n".join(values), encoding="utf-8")


def load_reference(tag):
    p = OUT / f"{tag}.txt"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else None


def _norm(s):
    # タスクの validate は大文字化してから判定する。生出力のまま突き合わせると
    # 表記ゆれだけの差を不一致として数え、実際に一度それで誤った数字を出した。
    return s.strip().upper()


def agreement(ref, got):
    """件数を合わせてから比較する。長さが違えば比較しない。

    途中で1件欠けた列を位置で zip すると以降が全部ずれ、実態より低い一致率が出る。
    """
    if ref is None or len(ref) != len(got):
        return None
    return sum(1 for a, b in zip(ref, got) if _norm(a) == _norm(b))
