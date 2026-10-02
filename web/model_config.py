"""版本化模型配置与无网络的执行能力校验。"""

import os
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ModelID = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\s\x00-\x1f]+$")]
ConnectionID = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmptyParameters(StrictModel):
    pass


class TextParameters(StrictModel):
    max_output_tokens: int | None = Field(default=None, ge=1, le=131072)
    temperature: float | None = Field(default=None, ge=0, le=2)


class SpeechParameters(StrictModel):
    speech_rate: float = Field(default=1.0, gt=0, le=2)


class Stage(StrictModel):
    mode: Literal["api", "local"]
    provider: Literal["bailian", "openai_compatible", "qwen3", "gui_owl", "musetalk"]
    connection_id: ConnectionID | None = None
    model: ModelID
    parameters: EmptyParameters = Field(default_factory=EmptyParameters)


class TextStage(Stage):
    provider: Literal['bailian', 'openai_compatible']
    parameters: TextParameters = Field(default_factory=TextParameters)


class SpeechStage(Stage):
    provider: Literal['bailian', 'qwen3']
    parameters: SpeechParameters = Field(default_factory=SpeechParameters)


class ASRStage(Stage):
    provider: Literal['bailian', 'qwen3']


class GroundingStage(Stage):
    provider: Literal['bailian', 'gui_owl']


class Stages(StrictModel):
    text: TextStage
    vision: TextStage
    tts: SpeechStage
    asr: ASRStage
    grounding: GroundingStage


class Timing(StrictModel):
    source: Literal["tts_timestamp", "local_forced_aligner"]


class LocalVoice(StrictModel):
    kind: Literal['default', 'upload'] = 'default'
    path: str | None = Field(default=None, max_length=2000)
    ref_text: str | None = Field(default=None, max_length=20000)


class Avatar(Stage):
    provider: Literal['bailian', 'musetalk']
    enabled: bool


class PipelineModelConfig(StrictModel):
    schema_version: Literal[2] = 2
    preset: Literal["bailian_api", "local_existing", "custom"] = "custom"
    stages: Stages
    timing: Timing
    avatar: Avatar

    @model_validator(mode="after")
    def check_combinations(self):
        providers = {"text": "openai_compatible", "vision": "openai_compatible",
                     "tts": "qwen3", "asr": "qwen3", "grounding": "gui_owl", "avatar": "musetalk"}
        for role, stage in self.roles().items():
            field = 'avatar' if role == 'avatar' else f'stages.{role}'
            allowed = {"bailian", "openai_compatible"} if role in ("text", "vision") else {"bailian", providers[role]}
            if stage.provider not in allowed:
                raise ConfigurationError(field + '.provider', '该环节不支持此提供方')
            if stage.provider == "bailian" and stage.mode != "api":
                raise ConfigurationError(field + '.mode', '百炼必须使用API')
            needs_connection = stage.mode == "api" or stage.provider == "openai_compatible"
            if needs_connection and not stage.connection_id:
                raise ConfigurationError(field + '.connection_id', '此环节需要连接引用')
            if not needs_connection and stage.connection_id is not None:
                raise ConfigurationError(field + '.connection_id', '本地权重不使用API连接')
            if role not in ("text", "vision") and stage.mode == "api" and stage.provider != "bailian":
                raise ConfigurationError(field + '.provider', '仅百炼提供此API路径')
        if self.timing.source == "tts_timestamp" and self.stages.tts.mode != "api":
            raise ConfigurationError('timing.source', '本地TTS不能提供API合成时间戳')
        return self

    def roles(self):
        return {**{role: getattr(self.stages, role) for role in Stages.model_fields}, "avatar": self.avatar}

    def public_dict(self):
        return self.model_dump(exclude_none=True)


class ConfigurationError(ValueError):
    def __init__(self, field, message, code="invalid_model_configuration", status=422):
        super().__init__(message)
        self.field, self.code, self.status = field, code, status

    def detail(self):
        return {"code": self.code, "problems": [{"field": self.field, "message": str(self)}]}


LOCAL_MODELS = {"tts": "qwen3tts", "asr": "qwen3-asr", "grounding": "gui-owl", "avatar": "musetalk"}


def legacy_config(config, connection_id="legacy"):
    stages = {}
    for role, field in (("text", "text_model"), ("vision", "vision_model")):
        stages[role] = {"mode": "api", "provider": "openai_compatible", "connection_id": connection_id,
                        "model": config.get(field) or os.environ.get("PAPER2VIDEO_LLM_MODEL" if role == "text" else "PAPER2VIDEO_VLM_MODEL", "deepseek-flash")}
    for role in ("tts", "asr", "grounding"):
        stages[role] = {"mode": "local", "provider": "gui_owl" if role == "grounding" else "qwen3", "model": LOCAL_MODELS[role]}
    return PipelineModelConfig.model_validate({
        "schema_version": 2, "preset": "custom", "stages": stages,
        "timing": {"source": "local_forced_aligner"},
        "avatar": {"enabled": bool(config.get("generate_talking", False)), "mode": "local", "provider": "musetalk", "model": "musetalk"}})


def derived_policy(config):
    return {"avatar_enabled": config.avatar.enabled, "max_segment_seconds_exclusive": 20 if config.avatar.enabled else None,
            "target_segment_seconds": 18 if config.avatar.enabled else None, "policy_version": 1,
            "implemented": False}


def required_roles(config, pipeline_config):
    stages = pipeline_config.get("stages", ["1", "2", "3"])
    if not isinstance(stages, list) or not stages or any(stage not in ("0", "1", "2", "3") for stage in stages):
        raise ConfigurationError("config.stages", "运行阶段必须为0/1/2/3的非空列表")
    selected = {"1", "2", "3"} if "0" in stages else set(stages)
    roles = set()
    if "1" in selected:
        roles.update(("text", "vision"))
    if "2" in selected:
        if not (pipeline_config.get("reuse_script") and pipeline_config.get("require_reuse_script")):
            roles.add("vision")
        if not pipeline_config.get("review_before_render"):
            roles.update(("tts", "grounding"))
            if pipeline_config.get("ref_audio") and not pipeline_config.get("ref_text"):
                roles.add("asr")
    if "3" in selected and config.avatar.enabled:
        roles.add("avatar")
    return sorted(roles)


def ensure_implemented(config, roles):
    for role in roles:
        stage = config.roles()[role]
        field = "config.model_config.avatar" if role == "avatar" else f"config.model_config.stages.{role}"
        if role not in ("text", "vision") and (stage.mode != "local" or stage.model != LOCAL_MODELS[role]):
            raise ConfigurationError(field, "该模型适配器尚未实现，不允许回退本地", "adapter_not_implemented")
        if role == "tts" and stage.parameters.speech_rate != 1.0:
            raise ConfigurationError(field + ".parameters.speech_rate", "当前本地TTS仅支持默认语速", "adapter_not_implemented")
        if role == "avatar":
            raise ConfigurationError(field, "数字人严格分段将在P6实现；旧任务路线保持兼容", "adapter_not_implemented")
    if "tts" in roles and config.timing.source != "local_forced_aligner":
        raise ConfigurationError("config.model_config.timing.source", "API合成时间戳将在P4/P5接入", "adapter_not_implemented")


def capabilities():
    return {"schema_version": 2, "schema": PipelineModelConfig.model_json_schema(),
            "implemented": {"compatible_text_vision": True, "local_tts_asr_grounding": True,
                            "independent_text_vision_connections": True, "role_model_parameters": True,
                            "cloud_voices": False, "tts_timestamps": False, "bailian_speech": False,
                            "bailian_grounding": False, "bailian_avatar": False, "avatar_segmentation": False}}


def effective_mode(config, roles):
    selected = [config.roles()[role] for role in roles]
    if not selected:
        return 'no_model_inference'
    if all(stage.mode == 'local' for stage in selected):
        return 'local'
    if all(stage.mode == 'api' for stage in selected):
        return 'bailian_api' if all(stage.provider == 'bailian' for stage in selected) else 'api'
    return 'mixed'


def reject_embedded_secrets(value, field="config"):
    if isinstance(value, dict):
        for name, item in value.items():
            path = f"{field}.{name}"
            if str(name).lower() in ("api_key", "apikey", "authorization", "access_token", "secret"):
                raise ConfigurationError(path, "凭据不能写入任务配置，请使用连接引用")
            reject_embedded_secrets(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_embedded_secrets(item, f"{field}.{index}")
