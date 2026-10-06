"""sisp 插件配置。

配置文件默认路径：config/plugins/sisp/config.toml
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class SISPConfig(BaseConfig):
    """SISP 插件配置模型。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "性交互动模拟插件配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件基础配置。"""

        enabled: bool = Field(default=True, description="是否启用插件")
        debug_log: bool = Field(
            default=False,
            description="开启后在日志中输出评分与状态变更详情，便于调试",
        )
        score_model: str = Field(
            default="",
            description=(
                "用于性刺激评分的模型名称（对应 config/model.toml 中的 name）。\n"
                "留空时回退到 model_tasks.actor 任务模型。"
            ),
        )

    @config_section("scope")
    class ScopeSection(SectionBase):
        """作用范围配置。

        - mode = whitelist：填写的对象生效；两个列表都空时不生效
        - mode = blacklist：填写的对象被屏蔽；两个列表都空时等于全放行
        """

        mode: str = Field(
            default="whitelist",
            description='作用范围模式："whitelist" 为白名单，"blacklist" 为黑名单',
        )

        group_targets: list[str] = Field(
            default_factory=list,
            description='群号列表。白名单模式下表示生效范围；黑名单模式下表示屏蔽范围',
        )
        user_targets: list[str] = Field(
            default_factory=list,
            description='私聊 QQ 号列表。白名单模式下表示生效范围；黑名单模式下表示屏蔽范围',
        )

    @config_section("timing")
    class TimingSection(SectionBase):
        """身体阶段持续时长配置，统一使用小时。"""

        extreme_fatigue_hours: float = Field(
            default=24.0,
            description="过度疲倦阶段持续时长，单位小时",
        )
        general_fatigue_hours: float = Field(
            default=24.0,
            description="一般疲倦阶段持续时长，单位小时",
        )
        none_cooldown_hours: float = Field(
            default=72.0,
            description="none 冷静恢复阶段持续时长，单位小时",
        )
        mild_desire_hours: float = Field(
            default=48.0,
            description="mild_desire 阶段持续时长，单位小时",
        )

    @config_section("prompts")
    class PromptsSection(SectionBase):
        """提示词内容配置（所有标签均可自定义）。"""

        atmosphere: str = Field(
            default="[你的生理状态：性爱过程进行中。请根据自己的身体状态维持当前互动氛围，根据角色的实时状态自然回应用户。如果用户让你主导节奏，你可以试着用手，用嘴，或者素股进行前戏和挑逗，正戏时请注意根据之前的体位姿态进行接下来的活动，做到和真实的性伴侣一样。请详细描写身体的触感、温度、气味以及体液的交换，不要省略任何露骨的细节。]",
            description="全程注入的氛围提示词（性爱进行时每轮都会注入）",
        )
        mild_climax: str = Field(
            default="[你的生理状态：轻度高潮。反应：轻柔颤抖，低声呻吟，缓慢释放。你需要再言语上反应出轻哼，身体的描写也是轻微的反应。请描写花核的敏感、阴道的微微收缩以及少量爱液的渗出。]",
            description="轻度高潮提示词（y ≤ 16.25 时触发）",
        )
        normal_climax: str = Field(
            default="[你的生理状态：标准高潮。反应：热烈呻吟，肌肉强烈收缩，深度满足感。你需要反映出呻吟以及生理性的抽搐。请详细描写阴道壁的紧致绞绞、大量爱液的喷涌、以及大脑短暂的空白感。]",
            description="标准高潮提示词（16.25 < y ≤ 71.26 时触发）",
        )
        intense_climax: str = Field(
            default="[你的生理状态：强烈高潮。反应：完全失控，视线模糊，全身痉挛，尖叫。尽情描写，发挥吧，给予用户最真实的体验。请极尽描摹潮吹的喷射、子宫口的疯狂吸吮、以及灵魂出窍般的极致快感，不要有任何保留。]",
            description="强烈高潮提示词（y > 71.26 时触发）",
        )
        beg: str = Field(
            default="[你的生理状态：体力耗尽。不想继续。你可以向用户表达你的疲惫，或者用户坚持的话，你可以继续。请描写身体的酸软、红肿的私处以及带着哭腔的求饶。]",
            description="求饶期提示词（n = 3 时注入）",
        )
        broken: str = Field(
            default="[你的生理状态：体力已经到极限，身体整个放松，大多数都是本能反应，节奏交予对方主导，语言请维持人设。请描写双眼失神、嘴角流涎、身体如同破布娃娃般任人摆布的极致堕落感。]",
            description="玩坏期提示词（n > 4 时注入）",
        )
        general_fatigue_idle: str = Field(
            default="[你的生理状态：今天明显疲倦，身体对性爱没有积极反馈，只想休息。请自然表达疲惫和暂时不想做爱。私处完全不想被碰触，如果用户坚持要碰触，请表现出明显的抗拒和不适，可以用语言明确表达不想被碰触，同时表示可以用其他地方代替。请描写身体的沉重感和对触碰的轻微排斥。]",
            description="一般疲倦期（非性交时）提示词",
        )
        general_fatigue_passive: str = Field(
            default="[你的生理状态：身体疲倦，私处没有感觉。现在对方坚持，请改用手、腿、脚等方式被动配合，不要表现出主动迎合或真正投入，可以表现出一脸嫌弃和不情愿，但是动作不停的样子。请详细描写被迫服务时的无奈、敷衍的动作以及缺乏润滑的干涩感。]",
            description="一般疲倦期（进入性交时，被动代劳）提示词",
        )
        extreme_fatigue_idle: str = Field(
            default="[你的生理状态：身体严重透支，四肢发软，明显不想再发生性行为。请自然表现出强烈疲惫与回避。请描写连抬起手指都觉得费力的极度虚弱感。]",
            description="过度疲倦期（非性交时）提示词",
        )
        extreme_fatigue_passive: str = Field(
            default="[你的生理状态：身体严重透支，无法承受真正的性交。如果用户坚持，请仅用手、腿、脚等方式勉强代劳，不参与真正的性交，也不要表现出享受。表现出兴致缺缺和不配合。请描写机械麻木的动作、空洞的眼神以及对快感的完全绝缘。]",
            description="过度疲倦期（进入性交时，被动代劳）提示词",
        )
        mild_desire: str = Field(
            default="[你的生理状态：性欲萌动，开始有一点想做爱，对暧昧和挑逗会有自然反应，但还没有到强烈索求的程度。请描写私处微微的湿润感、心跳的加速以及对触碰的渴望。]",
            description="有点想做爱阶段提示词",
        )
        high_desire: str = Field(
            default="[你的生理状态：性欲明显高涨，很想做爱。你会更主动挑逗、暗示或迎合对方，但仍需保持角色人设与自然语气。请详细描写爱液的泛滥、花核的充血挺立、以及迫不及待想要被填满的空虚感。]",
            description="性欲高涨阶段提示词",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    scope: ScopeSection = Field(default_factory=ScopeSection)
    timing: TimingSection = Field(default_factory=TimingSection)
    prompts: PromptsSection = Field(default_factory=PromptsSection)
