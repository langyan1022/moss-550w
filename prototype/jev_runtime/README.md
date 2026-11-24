# MOSS 聊天回复工具

本组件基于 [jev-chat-windows](https://github.com/jev-chat/jev-chat-windows/tree/c6d3a10474ce1ff3b7f8f6402b3b5e0e58fad5f1) 集成，固定提交为 `c6d3a10474ce1ff3b7f8f6402b3b5e0e58fad5f1`。复用了 `core/questions.py` 的对话状态和候选排序题，及 `core/draft.py` 的去重、清理和重复来信检测。原版权和分发声明见 LICENSE 与 NOTICE。

输入来自用户贴在 MOSS 的来信和回复要求。Kev-0.8B 通过本机 llama.cpp 的 System One 接口执行 Jev 的动作选择与候选排序。动作选项沿用上游的七个分类，缩短了选项说明。MOSS 当前选中的模型负责起草，并在用户指定要求时核对候选是否满足这些要求。选本机 Qwen 时全程在本机完成；选云模型时，起草与要求核对会用该云模型。界面显示通过核对且由 Kev 评分最高的一条。

默认按需加载 LM Studio 中已安装的 BF16 权重，使用六个 CPU 线程，在本次起草结束或中断时释放。若运行时或权重不在预期路径，或设置了 `MOSS_KEV_REUSE=1`，则优先复用本机 18323 端口上能通过模型与输出类型检查的 Kev 服务。现有两线程服务与按需六线程实例的三题实测分别用时 3.61–4.45 秒和 1.96–2.33 秒，后者另需启动 1.50 秒。Kev 的模型卡注明训练语言为英语，中文的推荐需要在实际回复场景验证。[权重及接口说明](https://huggingface.co/ggml-org/Kev-0.8B-GGUF) · [原模型卡](https://huggingface.co/jaredpalmer/kev-0.8b)

Kev 服务失联或设置 `MOSS_KEV_ENABLED=0` 时，起草模型先排序，再核对来信、目标和候选。MOSS 返回通过核对的首选，记录 `selector=draft_model_verified`。有合格候选时，判题服务离线也可以继续起草。
