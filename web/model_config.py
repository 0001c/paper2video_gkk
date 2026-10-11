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
    qa: TextStage | None = None


class EvaluationPolicy(StrictModel):
    enabled: bool = False
    script_threshold: int = Field(default=80, ge=0, le=100)
    minimum_threshold: int = Field(default=60, ge=0, le=100)
    standard_threshold: int = Field(default=80, ge=0, le=100)
    question_count: int = Field(default=20, ge=4, le=40)
    max_auto_retries: int = Field(default=2, ge=0, le=3)

    @model_validator(mode='after')
    def ordered_thresholds(self):
        if self.minimum_threshold > self.standard_threshold:
            raise ConfigurationError('evaluation.minimum_threshold', '最低通过线不能高于标准通过线')
        return self


class Timing(StrictModel):
    source: Literal["tts_timestamp", "local_forced_aligner"]


class LocalVoice(StrictModel):
    kind: Literal['default', 'upload'] = 'default'
    path: str | None = Field(default=None, max_length=2000)
    ref_text: str | None = Field(default=None, max_length=20000)


class CloudVoiceReference(StrictModel):
    kind: Literal['cloud']
    resource_id: ConnectionID


class AvatarParameters(StrictModel):
    resolution: Literal["480P", "720P"] = "480P"


class Avatar(Stage):
    parameters: EmptyParameters | AvatarParameters = Field(default_factory=EmptyParameters)
    provider: Literal['bailian', 'musetalk']
    enabled: bool = False


class PipelineModelConfig(StrictModel):
    schema_version: Literal[2] = 2
    preset: Literal["bailian_api", "local_existing", "custom"] = "custom"
    stages: Stages
    timing: Timing
    avatar: Avatar
    evaluation: EvaluationPolicy = Field(default_factory=EvaluationPolicy)

    @model_validator(mode="after")
    def check_combinations(self):
        providers = {"text": "openai_compatible", "vision": "openai_compatible", "qa": "openai_compatible",
                     "tts": "qwen3", "asr": "qwen3", "grounding": "gui_owl", "avatar": "musetalk"}
        for role, stage in self.roles().items():
            field = 'avatar' if role == 'avatar' else f'stages.{role}'
            allowed = {"bailian", "openai_compatible"} if role in ("text", "vision", "qa") else {"bailian", providers[role]}
            if stage.provider not in allowed:
                raise ConfigurationError(field + '.provider', '该环节不支持此提供方')
            if stage.provider == "bailian" and stage.mode != "api":
                raise ConfigurationError(field + '.mode', '百炼必须使用API')
            needs_connection = stage.mode == "api" or stage.provider == "openai_compatible"
            if needs_connection and not stage.connection_id:
                raise ConfigurationError(field + '.connection_id', '此环节需要连接引用')
            if not needs_connection and stage.connection_id is not None:
                raise ConfigurationError(field + '.connection_id', '本地权重不使用API连接')
            if role not in ("text", "vision", "qa") and stage.mode == "api" and stage.provider != "bailian":
                raise ConfigurationError(field + '.provider', '仅百炼提供此API路径')
        if self.avatar.mode == "local" and isinstance(self.avatar.parameters, AvatarParameters):
            raise ConfigurationError("avatar.parameters.resolution", "分辨率仅适用于百炼Wan数字人")
        if self.avatar.mode == "api" and isinstance(self.avatar.parameters, EmptyParameters):
            self.avatar.parameters = AvatarParameters()
        if self.timing.source == "tts_timestamp" and self.stages.tts.mode != "api":
            raise ConfigurationError('timing.source', '本地TTS不能提供API合成时间戳')
        if self.evaluation.enabled and self.stages.qa is None:
            raise ConfigurationError('stages.qa', '开启问答评价后必须选择评价模型')
        if self.evaluation.enabled and (self.stages.asr.mode != 'local' or self.stages.asr.model != 'qwen3-asr'):
            raise ConfigurationError('stages.asr', '成片问答当前使用本地Qwen3-ASR识别实际音轨，请选择本地识别模型')
        return self

    def roles(self):
        return {**{role: getattr(self.stages, role) for role in Stages.model_fields if getattr(self.stages, role) is not None}, "avatar": self.avatar}

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
            "target_segment_seconds": 18 if config.avatar.enabled else None, "policy_version": 2,
            "implemented": True}


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
            if config.stages.tts.mode == 'local' and pipeline_config.get("ref_audio") and not pipeline_config.get("ref_text"):
                roles.add("asr")
    if "3" in selected and config.avatar.enabled:
        roles.add("avatar")
    if config.evaluation.enabled:
        roles.add('qa')
        if not pipeline_config.get('review_before_render'):
            roles.add('asr')
    return sorted(roles)


def ensure_implemented(config, roles):
    for role in roles:
        stage = config.roles()[role]
        field = "config.model_config.avatar" if role == "avatar" else f"config.model_config.stages.{role}"
        cloud_tts = role == 'tts' and stage.mode == 'api' and stage.model in (
            'cosyvoice-v3.5-plus', 'cosyvoice-v3.5-flash', 'cosyvoice-v3-plus', 'cosyvoice-v3-flash', 'cosyvoice-v2')
        cloud_avatar = role == 'avatar' and stage.mode == 'api' and stage.model == 'wan2.2-s2v'
        cloud_grounding = role == 'grounding' and stage.mode == 'api' and stage.model == 'qwen3-vl-plus'
        if role not in ("text", "vision", "qa") and not cloud_tts and not cloud_grounding and not cloud_avatar and (stage.mode != "local" or stage.model != LOCAL_MODELS[role]):
            raise ConfigurationError(field, "该模型适配器尚未实现，不允许回退本地", "adapter_not_implemented")
        if role == "tts" and stage.mode == 'local' and stage.parameters.speech_rate != 1.0:
            raise ConfigurationError(field + ".parameters.speech_rate", "当前本地TTS仅支持默认语速", "adapter_not_implemented")
        if cloud_tts and stage.parameters.speech_rate < 0.5:
            raise ConfigurationError(field + '.parameters.speech_rate', '百炼语速范围为0.5至2')
    if 'tts' in roles and config.stages.tts.mode == 'api' and config.timing.source != 'tts_timestamp':
        raise ConfigurationError('config.model_config.timing.source', 'P4云配音必须使用合成时间戳，不隐式启用本地对齐')


def capabilities():
    return {"schema_version": 2, "schema": PipelineModelConfig.model_json_schema(),
            "implemented": {"compatible_text_vision": True, "local_tts_asr_grounding": True,
                            "independent_text_vision_connections": True, "role_model_parameters": True,
                            "cloud_voices": True, "tts_timestamps": True, "bailian_speech": True,
                            "bailian_grounding": True, "bailian_avatar": True, "avatar_segmentation": True, "qa_evaluation": True}}


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
