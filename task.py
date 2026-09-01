"""タスクファイルの読み込みと、宣言の検証。

タスクが宣言する軸は2本ある。**linebatch ではこの2本がたまたま一致していたので、
1本に見えていた。**

  UNIT   モデルに見せる塊       line / para / heading / full
  SCOPE  塊のうち改変を許す範囲  unit(塊全体)/ match(PATTERN の一致範囲だけ)

行スコープの編集は「行を見せて行全体を書き換える」なので unit、ブロックスワップは
「段落を見せて一致した文だけ消す」なので match になる。**範囲の広さがそのまま
モデルに与える裁量**なので、これは実行時オプションではなくタスクの宣言に置く。

組み合わせは実測のある4つだけを通す(`SUPPORTED`)。外れたものは起動時に落とす — 黙って劣化させず、
使い方をひねり出そうとした側にその場で分かるようにするため。
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from blocks import UNITS

HERE = Path(__file__).resolve().parent

SCOPES = ("unit", "match")

# 実測のある組み合わせだけを支持する。
#   (line, unit)  linebatch。48件の分類で一致率83.3%・不正出力0
#   (para, match) blockswap。8本で 8/8・60.6秒、素通り0件・誤検出0件
# (para, unit) は「段落を丸ごと書き直させる」= 意味を保った散文の書き換えであり、
# 判断ラダーの3段目にあたる。局所性ゲートが原理的に効かないので開けていない。
SUPPORTED = {
    ("line", "unit"),
    ("para", "match"),
    ("heading", "match"),
    ("full", "match"),
}


# 編集対象リポジトリのルート。**ランナー自身の置き場所ではない。**
#
# 以前このツールは編集対象のリポジトリの中(tools/llm/batch/)に同居していたので、
# 自分の位置から遡れば対象が取れた。単体で配布する以上その前提は消えるので、
# 対象は外から渡す。決定順は次の通り:
#
#   1. --root(明示。他のどの推測にも優先する)
#   2. 環境変数 BLOCKEDIT_ROOT
#   3. **カレントディレクトリ**の git ルート — ツールの位置ではない
#   4. カレントディレクトリ
_ROOT = None
_EXPLICIT = False


def set_root(path):
    """--root / 設定ファイルからの明示指定。推測より優先し、自己編集の門も外す。"""
    global _ROOT, _EXPLICIT
    r = Path(path).expanduser().resolve()
    if not r.is_dir():
        sys.exit(f"対象ルートが存在しません: {r}")
    _ROOT, _EXPLICIT = r, True


def root():
    global _ROOT
    if _ROOT is None:
        _ROOT = _detect_root()
        _guard_self(_ROOT)
    return _ROOT


def _detect_root():
    env = os.environ.get("BLOCKEDIT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True, cwd=Path.cwd(),
        )
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd().resolve()


def _guard_self(r):
    """推測の結果がランナー自身のリポジトリなら止める。

    このツールを clone したディレクトリの中から引数なしで起動すると、git ルートは
    ツール自身になる。そのまま走れば自分のソースを編集対象にしてしまい、しかも
    「対象0件」ではなく「それらしく動く」ので気づきにくい。明示指定なら通す。
    """
    if _EXPLICIT:
        return
    if r == HERE or HERE.is_relative_to(r) and (r / "blockedit.py").exists():
        sys.exit(
            f"対象ルートがランナー自身になっています: {r}\n"
            f"編集したいリポジトリを --root か環境変数 BLOCKEDIT_ROOT で指定してください。"
        )


def load(path):
    """タスクを読み、宣言を検証して返す。落ちるときは sys.exit で理由を出す。

    **__pycache__ を経由させない。**importlib の既定は .pyc の有効性をソースの
    mtime(秒精度)とサイズだけで判定するので、同じ秒のうちにサイズの変わらない
    書き換え — 正規表現の1文字を差し替える、`LEVEL` の数字を変える — をすると、
    古いタスクが黙って実行される。実際に踏んだ。実ファイルを書き換える道具で
    「読んだつもりのタスクと違うものが動く」は許容できないので、毎回ソースから
    compile する(タスクは小さいので実行時間には出ない)。
    """
    path = Path(path).resolve()
    spec = importlib.util.spec_from_file_location(f"blockedit_task_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), mod.__dict__)

    def need(attr, why=""):
        if not hasattr(mod, attr):
            sys.exit(f"タスクに {attr} がありません{why}: {path}")

    need("FILES")
    need("PATTERN")
    need("UNIT", f"(いずれか: {UNITS})")
    need("SCOPE", f"(いずれか: {SCOPES})")

    if mod.UNIT not in UNITS:
        sys.exit(f"UNIT は {UNITS} のいずれか: {mod.UNIT!r}")
    if mod.SCOPE not in SCOPES:
        sys.exit(f"SCOPE は {SCOPES} のいずれか: {mod.SCOPE!r}")
    if (mod.UNIT, mod.SCOPE) not in SUPPORTED:
        sys.exit(
            f"UNIT={mod.UNIT!r} と SCOPE={mod.SCOPE!r} の組み合わせは実測がなく、"
            f"支持していません。使えるのは {sorted(SUPPORTED)}: {path}")

    # LEVEL は見出し分割の正規表現 `^#{2,LEVEL} ` にそのまま埋まる。2未満だと
    # `#{2,1}` のような空の繰り返しになり、宣言の検証ではなく scan の途中で
    # re.error で落ちる。ここで落として理由を出す
    level = getattr(mod, "LEVEL", 2)
    if isinstance(level, bool) or not isinstance(level, int) or level < 2:
        sys.exit(f"LEVEL は 2 以上の整数です(分割は `##` 以上の見出しで行う): "
                 f"{level!r}: {path}")

    if not hasattr(mod, "transform") and not hasattr(mod, "INSTRUCTION"):
        sys.exit(f"タスクに transform() も INSTRUCTION もありません: {path}")

    # few-shot が要るのは行スコープだけ。実測: 素の指示は9行中8行を系統的に間違えた。
    # ブロックスワップ側は逐語コピーなので few-shot 無しで 8/8 が出ている
    if mod.UNIT == "line" and hasattr(mod, "INSTRUCTION") \
            and not getattr(mod, "FEWSHOT", None):
        sys.exit(f"UNIT='line' の LLM方式には FEWSHOT が必須です(誤答実例を最低1件): {path}")

    _resolve_residue(mod, path)
    return mod


def _resolve_residue(mod, path):
    """完了ゲートのパターンを確定させる。

    scope="match" では RESIDUE を省略させない。完了判定に PATTERN を流用すると
    完全形しか拾わないので、「開き括弧だけ消えた」ような半端な破壊を
    「残っていない」と誤判定する。実際にそう実装してE4Bの2件を取りこぼした。

    scope="unit" は塊ごと差し替わるので半端な残り方をせず、PATTERN の流用でよい
    (行スコープが元からそう動いていた)。痕跡が残ってよいタスク — 分類のように
    出力が原文と無関係になるもの — は ALLOW_PATTERN_REMAIN で外す。
    """
    if mod.SCOPE == "match":
        if not hasattr(mod, "RESIDUE"):
            sys.exit(f"SCOPE='match' には RESIDUE が必須です"
                     f"(PATTERN の流用は不可、緩いパターンを別に書く): {path}")
        return
    if not hasattr(mod, "RESIDUE"):
        mod.RESIDUE = None if getattr(mod, "ALLOW_PATTERN_REMAIN", False) else mod.PATTERN


def target_files(task, override):
    globs = override or task.FILES
    if isinstance(globs, str):
        globs = [globs]
    excludes = getattr(task, "EXCLUDE", []) or []
    r = root()
    skip = {p for g in excludes for p in r.glob(g)}
    seen, files = set(), []
    for g in globs:
        for p in sorted(r.glob(g)):
            if p.is_file() and p not in seen and p not in skip:
                seen.add(p)
                files.append(p)
    return files
