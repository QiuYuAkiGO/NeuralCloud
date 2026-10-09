# Misty 模型调用点索引

基准：2026 年 10 月 9 日本地工作区。配合 [中文说明书](MISTY_MANUAL_ZH.md) 阅读。

静态扫描 harness 下 Python AST 中对 complete_json、complete_validation_json、complete_chat、complete_chat_result、complete_aux_text_result 的属性引用，包括直接调用和传给线程执行器的引用。包含包装层、备用、探测、研究路径；不是线上调用清单，不能相加得到单轮请求数。字符串 getattr、动态别名、直接 HTTP 或其他命名 API 可能不在此索引中。行号随代码修改会变化，所属函数用于继续定位。

本次记录 57 个属性引用，涉及 34 个文件。

## harness/agency/planner.py

[打开源码](../harness/agency/planner.py)

- 第 168 行：`AgencyService.reconsider` → `complete_chat_result`。

## harness/body_belief/appraisal.py

[打开源码](../harness/body_belief/appraisal.py)

- 第 59 行：`BodyBeliefAppraiser.appraise` → `complete_chat`。

## harness/cognitive/autonomous.py

[打开源码](../harness/cognitive/autonomous.py)

- 第 158 行：`run_or_load_assessment` → `complete_chat_result`。

## harness/cognitive/basic_terminal.py

[打开源码](../harness/cognitive/basic_terminal.py)

- 第 242 行：`BasicTerminalActionPlanner.decide` → `complete_chat_result`。
- 第 244 行：`BasicTerminalActionPlanner.decide` → `complete_chat`。

## harness/cognitive/board.py

[打开源码](../harness/cognitive/board.py)

- 第 256 行：`prepare` → `complete_chat_result`。

## harness/cognitive/capabilities.py

[打开源码](../harness/cognitive/capabilities.py)

- 第 376 行：`prepare` → `complete_chat_result`。

## harness/cognitive/diary.py

[打开源码](../harness/cognitive/diary.py)

- 第 220 行：`prepare` → `complete_chat_result`。

## harness/cognitive/diary_reflection.py

[打开源码](../harness/cognitive/diary_reflection.py)

- 第 154 行：`DiaryReflectionWorker.ask.invoke` → `complete_chat_result`。

## harness/cognitive/goals.py

[打开源码](../harness/cognitive/goals.py)

- 第 192 行：`prepare` → `complete_chat_result`。

## harness/cognitive/grounded_agency.py

[打开源码](../harness/cognitive/grounded_agency.py)

- 第 136 行：`GroundedAgencyDecisionModel.decide` → `complete_chat`。

## harness/cognitive/interruption.py

[打开源码](../harness/cognitive/interruption.py)

- 第 78 行：`InterruptionAssessor._call` → `complete_chat_result`。

## harness/cognitive/production.py

[打开源码](../harness/cognitive/production.py)

- 第 253 行：`_ask` → `complete_chat`。

## harness/cognitive/web.py

[打开源码](../harness/cognitive/web.py)

- 第 393 行：`prepare` → `complete_chat_result`。

## harness/emotion/endpoint.py

[打开源码](../harness/emotion/endpoint.py)

- 第 21 行：`DeepSeekEmotionClient.complete_json` → `complete_json`。
- 第 28 行：`DeepSeekEmotionClient.complete_json` → `complete_json`。

## harness/emotion/service.py

[打开源码](../harness/emotion/service.py)

- 第 423 行：`EmotionService._model.invoke` → `complete_json`。

## harness/llm/character.py

[打开源码](../harness/llm/character.py)

- 第 68 行：`CharacterModel._complete` → `complete_chat`。

## harness/llm/client.py

[打开源码](../harness/llm/client.py)

- 第 292 行：`OpenAICompatClient.complete_json` → `complete_chat_result`。
- 第 311 行：`OpenAICompatClient.complete_chat` → `complete_chat_result`。
- 第 612 行：`RoutingLLMClient.complete_json` → `complete_chat_result`。
- 第 620 行：`RoutingLLMClient.complete_json` → `complete_json`。
- 第 634 行：`RoutingLLMClient.complete_validation_json` → `complete_json`。
- 第 642 行：`RoutingLLMClient.complete_validation_json` → `complete_json`。
- 第 662 行：`RoutingLLMClient.complete_validation_json` → `complete_chat_result`。
- 第 667 行：`RoutingLLMClient.complete_validation_json` → `complete_json`。
- 第 677 行：`RoutingLLMClient.complete_chat` → `complete_chat`。
- 第 696 行：`RoutingLLMClient.complete_chat_result` → `complete_chat_result`。
- 第 708 行：`RoutingLLMClient.complete_chat_result` → `complete_chat_result`。
- 第 733 行：`RoutingLLMClient.complete_aux_text_result` → `complete_chat_result`。
- 第 949 行：`MockLLM.complete_chat` → `complete_chat_result`。

## harness/llm/fallback.py

[打开源码](../harness/llm/fallback.py)

- 第 58 行：`FallbackLLMClient.complete_json` → `complete_json`。
- 第 61 行：`FallbackLLMClient.complete_chat` → `complete_chat_result`。
- 第 94 行：`FallbackLLMClient.complete_chat_result` → `complete_chat_result`。
- 第 121 行：`FallbackLLMClient.complete_chat_result` → `complete_chat_result`。

## harness/memory/dual.py

[打开源码](../harness/memory/dual.py)

- 第 40 行：`DualMemoryWriter.enrich` → `complete_json`。

## harness/memory/qq_assimilation.py

[打开源码](../harness/memory/qq_assimilation.py)

- 第 488 行：`QQMemoryProposer._generate` → `complete_chat_result`。
- 第 499 行：`QQMemoryProposer._generate` → `complete_chat_result`。
- 第 505 行：`QQMemoryProposer._generate` → `complete_json`。
- 第 535 行：`QQMemoryProposer._complete_without_thinking` → `complete_chat_result`。

## harness/memory/retrieval/planner.py

[打开源码](../harness/memory/retrieval/planner.py)

- 第 34 行：`RetrievalPlanner.plan` → `complete_json`。

## harness/memory/tier.py

[打开源码](../harness/memory/tier.py)

- 第 90 行：`TierClassifier.classify` → `complete_json`。

## harness/memory/writer/curator.py

[打开源码](../harness/memory/writer/curator.py)

- 第 62 行：`LLMCurator.propose` → `complete_json`。

## harness/pipeline/parser.py

[打开源码](../harness/pipeline/parser.py)

- 第 59 行：`ContextParser.parse` → `complete_json`。

## harness/qq/decision.py

[打开源码](../harness/qq/decision.py)

- 第 1478 行：`_QQScopedLLM.complete_chat` → `complete_chat_result`。
- 第 2077 行：`decide_qq_reply` → `complete_chat_result`。
- 第 2141 行：`decide_qq_reply` → `complete_chat_result`。

## harness/qq/desktop.py

[打开源码](../harness/qq/desktop.py)

- 第 346 行：`DesktopBackend._vision_client.BoundedVision.complete_chat_result` → `complete_chat_result`。

## harness/qq/direct_images.py

[打开源码](../harness/qq/direct_images.py)

- 第 406 行：`_describe` → `complete_chat_result`。

## harness/qq/grounding.py

[打开源码](../harness/qq/grounding.py)

- 第 212 行：`inspect_qq_frame` → `complete_chat_result`。

## harness/qq/vision.py

[打开源码](../harness/qq/vision.py)

- 第 217 行：`observe_qq_frame` → `complete_chat_result`。

## harness/qq/vision_tool.py

[打开源码](../harness/qq/vision_tool.py)

- 第 399 行：`QQVisionTool.invoke` → `complete_chat_result`。

## harness/router/gate.py

[打开源码](../harness/router/gate.py)

- 第 113 行：`ComplexityGate._decide` → `complete_json`。

## harness/social_world/observations.py

[打开源码](../harness/social_world/observations.py)

- 第 108 行：`SocialWorldService.appraise` → `complete_chat_result`。

## harness/subagents/interpreter.py

[打开源码](../harness/subagents/interpreter.py)

- 第 44 行：`SocialInterpreter.interpret` → `complete_json`。

## harness/validator/validator.py

[打开源码](../harness/validator/validator.py)

- 第 461 行：`ResponseValidator._llm_check` → `complete_json`。
- 第 472 行：`ResponseValidator._llm_check` → `complete_json`。
