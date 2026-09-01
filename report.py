"""レポート出力と、適用後の git 検査。

**標準出力は予算である。**1件ずつログを吐かず、数行に固定する。dry-run の出口は
標準出力ではなくレポートファイルのほうで、初回実行で「何を書こうとしたのか」を
そこで確認する。受理側は `--apply` 済みなら git diff で見られるので dry-run のときだけ書く。
"""

import subprocess
from datetime import datetime
from pathlib import Path

from task import root

HERE = Path(__file__).resolve().parent
REPORTS_DIR = HERE / "reports"


def write(task_path, stats, rejects, accepted, model, mode):
    REPORTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = REPORTS_DIR / f"{Path(task_path).stem}_{ts}.txt"
    body = [f"# {task_path}  {ts}", f"# model={model}  mode={mode}", f"# {stats}", ""]
    for path, where, before, after, reason in rejects:
        rel = path.relative_to(root()).as_posix()
        body += [f"REJECTED {rel}  {where}  [{reason}]", "--- before ---",
                 before, "--- after ----", after, ""]
    if mode == "DRY-RUN":
        for path, where, before, after in accepted:
            rel = path.relative_to(root()).as_posix()
            body += [f"ACCEPTED {rel}  {where}", "--- before ---", before,
                     "--- after ----", after, ""]
    out.write_text("\n".join(body), encoding="utf-8")
    return out


def git_stat():
    out = subprocess.run(
        ["git", "diff", "--stat"], capture_output=True, text=True, cwd=root(),
    )
    return out.stdout.strip()


def any_tracked(paths):
    if not paths:
        return False
    out = subprocess.run(
        ["git", "ls-files", "--"] + [str(p) for p in paths],
        capture_output=True, text=True, cwd=root(),
    )
    return bool(out.stdout.strip())
