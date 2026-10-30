# 多玩具配置

每个玩具只需要三样，其余（OTA、设备、裁判模型、评测流程）全仓库固定，见 `config.py`。

| 文件 | 作用 |
| --- | --- |
| `toy.json` | 只写该玩具的 `token`（可选覆盖用例/工具路径） |
| `测试样例.xlsx` | 该玩具的 Excel 用例 |
| `工具描述.txt` | 该玩具的 MCP 工具清单 |

`toy.json` 字段：

- `token`：该玩具服务令牌。也可改用环境变量 `TOY_AGENT_TOKEN` 或 `TOY_AGENT_TOKEN_<ID>`
- `cases_xlsx`：可选。不写则用本目录 `测试样例.xlsx`
- `mcp_tools_file`：可选。不写则用本目录 `工具描述.txt`

## 加一个新玩具

1. 复制 `toys/_template/` 为 `toys/<id>/`
2. 在 `toy.json` 填入该玩具 token
3. 放入该玩具的 `测试样例.xlsx` 和 `工具描述.txt`
4. 跑：`python .\text_input_test.py --toy <id> --run`

`--toy` 填的就是这个文件夹名，例如目录是 `toys/story-bear/`，则：

```powershell
python .\text_input_test.py --toy story-bear --run
```

查看已配置玩具：

```powershell
python .\text_input_test.py --list-toys
```

默认玩具 `default` 仍使用仓库 `data/` 里的用例和工具。评测流程、裁判模型（Qwen + DeepSeek V4 Pro）全仓库共用，不要写进玩具配置。
