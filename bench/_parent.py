"""bench から本体(リポジトリルートのランナー)を import できるようにする。

bench のスクリプトは bench 直下を作業ディレクトリにして起動するので、親は
import path に載っていない。`import _parent` の副作用で載る。

**本体のロジックを bench 側へ複製しないため**の仕掛けである。分割・ゲート・
サンプラーが1バイトでも違うと、測定で出した成績が運用の成績でなくなる。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
