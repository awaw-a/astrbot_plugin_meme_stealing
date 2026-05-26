# astrbot_plugin_meme_stealing

QQ群表情包采集与自动回复插件。插件会按配置概率保存群友发送的图片/表情包，调用 AstrBot 已配置的多模态 LLM 生成描述、标签和情绪场景，并用关键词匹配在群聊里自动发送合适的表情包。

## 功能

- 自动采集群聊 Image 消息，默认采样概率 5%。
- `/meme_save latest` 保存当前群最近一张图片。
- 尽力支持“回复图片消息后发送 `/meme_save`”保存被回复图片；不同 QQ 适配器的回复消息结构不完全一致，若本地版本取不到被回复内容，请使用 `latest`。
- SQLite 保存元数据，图片保存到 `data/plugin_data/astrbot_plugin_meme_stealing/images/`。
- SHA-256 hash 去重。
- 通过已配置 provider 的 `text_chat(..., image_urls=[...])` 调用多模态 LLM。
- 保存前会先判断图片是否像表情包/贴纸/梗图/反应图，普通照片、文档、二维码、广告等会被跳过。
- 关键词匹配自动发送，支持 `/meme_on`、`/meme_off` 按群开关。
- 本地 FastAPI 管理面板，默认 `127.0.0.1` 仅限本机访问；配置为 `0.0.0.0` 时允许公网访问；数据 API 和图片访问需要 `admin_token` 鉴权。

## 安装

1. 将本仓库放到 AstrBot 的 `data/plugins/astrbot_plugin_meme_stealing`。
2. 在 AstrBot WebUI 重新加载插件。
3. 如需管理面板，确保安装依赖：

```bash
pip install -r requirements.txt
```

AstrBot 当前插件规范会读取 `_conf_schema.json` 并在 WebUI 生成配置。建议先修改：

- `admin_users`
- `allow_all_users_commands`
- `admin_token`
- `llm_provider`
- `group_whitelist` / `group_blacklist`
- `auto_reply_enabled`
- `meme_filter_enabled`
- `meme_filter_confidence_threshold`

## 指令

- `/meme_on`：开启当前群自动表情回复。
- `/meme_off`：关闭当前群自动表情回复。
- `/meme_save`：尝试保存被回复消息中的图片。
- `/保存表情`：同 `/meme_save`。
- `/meme_save latest`：保存当前群最近一张图片。
- `/meme_list`：列出最近保存的表情包。
- `/meme_delete <id>`：删除指定表情包和本地图片文件。
- `/meme_desc <id> <新描述>`：修改描述。
- `/meme_tags <id> <tag1,tag2,tag3>`：修改标签。
- `/meme_panel`：返回管理面板地址。
- `/meme_stats`：查看总数、启用数、待审核数、今日保存数。

默认情况下，所有聊天指令都只有 `admin_users` 中配置的管理员 QQ 号可以使用。`admin_users` 留空且 `allow_all_users_commands` 关闭时，任何人都不能使用插件指令。若希望群内所有人都能使用指令，请开启 `allow_all_users_commands`。

## 管理面板

默认地址：

```text
http://127.0.0.1:8756/?token=<admin_token>
```

面板功能包括图片预览、来源群聊、保存时间、搜索、编辑 description/tags/emotion、启用/禁用、待审核状态、单张删除和批量删除；表情包较多时可点击“加载更多”继续查看更早保存的记录。
直接打开面板地址时会显示身份校验窗口，输入插件配置里的 `admin_token` 后才能加载数据；也可以继续使用带 `?token=` 的地址自动校验。

`panel_host` 可选：

- `127.0.0.1`：仅限 AstrBot 所在机器本机访问。
- `0.0.0.0`：允许公网访问，服务会监听所有网卡；实际访问请使用服务器公网 IP 或域名，并确认端口、防火墙、NAT 或反向代理已经正确配置。

公网访问风险更高，务必修改 `admin_token`，并建议配合防火墙白名单、VPN 或反向代理鉴权使用。

也可以单独运行面板。请把 `--db` 改成实际的 `memes.sqlite3` 路径；在 AstrBot 中通常位于 `data/plugin_data/astrbot_plugin_meme_stealing/memes.sqlite3`：

```bash
python -m panel.server --db /path/to/AstrBot/data/plugin_data/astrbot_plugin_meme_stealing/memes.sqlite3 --token change-me
```

## 数据与隐私

- 默认不保存发送者 QQ 号。
- 如确实需要溯源，可打开 `store_sender_id`。
- 自动采集和自动回复都可以关闭。
- 建议在群内告知成员：bot 会学习并保存群聊中出现的表情包。
- 插件会限制图片大小、采集概率、群冷却和 LLM 调用间隔，避免过度保存或频繁调用模型。
- `meme_filter_enabled` 默认开启。开启后，图片会先由多模态 LLM 判定是否适合作为表情包保存；如果判定不是表情包，手动保存会返回原因，自动采集会静默跳过。
- 如果 LLM 判定表情包可能存在隐私泄露、淫秽色情或其他违法风险，插件会保存为待审核状态，不会参与自动回复，需管理员在面板中人工处理。

## 兼容说明

- 事件监听基于 `@filter.event_message_type(filter.EventMessageType.ALL)`。
- 发送图片使用 `event.image_result(path)`。
- LLM 标注使用 AstrBot Provider 的 `text_chat(..., image_urls=[本地图片路径])`。若你的 AstrBot 版本 provider 签名不同，请调整 `llm.py` 的 `analyze_image()` 中 provider 调用部分。
- 图片接收字段在不同适配器中可能是 `url`、`file`、`path`、base64 或 raw message dict。若某个 QQ 协议端取不到图片，请在 `image_store.py` 的 `extract_image_from_component()` 中补充对应字段。
- 回复消息强制保存依赖适配器是否提供被回复消息内容；否则使用 `/meme_save latest`。
