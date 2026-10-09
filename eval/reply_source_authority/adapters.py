"""Research-only interventions; original evidence and claim invariants are unchanged."""

ARMS = ("control", "conditional_example", "source_only")
FOLLOWUP_ARMS = ("source_only", "source_explicit")


def source_contexts(kwargs):
    result = dict(kwargs)
    task = result.get("response_semantics")
    expression = result.get("response_expression")
    if task is not None:
        result["response_semantics"] = task.model_copy(update={"understanding": None, "must_address": ""})
    if expression is not None:
        result["response_expression"] = expression.model_copy(
            update={"internal_constraints": [], "disclosure_requirements": []}
        )
    return result


def install(arm):
    if arm not in {*ARMS, *FOLLOWUP_ARMS}:
        raise ValueError("unknown_source_authority_arm")
    if arm in {"source_only", "source_explicit"}:
        from harness.validator.validator import ResponseValidator

        original = ResponseValidator._llm_check

        def check(self, candidate, persona, **kwargs):
            return original(self, candidate, persona, **source_contexts(kwargs))

        ResponseValidator._llm_check = check
        if arm == "source_explicit":
            from harness.validator import validator

            original_project = validator.project_semantics

            def project(task, evidence):
                value = original_project(task, evidence)
                # Exact admitted input, not a generated summary or an oracle
                # task label. The original row still owns speaker/scope/status.
                value["source_messages"] = [
                    {"evidence_row": index, "text": evidence.entries[index].text}
                    for index in value["trigger_rows"]
                ]
                return value

            validator.project_semantics = project
    elif arm == "conditional_example":
        from harness.compiler import expression

        before = "已收到图片但工具失败时说这次没读出内容。"
        after = "已收到图片且对方要识别内容、工具失败阻止回答时说这次没读出内容。"
        if expression.EXPRESSION_RULES.count(before) != 1:
            raise ValueError("conditional_example_anchor_changed")
        expression.EXPRESSION_RULES = expression.EXPRESSION_RULES.replace(before, after)
