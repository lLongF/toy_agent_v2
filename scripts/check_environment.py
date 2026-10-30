"""验证统一 Python 系列和当前平台适用的精确依赖。"""
from __future__ import annotations

import importlib.metadata
import platform
import sys
from pathlib import Path

from packaging.requirements import Requirement


def main() -> int:
    errors = []
    if sys.version_info[:2] != (3, 11):
        errors.append(f"需要 Python 3.11.x，当前为 {platform.python_version()}")
    path = Path(__file__).resolve().parents[1] / "requirements.txt"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        req = Requirement(line)
        if req.marker and not req.marker.evaluate():
            continue
        try:
            version = importlib.metadata.version(req.name)
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"缺少依赖：{req.name}")
            continue
        if version not in req.specifier:
            errors.append(f"{req.name}=={version} 不符合 {req.specifier}")
    if errors:
        print("\n".join(errors))
        return 1
    print(f"环境检查通过：Python {platform.python_version()}，全部锁定依赖版本一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
