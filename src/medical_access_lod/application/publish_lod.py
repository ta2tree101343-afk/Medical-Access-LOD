"""公開処理。

CLAUDE.md §9: 出典・ライセンス未確定のまま公開処理は書かない。
G-1 確定後に S3/CloudFront/GitHub Pages への配置を実装する。
"""

from __future__ import annotations

from pathlib import Path


def publish(source_dir: Path) -> None:

    raise NotImplementedError("公開処理は docs/source-and-license.md 確定後に実装する。")
