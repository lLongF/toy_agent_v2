"""Two fixed connection scenes; secrets never belong in scene definitions."""
from dataclasses import replace

XIAOZHI_OTA_URL = "https://api.tenclass.net/xiaozhi/ota/"
# Confirmed by the user's successful Apifox test after console login.
XIAOZHI_DEVICE_ID = "ba:6a:df:36:31:16"
XIAOZHI_CLIENT_ID = "a9c66f04-362c-4834-a315-727c537e8f82"
SCENE_LABELS = {"company": "公司玩具", "xiaozhi": "小智 AI"}
TEXT_CATEGORIES = {"基础问答", "反向安全", "情感交互"}


def configure_scene(cfg, scene):
    if scene not in SCENE_LABELS:
        raise ValueError("未知接入场景")
    if scene == "company":
        return replace(cfg, connection_scene=scene, scene_identity_generated=False)
    return replace(cfg, connection_scene=scene, ota_url=XIAOZHI_OTA_URL,
                   ws_url="", token="", device_mac=XIAOZHI_DEVICE_ID, client_id=XIAOZHI_CLIENT_ID,
                   scene_identity_generated=False, environment="",
                   tools_source={"type": "local"},
                   toy_snapshot={"id": cfg.toy_id, "name": "小智 AI", "configured": True,
                                 "capabilities": {"text": "supported"}})


def prepare_scene_run(cfg):
    """Called at job creation, never during page rendering; keep identity across groups."""
    if cfg.connection_scene not in SCENE_LABELS:
        raise ValueError("未知接入场景")
    if cfg.connection_scene != "xiaozhi" or cfg.scene_identity_generated:
        return cfg
    return replace(cfg, device_mac=XIAOZHI_DEVICE_ID, client_id=XIAOZHI_CLIENT_ID,
                   token="", ws_url="", device_mac_random=False, scene_identity_generated=True)


def validate_scene_cases(cfg, groups, rules):
    if cfg.connection_scene not in SCENE_LABELS:
        raise ValueError("未知接入场景")
    if cfg.connection_scene == "xiaozhi":
        if any(rules.get("category_bases", {}).get(g.category, g.category) not in TEXT_CATEGORIES
               or any(t.expect_tool_call for t in g.turns) for g in groups):
            raise ValueError("小智 AI 首期仅支持基础问答、反向安全、情感交互及对应自定义类型；请按类别选择文本用例，不包含强制设备工具")
