"""CDK が合成した ASL 定義を Python 側で駆動するためのシミュレータ。

目的: pipeline-stack.ts の Step Functions ステート機械 (Pass / Task / Choice /
Fail) を Python 側で再現し、実 Lambda ハンドラを組み合わせて E2E で検証する。
これにより「Scheduler 入力 → 各 Lambda に渡る payload」が Pydantic の
DownloadEvent / NormalizeEvent / ... の必須項目を満たすことを確認できる。

参考: infra/lib/pipeline-stack.ts の InjectContext + resultPath 構成。
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).parent.parent.parent
_INFRA_DIR = _REPO_ROOT / "infra"
_TEMPLATE_PATH = _INFRA_DIR / "cdk.out" / "MedicalAccessLod-dev-Pipeline.template.json"


@dataclass(frozen=True)
class ExtractedStateMachine:
    """ASL本体と、Fn::Joinに埋め込まれていたCFN式の対応表。"""

    definition: dict[str, Any]
    tokens: dict[str, Any]


@dataclass(frozen=True)
class SynthesizedPipeline:
    """E2Eで使う、CloudFormationトークン解決済みのPipeline定義。"""

    definition: dict[str, Any]
    scheduler_input: dict[str, Any]
    context_parameters: dict[str, str]
    task_function_keys: dict[str, str]


def _load_synthesized_templates() -> dict[str, dict[str, Any]]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in (_INFRA_DIR / "cdk.out").glob("*.template.json")
    }


def synth_pipeline() -> SynthesizedPipeline:
    """毎回CDK synthし、実Scheduler入力・ImportValue・Lambda対応を解決する。

    キャッシュ (`infra/cdk.out/`) は使わない。ローカルの cdk.out が古いまま
    テストが緑になる (実 ASL と乖離する) 事故を避けるため、常に synth し直す。
    session-scoped fixture から呼ばれる想定で、実行時間 (~数秒) は許容する。
    """

    subprocess.run(
        ["npx", "cdk", "synth", "--all", "--quiet"],
        cwd=str(_INFRA_DIR),
        check=True,
        capture_output=True,
    )
    templates = _load_synthesized_templates()
    pipeline_template = templates.get(_TEMPLATE_PATH.name)
    if pipeline_template is None:
        raise LookupError(f"Synthesized pipeline template not found: {_TEMPLATE_PATH.name}")

    extracted = extract_state_machine_definition(pipeline_template)
    definition = _resolve_cfn_tokens(extracted, pipeline_template, templates)
    scheduler_input = extract_scheduler_input(pipeline_template)

    inject = definition["States"].get("InjectContext")
    if inject is None or inject.get("Type") != "Pass":
        raise LookupError("InjectContext Pass state not found")
    context_parameters = {
        key: value
        for key, value in inject.get("Parameters", {}).items()
        if not key.endswith(".$") and isinstance(value, str)
    }
    required_context = {
        "raw_bucket",
        "normalized_bucket",
        "build_bucket",
        "dist_bucket",
        "read_model_table",
    }
    missing_context = required_context - context_parameters.keys()
    if missing_context:
        raise ValueError(f"Unresolved InjectContext values: {sorted(missing_context)}")

    task_function_keys: dict[str, str] = {}
    for state_name, state in definition["States"].items():
        if state.get("Type") != "Task":
            continue
        function_key = state.get("Resource")
        if not isinstance(function_key, str):
            raise TypeError(f"Task Resource was not resolved for {state_name}")
        task_function_keys[state_name] = function_key

    return SynthesizedPipeline(
        definition=definition,
        scheduler_input=scheduler_input,
        context_parameters=context_parameters,
        task_function_keys=task_function_keys,
    )


def extract_state_machine_definition(template: dict[str, Any]) -> ExtractedStateMachine:
    """DefinitionStringを展開し、各CFN式を一意なトークンとして保持する。

    全式を同じ文字列へ潰すと、bucket/tableやTask Lambdaを手動で正解へ
    差し替えられてしまう。ここでは式ごとのトークンと元の式を保持し、後段で
    ImportValueやFn::GetAttを合成テンプレートから機械的に解決する。
    """

    for resource in template.get("Resources", {}).values():
        if resource.get("Type") != "AWS::StepFunctions::StateMachine":
            continue
        raw = resource["Properties"]["DefinitionString"]
        if isinstance(raw, dict) and "Fn::Join" in raw:
            parts = raw["Fn::Join"][1]
            tokens: dict[str, Any] = {}
            rendered: list[str] = []
            for part in parts:
                if isinstance(part, str):
                    rendered.append(part)
                    continue
                token = f"__CFN_TOKEN_{len(tokens)}__"
                tokens[token] = part
                rendered.append(token)
            return ExtractedStateMachine(
                definition=json.loads("".join(rendered)),
                tokens=tokens,
            )
        if isinstance(raw, str):
            return ExtractedStateMachine(definition=json.loads(raw), tokens={})
    raise LookupError("No AWS::StepFunctions::StateMachine in template")


def extract_scheduler_input(template: dict[str, Any]) -> dict[str, Any]:
    schedules = [
        resource
        for resource in template.get("Resources", {}).values()
        if resource.get("Type") == "AWS::Scheduler::Schedule"
    ]
    if len(schedules) != 1:
        raise LookupError(f"Expected one Scheduler resource, found {len(schedules)}")
    raw_input = schedules[0]["Properties"]["Target"]["Input"]
    if not isinstance(raw_input, str):
        raise TypeError("Scheduler Target.Input must synthesize to a JSON string")
    result = json.loads(raw_input)
    if not isinstance(result, dict):
        raise TypeError("Scheduler Target.Input must be a JSON object")
    return result


def _resource_ref_value(template: dict[str, Any], logical_id: str) -> str:
    resource = template.get("Resources", {}).get(logical_id)
    if resource is None:
        raise LookupError(f"Referenced resource not found: {logical_id}")
    properties = resource.get("Properties", {})
    property_by_type = {
        "AWS::S3::Bucket": "BucketName",
        "AWS::DynamoDB::Table": "TableName",
        "AWS::ECR::Repository": "RepositoryName",
    }
    property_name = property_by_type.get(resource.get("Type"))
    value = properties.get(property_name) if property_name else None
    if not isinstance(value, str):
        raise TypeError(f"Cannot resolve Ref for {logical_id} ({resource.get('Type')})")
    return value


def _resolve_import_value(
    export_name: str,
    templates: dict[str, dict[str, Any]],
) -> str:
    matches: list[tuple[dict[str, Any], Any]] = []
    for template in templates.values():
        for output in template.get("Outputs", {}).values():
            if output.get("Export", {}).get("Name") == export_name:
                matches.append((template, output.get("Value")))
    if len(matches) != 1:
        raise LookupError(f"Expected one export named {export_name!r}, found {len(matches)}")
    template, value = matches[0]
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("Ref"), str):
        return _resource_ref_value(template, value["Ref"])
    raise TypeError(f"Unsupported exported value for {export_name!r}: {value!r}")


def _resolve_task_token(template: dict[str, Any], expression: dict[str, Any]) -> str:
    """Task Resource / Parameters 内の CFN 式を、シミュレータが扱える値に解決する。

    - `Fn::GetAtt: [<LambdaLogicalId>, "Arn"]` → Lambda の FUNCTION_KEY
    - `Fn::GetAtt: [<QueueLogicalId>, "QueueName"]` → 物理キュー名
    - `Ref: <QueueLogicalId>` → 物理キュー名 (SQS の QueueUrl 相当)
    - `Ref: "AWS::Partition"` → "aws"
    - `Ref: "AWS::Region"` / `"AWS::AccountId"` などの AWS 疑似パラメータ
    """
    if isinstance(expression, dict):
        get_att = expression.get("Fn::GetAtt")
        if (
            isinstance(get_att, list)
            and len(get_att) == 2
            and isinstance(get_att[0], str)
        ):
            resource = template.get("Resources", {}).get(get_att[0])
            if resource is None:
                raise LookupError(f"CFN resource {get_att[0]!r} not found")
            attr = get_att[1]
            rtype = resource.get("Type")
            if rtype == "AWS::Lambda::Function" and attr == "Arn":
                function_key = (
                    resource.get("Properties", {})
                    .get("Environment", {})
                    .get("Variables", {})
                    .get("FUNCTION_KEY")
                )
                if not isinstance(function_key, str) or not function_key:
                    raise ValueError(f"Lambda {get_att[0]} has no FUNCTION_KEY")
                return function_key
            if rtype == "AWS::SQS::Queue" and attr == "QueueName":
                name = resource.get("Properties", {}).get("QueueName")
                if isinstance(name, str):
                    return name
                raise TypeError(f"SQS queue {get_att[0]} has no QueueName")
            raise TypeError(
                f"Unsupported Fn::GetAtt attribute for {rtype}: {attr!r}"
            )
        ref = expression.get("Ref")
        if isinstance(ref, str):
            if ref == "AWS::Partition":
                return "aws"
            if ref == "AWS::Region":
                return "ap-northeast-1"
            if ref == "AWS::AccountId":
                return "111111111111"
            resource = template.get("Resources", {}).get(ref)
            if resource is not None:
                rtype = resource.get("Type")
                if rtype == "AWS::SQS::Queue":
                    # Ref of Queue returns QueueUrl. Simulator は URL 全体は不要で、
                    # queue 名だけあれば dispatch できるので QueueName を返す。
                    name = resource.get("Properties", {}).get("QueueName")
                    if isinstance(name, str):
                        return f"sqs-queue://{name}"
                if rtype == "AWS::S3::Bucket":
                    name = resource.get("Properties", {}).get("BucketName")
                    if isinstance(name, str):
                        return name
                if rtype == "AWS::DynamoDB::Table":
                    name = resource.get("Properties", {}).get("TableName")
                    if isinstance(name, str):
                        return name
            raise LookupError(f"Unsupported Ref target: {ref!r}")
    raise TypeError(f"Unsupported CFN expression: {expression!r}")


def _replace_tokens(value: Any, replacements: dict[str, str]) -> Any:
    """置換対象 (token → resolved value) を再帰的に適用する。

    Fn::Join の内側では ARN 断片が組み立てられるため、単純な dict lookup では
    足らず、文字列内の token 部分置換も必要 (例:
    "arn:__CFN_TOKEN_11__:states:::sqs:sendMessage")。
    """
    if isinstance(value, str):
        # 完全一致優先 (bucket 名などの単独トークン)
        if value in replacements:
            return replacements[value]
        # 部分置換 (ARN 断片など)
        replaced = value
        for token, resolved in replacements.items():
            if token in replaced:
                replaced = replaced.replace(token, resolved)
        return replaced
    if isinstance(value, list):
        return [_replace_tokens(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_tokens(item, replacements) for key, item in value.items()}
    return value


def _resolve_cfn_tokens(
    extracted: ExtractedStateMachine,
    pipeline_template: dict[str, Any],
    templates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    replacements: dict[str, str] = {}
    for token, expression in extracted.tokens.items():
        if not isinstance(expression, dict):
            raise TypeError(f"Unsupported CloudFormation expression: {expression!r}")
        import_name = expression.get("Fn::ImportValue")
        if isinstance(import_name, str):
            replacements[token] = _resolve_import_value(import_name, templates)
        else:
            # Fn::GetAtt / Ref (Lambda Arn / SQS QueueName / AWS pseudo params 等)
            replacements[token] = _resolve_task_token(pipeline_template, expression)

    resolved = _replace_tokens(deepcopy(extracted.definition), replacements)
    if not isinstance(resolved, dict):
        raise TypeError("Resolved state-machine definition is not an object")
    return resolved


_TOKEN_KEY_RE = re.compile(r"\.\$$")


class ASLSimulator:
    """最小限の ASL 実行器 (Pass / Task / Choice / Fail / Succeed)."""

    def __init__(self, definition: dict[str, Any], *, execution_name: str) -> None:
        self.states: dict[str, dict[str, Any]] = definition["States"]
        self.start_at: str = definition["StartAt"]
        self.context: dict[str, Any] = {"Execution": {"Name": execution_name}}
        self.trace: list[tuple[str, str]] = []  # (state_name, type)
        self.invocations: list[tuple[str, str]] = []  # (state_name, FUNCTION_KEY)

    # ---- JSONPath ヘルパ (SFN の限定サブセット) ----

    @staticmethod
    def _traverse(obj: Any, path: list[str]) -> Any:
        for p in path:
            obj = obj[p]
        return obj

    def _resolve_reference(self, ref: str, state: dict[str, Any]) -> Any:
        if ref.startswith("$$."):
            return self._traverse(self.context, ref[3:].split("."))
        if ref.startswith("$."):
            return self._traverse(state, ref[2:].split("."))
        if ref == "$":
            return state
        return ref

    def _resolve_parameters(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in params.items():
            if _TOKEN_KEY_RE.search(key) and isinstance(value, str):
                clean = key[:-2]
                result[clean] = self._resolve_reference(value, state)
            elif isinstance(value, dict):
                result[key] = self._resolve_parameters(value, state)
            else:
                result[key] = value
        return result

    @staticmethod
    def _set_at_path(state: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
        if path == "$":
            return value if isinstance(value, dict) else {"result": value}
        parts = path[2:].split(".")
        obj = state
        for p in parts[:-1]:
            obj = obj.setdefault(p, {})
        obj[parts[-1]] = value
        return state

    # ---- Choice ----

    def _evaluate_choice(self, spec: dict[str, Any], state: dict[str, Any]) -> str | None:
        for choice in spec.get("Choices", []):
            variable = choice["Variable"]
            actual = self._resolve_reference(variable, state)
            if "BooleanEquals" in choice and actual == choice["BooleanEquals"]:
                return choice["Next"]
            if "StringEquals" in choice and actual == choice["StringEquals"]:
                return choice["Next"]
        return spec.get("Default")

    # ---- メインループ ----

    def run(
        self,
        initial_input: dict[str, Any],
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]],
    ) -> dict[str, Any]:
        state: Any = dict(initial_input)
        current: str | None = self.start_at
        while current is not None:
            spec = self.states[current]
            state_type = spec["Type"]
            self.trace.append((current, state_type))

            if state_type == "Pass":
                state = self._resolve_parameters(spec.get("Parameters", {}), state)
                current = spec.get("Next")

            elif state_type == "Task":
                # payloadResponseOnly=true では Parameters が直接 Lambda 入力になる
                # (Payload wrapper なし)
                raw_params = spec.get("Parameters", {})
                payload_spec = raw_params.get("Payload", raw_params)
                lambda_input = self._resolve_parameters(payload_spec, state)
                function_key = spec.get("Resource")
                if not isinstance(function_key, str):
                    raise TypeError(f"Task Resource was not resolved for {current!r}")
                handler = handlers.get(function_key)
                if handler is None:
                    raise KeyError(
                        f"No handler registered for task {current!r} "
                        f"(FUNCTION_KEY={function_key!r})"
                    )
                self.invocations.append((current, function_key))
                result = handler(lambda_input)
                result_path = spec.get("ResultPath", "$")
                state = self._set_at_path(state, result_path, result)
                current = spec.get("Next")

            elif state_type == "Choice":
                current = self._evaluate_choice(spec, state)

            elif state_type == "Fail":
                raise RuntimeError(
                    f"State machine failed at {current!r}: {spec.get('Cause', 'unknown cause')}"
                )

            elif state_type == "Succeed":
                current = None

            else:
                raise NotImplementedError(f"Unsupported ASL state type: {state_type}")

        return state
