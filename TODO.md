# TODO

本轮（P0 + P1 + P2）安全/稳定性修复已完成。剩余代办仅是工程结构优化，不紧急。

## P3-1 — 拆分 main.py（904 行）

按职责切出独立模块：

- `config_loader.py` — 吸收 `read_config_value` + `[录制设置]` 读取逻辑。
- `url_config.py` — 吸收 `update_file` / `delete_line` / URL 解析主循环体（目前仍在 `main.py` 的模块级 `while True:` 里）。
- `recorder.py` — 吸收 `start_record` / `check_subprocess` / `converts_mp4`。
- `main.py` — 只保留启动串线、三个常驻 daemon（display、adjust_max_request、backup_file_start）、WebUI 初始化。

风险：`main.py` 里大量共享全局（`state_lock`、`max_request_lock`、`recording`、`running_list`、`url_comments`、`need_update_line_list`…）需要搬家到一个 `state.py`，否则 import 会循环。建议先抽 `state.py`，再一块块迁。

## P3-2 — 加最小 lint + pytest

- `pyproject.toml` 加 `[tool.ruff]`：`line-length = 120`、`extend-select = ["E", "F", "I"]`。
- 加 `tests/`，先给这几个纯函数写单测：
  - `src/stream.py::get_quality_index`
  - `src/utils.py::get_query_params`
  - `main.py::clean_name`（但它依赖 `clean_emoji` 全局，拆完 P3-1 后再测）

## 其他可选

- WebUI 前端目前是 Vue 3 CDN 单页面，想做远程访问的话再考虑加 token 鉴权（本轮已明确只绑 127.0.0.1）。
- 抖音 cookie 过期的刷新策略目前是手动改 `config/config.ini`，可以考虑在 WebUI 加入一个 cookie 编辑框。
