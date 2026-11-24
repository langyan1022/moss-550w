# HoloJarvis Brain

`brain.py` 的 `Brain.ask_stream`、工具增量合并、`_run_tools` 和工具 schema 转换取自 HoloJarvis 提交 `7473f91e755b3683173de025c5ee32032dbdd75a`。上游作者为 wang64862，MIT 许可随代码提供。[固定版本源码](https://github.com/wqq64842-commits/holojarvis/blob/7473f91e755b3683173de025c5ee32032dbdd75a/jarvis/brain.py)

本地版本将读取模型流与执行工具改为 asyncio。每轮由 MOSS 传入允许使用的工具、已选记忆及本会话的历史。模型传输复用 MOSS 配置的客户端。纯文字回答按生成进度更新，带工具的轮次等工具调用结束再发布最终正文。中断的模型请求直接退出，工具名称、调用编号、参数 JSON 和完成原因均会校验。
