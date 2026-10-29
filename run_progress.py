"""发版自测进度只记录阶段和实际计数，不包含业务内容或凭据。"""
import json
from datetime import datetime
from pathlib import Path


def update_progress(cfg, stage, completed=0, total=0):
    if cfg.trigger_type != "release_selftest":
        return
    path = Path(cfg.reports_dir) / "progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"stage": stage, "completed": completed, "total": total,
            "updated_at": datetime.now().isoformat()}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
