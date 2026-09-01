"""ベンチの計測対象を外から受け取る。

**ベンチは自分の対象を知らない。**以前は特定のリポジトリの文書パスをソースに
直接書いていたが、それはツールを切り出した時点で二重に壊れる — 手元に無い
パスを指すし、そのリポジトリの構成が公開物に残る。

対象は `bench/targets.json`(git管理外)で宣言する。雛形は
`targets.example.json`。設定が無ければ計測せずに落ちる — 対象を推測して
「それらしい数字」を出すより、その場で止まったほうがよい。
"""

import json
import sys
from pathlib import Path

import _parent  # noqa: F401  親(ランナー本体)を import path に載せる
import task as taskmod

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "targets.json"
EXAMPLE = HERE / "targets.example.json"


def _load():
    if not CONFIG.exists():
        sys.exit(
            f"計測対象の設定がありません: {CONFIG}\n"
            f"{EXAMPLE.name} をコピーして、手元のリポジトリのパスに書き換えてください。"
        )
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"{CONFIG} を読めません: {e}")
    for key in ("root", "task", "doc", "targets", "injections"):
        if key not in cfg:
            sys.exit(f"{CONFIG} に {key!r} がありません({EXAMPLE.name} を参照)")
    return cfg


_CFG = _load()

taskmod.set_root(_CFG["root"])
ROOT = taskmod.root()

# 負荷に使うタスクファイル名(tasks/ 配下)
TASK = _CFG["task"]

# 全文スワップの単体計測で使う1本
DOC = ROOT / _CFG["doc"]

# 汎化を見るための複数本。1本ずつ独立サンプルとして回す
TARGETS = list(_CFG["targets"])

# DOC に差し込むリンク。(原文, 差し込み後) の組。平坦化すると原文に戻る
INJECTIONS = [tuple(pair) for pair in _CFG["injections"]]

# 注入アンカーに使う偽リンク先。実在しなくてよい(平坦化で消える)
FAKE_PATHS = list(_CFG.get("fake_paths") or
                  ["./notes.md", "../README.md", "../guide/index.md"])
