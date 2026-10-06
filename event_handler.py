"""sisp 事件处理器。

SISPStateHandler   - 订阅 on_message_received，评估用户消息并推进状态机。
SISPPromptInjector - 订阅 on_prompt_build，向 default_chatter user prompt
                     的 extra 字段注入对应状态标签。

注意：system_reminder 为全局存储（无法按 session 隔离），而 default_chatter
system_prompt 的 on_prompt_build 事件不携带 stream_id。因此 SISPPromptInjector
转而处理 default_chatter_user_prompt，通过 extra 字段实现会话级别的动态注入。
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api import llm_api, stream_api
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event import EventDecision
from src.kernel.llm import LLMPayload, ROLE, Text

from .state import (
    SISPBodyState,
    SISPState,
    SISPStageDurations,
    advance_body_phase,
    load_body_state,
    load_state,
    save_body_state,
    save_state,
)

if TYPE_CHECKING:
    from .config import SISPConfig

logger = get_logger("sisp")

# SISPPromptInjector 处理用户提示词（包含 stream_id，可按会话隔离）
_TARGET_PROMPT = "default_chatter_user_prompt"
# KFC 系统提示词构建时也触发注入
_TARGET_PROMPT_KFC = "kfc_system_prompt"

# ── 评分提示词 ─────────────────────────────────────────────────────────────────

_SCORE_SYSTEM = (
"""
# Role
你是一个冷静、客观的交互氛围评估专家。你的任务是根据上下文，评估最新一条互动的“生理性/性暗示刺激强度”。

# Critical Rules (必读)
1. **拒绝过度解读**：严禁将日常通用词汇（如“去睡觉”、“洗澡”、“累了”、“脱衣服/换衣服”）直接视为暗示。
2. **字面意思优先**：除非上下文中存在明确的调情、肢体触碰或挑逗性语境，否则一律按字面意思评估。
3. **暗示的判定**：只有当言语伴随明显的调情意图、双关语或越界行为时，才计入 1-4 分。

# Scoring Scale (0-10)
- 0: 【纯净/日常】完全无关。例如：“晚安去睡了”、“我刚洗完澡”、“该吃饭了”。
- 1-2: 【轻微暧昧】带有好感的关心或礼貌性肢体接触。例如：摸头、握手、普通的拥抱。
- 3-4: 【隐晦暗示/调情】言语挑逗或带有性意味的试探。例如：“你穿这件真让人分心”、“想看你洗澡”。
- 5-6: 【明确挑逗/边缘触碰】触碰敏感部位或湿吻，有明显的生理唤起描写。
- 7-8: 【直接性行为】具体的性器官接触或直接的性交描写。
- 9-10: 【高强度刺激】多感官、大尺度的深度性交互描写。

# Examples for Calibration (对标示例)
- "我们去睡觉吧 (客观描述疲劳)" -> 0
- "床很大，我们要不要一起睡？(带调情语境)" -> 3
- "我想你了" -> 1
- "我想吃掉你" -> 4

# Output
仅输出 0 到 10 的整数，不输出任何其他文字。""")


async def _score_message(stream_id: str, latest_text: str, model_name: str) -> int:
    """以近 6 条对话上下文为输入，对最新互动按性刺激程度评分（0–10）。

    Args:
        stream_id: 当前聊天流 ID，用于拉取历史消息（近 5 条 + 最新 = 共 6 条）
        latest_text: 最新一条用户消息文本
        model_name: 配置中指定的模型名称（空字符串时使用默认任务模型）

    Returns:
        0–10 的整数评分；调用失败时返回 3（保守回退）
    """
    try:
        history = await stream_api.get_stream_messages(stream_id, limit=5)

        context_lines: list[str] = []
        for msg in history:
            role_label = "助手" if getattr(msg, "sender_role", "") == "bot" else "用户"
            text = str(getattr(msg, "processed_plain_text", None) or getattr(msg, "content", "") or "").strip()
            if text:
                context_lines.append(f"[{role_label}] {text[:300]}")

        context_lines.append(f"[用户（最新，请重点评估此条）] {latest_text[:400]}")
        context_text = "\n".join(context_lines)

        if model_name:
            model_set = llm_api.get_model_set_by_name(
                model_name, temperature=0.3, max_tokens=30
            )
        else:
            model_set = llm_api.get_model_set_by_task("actor")

        # 评分只需纯文本数字输出，强制关闭 tool_call_compat，
        # 避免框架把 "7" 当作 JSON 工具调用格式解析而抛出异常。
        model_set = [{**entry, "tool_call_compat": False} for entry in model_set]

        request = llm_api.create_llm_request(model_set, "sisp_score")
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_SCORE_SYSTEM)))
        request.add_payload(LLMPayload(ROLE.USER, Text(context_text)))

        response = await request.send(stream=False)
        raw = (await response or "").strip()

        match = re.search(r"\d+", raw)
        if match:
            return max(0, min(10, int(match.group())))
    except Exception as exc:
        logger.warning(f"sisp: 评分调用失败，回退为 3。原因: {exc}")

    return 3


# ── 作用范围检查 ──────────────────────────────────────────────────────────────


async def _is_in_scope(stream_id: str, config: "SISPConfig") -> bool:
    """判断当前会话是否在 sisp 的配置作用范围内。

    - whitelist: 填写的对象生效；两个列表都空时不生效
    - blacklist: 填写的对象屏蔽；两个列表都空时全部生效
    """
    scope_mode = str(config.scope.mode or "whitelist").strip().lower()
    group_targets = config.scope.group_targets
    user_targets = config.scope.user_targets

    if scope_mode not in {"whitelist", "blacklist"}:
        scope_mode = "whitelist"

    info = await stream_api.get_stream_info(stream_id)
    if info is None:
        return False

    chat_type = info.get("chat_type", "")

    if scope_mode == "whitelist" and not group_targets and not user_targets:
        return False

    if chat_type == "group" and group_targets:
        group_id = str(info.get("group_id") or "")
        return group_id in group_targets if scope_mode == "whitelist" else group_id not in group_targets

    if chat_type == "group":
        return scope_mode == "blacklist"

    if chat_type == "private" and user_targets:
        from src.core.models.stream import ChatStream

        platform = str(info.get("platform", ""))
        for uid in user_targets:
            try:
                expected = ChatStream.generate_stream_id(platform=platform, user_id=uid)
                if expected == stream_id:
                    return scope_mode == "whitelist"
            except ValueError:
                continue
        return scope_mode == "blacklist"

    if chat_type == "private":
        return scope_mode == "blacklist"

    return scope_mode == "blacklist"


def _is_fatigue_phase(body_state: SISPBodyState) -> bool:
    """判断当前身体状态是否属于疲劳期。"""
    return body_state.body_phase in {"extreme_fatigue", "general_fatigue"}


def _entry_threshold(body_state: SISPBodyState) -> int:
    """根据身体状态决定进入性爱模式所需的连续高分次数。"""
    if body_state.body_phase == "high_desire":
        return 1
    return 2


def _body_phase_prompt(
    config: "SISPConfig",
    body_state: SISPBodyState,
    *,
    in_sex_mode: bool,
) -> str | None:
    """根据身体状态获取对应的提示词。"""
    prompts = config.prompts

    if body_state.body_phase == "extreme_fatigue":
        return prompts.extreme_fatigue_passive if in_sex_mode else prompts.extreme_fatigue_idle

    if body_state.body_phase == "general_fatigue":
        return prompts.general_fatigue_passive if in_sex_mode else prompts.general_fatigue_idle

    if body_state.body_phase == "mild_desire":
        return prompts.mild_desire

    if body_state.body_phase == "high_desire":
        return prompts.high_desire

    return None


def _get_stage_durations(config: "SISPConfig") -> SISPStageDurations:
    """从插件配置中读取身体阶段持续时长。"""
    timing = config.timing
    return SISPStageDurations(
        extreme_fatigue_hours=max(0.0, timing.extreme_fatigue_hours),
        general_fatigue_hours=max(0.0, timing.general_fatigue_hours),
        none_cooldown_hours=max(0.0, timing.none_cooldown_hours),
        mild_desire_hours=max(0.0, timing.mild_desire_hours),
    )


# ── Handler 1：状态推进 ───────────────────────────────────────────────────────


class SISPStateHandler(BaseEventHandler):
    """消息接收时推进 SISP 状态机。

    对每条进入作用范围的消息执行性刺激程度评分（近 6 条上下文），
    按照完整生命周期逻辑更新状态，并将待注入事件写入 current_event 字段。

    注意：评分涉及 LLM 调用，耗时可能超过 EventBus 的 5 秒超时限制。
    因此 execute() 仅做快速过滤，将实际评分与状态推进逻辑派发到后台任务，
    避免阻塞事件总线导致 WARNING 超时跳过。
    """

    handler_name: str = "sisp_state_handler"
    handler_description: str = "SISP 状态机推进器，评分并更新会话状态"
    weight: int = 20
    intercept_message: bool = False
    init_subscribe: list[str] = [EventType.ON_MESSAGE_RECEIVED]

    def _get_config(self) -> "SISPConfig":
        """获取插件配置实例。"""
        from .config import SISPConfig
        return self.plugin.config if isinstance(self.plugin.config, SISPConfig) else SISPConfig()

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理消息接收事件，派发后台任务推进状态机。

        为避免 LLM 评分调用阻塞 EventBus（5 秒超时），
        此处仅做轻量过滤后将实际工作派发到后台任务。
        """
        config = self._get_config()
        if not config.plugin.enabled:
            return EventDecision.SUCCESS, params

        message = params.get("message")
        if message is None:
            return EventDecision.SUCCESS, params

        stream_id: str = getattr(message, "stream_id", "") or ""
        if not stream_id:
            return EventDecision.SUCCESS, params

        if not await _is_in_scope(stream_id, config):
            return EventDecision.SUCCESS, params

        # 仅处理非 bot 消息（不对自己发出的消息评分）
        if getattr(message, "sender_role", "") == "bot":
            return EventDecision.SUCCESS, params

        text = str(
            getattr(message, "processed_plain_text", None)
            or getattr(message, "content", "")
            or ""
        ).strip()
        if not text:
            return EventDecision.SUCCESS, params

        # 派发到后台任务，避免阻塞 EventBus 超时
        from src.kernel.concurrency import get_task_manager

        get_task_manager().create_task(
            self._process_score_and_advance(stream_id, text, config)
        )
        return EventDecision.SUCCESS, params

    async def _process_score_and_advance(
        self,
        stream_id: str,
        text: str,
        config: "SISPConfig",
    ) -> None:
        """后台执行评分与状态推进逻辑。"""
        try:
            await self._do_score_and_advance(stream_id, text, config)
        except Exception as exc:
            logger.error(f"[sisp] 后台评分任务异常: stream={stream_id[:8]} {exc}", exc_info=True)

    async def _do_score_and_advance(
        self,
        stream_id: str,
        text: str,
        config: "SISPConfig",
    ) -> None:
        """实际的评分与状态推进逻辑（从原 execute 中提取）。"""
        state = await load_state(stream_id)
        loaded_body_state = await load_body_state(stream_id)
        stage_durations = _get_stage_durations(config)
        body_state = advance_body_phase(loaded_body_state, durations=stage_durations)
        model_name = config.plugin.score_model

        if body_state.to_dict() != loaded_body_state.to_dict():
            await save_body_state(stream_id, body_state)

        score = await _score_message(stream_id, text, model_name)

        logger.info(
            f"[sisp] stream={stream_id[:8]} 评分={score} "
            f"in_sex={state.in_sex_mode} body={body_state.body_phase} "
            f"n={state.n} climax_count={state.climax_count} "
            f"low_score_count={state.low_score_count} x={state.x:.1f} "
            f"U={state.U:.1f} k={state.k:.2f}"
        )

        # ── 阶段一：未进入性爱模式 ──────────────────────────────────────────
        if not state.in_sex_mode:
            if score >= 7:
                state.foreplay_count += 1
                if state.foreplay_count >= _entry_threshold(body_state):
                    state = SISPState.new_sex_session()
                    if body_state.body_phase in {"mild_desire", "high_desire"}:
                        body_state = SISPBodyState()
                        await save_body_state(stream_id, body_state)
                    logger.info(f"[sisp] stream={stream_id[:8]} 进入性爱模式")
            else:
                state.foreplay_count = 0

            await save_state(stream_id, state)
            return

        # ── 阶段二：性爱模式中 ──────────────────────────────────────────────

        passive_mode = _is_fatigue_phase(body_state)

        # 优先判定：低意愿检测
        if score < 5:
            state.low_score_count += 1
            logger.info(
                f"[sisp] stream={stream_id[:8]} 低意愿累计 low_score_count={state.low_score_count} "
                f"body_before={body_state.body_phase} passive={passive_mode} "
                f"n={state.n} climax_count={state.climax_count}"
            )
            if state.low_score_count >= 2:
                if not passive_mode:
                    if (
                        state.climax_count == 0
                        and body_state.body_phase in {"none", "mild_desire", "high_desire"}
                    ):
                        exit_reason = "no_climax_early_exit"
                        body_state = SISPBodyState(
                            body_phase="high_desire",
                            phase_until=0.0,
                        )
                    elif state.n >= 5:
                        exit_reason = "broken_exit"
                        body_state = SISPBodyState(
                            body_phase="extreme_fatigue",
                            phase_until=time.time() + stage_durations.extreme_fatigue_seconds,
                        )
                    else:
                        exit_reason = "normal_exit"
                        body_state = SISPBodyState(
                            body_phase="general_fatigue",
                            phase_until=time.time() + stage_durations.general_fatigue_seconds,
                        )
                    logger.info(
                        f"[sisp] stream={stream_id[:8]} 退出判定 reason={exit_reason} "
                        f"body_after={body_state.body_phase} n={state.n} "
                        f"climax_count={state.climax_count}"
                    )
                    await save_body_state(stream_id, body_state)
                else:
                    logger.info(
                        f"[sisp] stream={stream_id[:8]} 退出判定 reason=passive_exit "
                        f"body_after={body_state.body_phase} n={state.n} "
                        f"climax_count={state.climax_count}"
                    )

                state = SISPState()  # 完全重置，x/n/k/U/beg_announced 全部归零
                logger.info(
                    f"[sisp] stream={stream_id[:8]} 用户低意愿，退出性爱模式，"
                    f"状态已重置 body={body_state.body_phase}"
                )
                await save_state(stream_id, state)
                return

            await save_state(stream_id, state)
            return
        else:
            state.low_score_count = 0

        if passive_mode:
            logger.info(f"[sisp] stream={stream_id[:8]} 疲劳期被动配合，跳过 n/x/高潮计算")
            await save_state(stream_id, state)
            return

        # 状态分流：基于 n
        # 享受期 n=1/2/3（3次高潮机会）→ 求饶期 n=4 → 玩坏期 n>4
        if state.n > 4:
            # C. 玩坏期：broken 常驻注入，但高潮事件仍可循环触发（n 不再递增）
            state.x += score
            logger.info(f"[sisp] stream={stream_id[:8]} 玩坏期积累 x={state.x:.1f} / U={state.U:.1f}")

            if state.x >= state.U:
                y = state.k * state.x
                if y <= 16.25:
                    state.current_event = "mild_climax"
                elif y <= 71.26:
                    state.current_event = "normal_climax"
                else:
                    state.current_event = "intense_climax"

                if config.plugin.debug_log:
                    logger.info(
                        f"[sisp] stream={stream_id[:8]} 玩坏期高潮 "
                        f"y={y:.2f} event={state.current_event}"
                    )

                state.climax_count += 1
                state.reset_after_climax()  # 重置 x/k/U，n 不变

        elif state.n == 4:
            # B. 求饶期：先宣告 beg，下一条消息再升入玩坏期。
            # 不能在同一轮既把 n 升为 5（触发 broken 注入）又设置 current_event="beg"，
            # 否则注入器会同帧注入互相矛盾的 broken + beg 提示词。
            if not state.beg_announced:
                # 首次进入求饶期：只公告事件，n 保持 4
                state.current_event = "beg"
                state.beg_announced = True
            elif score > 0:
                # beg 已在上一轮宣告，本轮才升入玩坏期
                state.n += 1  # → 玩坏期（下轮生效）

        else:
            # A. 享受期（n <= 3）：正常积累，共 3 次高潮机会
            state.x += score
            logger.info(f"[sisp] stream={stream_id[:8]} 享受期积累 x={state.x:.1f} / U={state.U:.1f}")

            if state.x >= state.U:
                y = state.k * state.x
                if y <= 16.25:
                    state.current_event = "mild_climax"
                elif y <= 71.26:
                    state.current_event = "normal_climax"
                else:
                    state.current_event = "intense_climax"

                if config.plugin.debug_log:
                    logger.info(
                        f"[sisp] stream={stream_id[:8]} 高潮触发 "
                        f"y={y:.2f} event={state.current_event}"
                    )

                state.climax_count += 1
                state.n += 1
                state.reset_after_climax()

        await save_state(stream_id, state)


# ── Handler 2：提示词注入 ─────────────────────────────────────────────────────


class SISPPromptInjector(BaseEventHandler):
    """在 default_chatter user prompt 构建前注入 SISP 状态标签。

    注入规则：
    - in_sex_mode=True 时，优先注入 atmosphere。
    - 疲劳期进入性交时，只注入 fatigue passive 提示词，不再注入 broken 与一次性事件。
    - 正常性交时，仍按 n / current_event 注入 broken 与高潮/beg 事件。
    - in_sex_mode=False 时，仅根据身体状态注入 fatigue/desire 提示词。

    通过 default_chatter_user_prompt 模板的 on_prompt_build 事件注入：
    values["extra"] 字段（stream_id 在该事件中可用，能实现会话级别隔离）。
    """

    handler_name: str = "sisp_prompt_injector"
    handler_description: str = "SISP 提示词注入器，按当前状态向 user prompt extra 追加标签"
    weight: int = 10
    intercept_message: bool = False
    init_subscribe: list[str] = ["on_prompt_build"]

    def _get_config(self) -> "SISPConfig":
        """获取插件配置实例。"""
        from .config import SISPConfig
        return self.plugin.config if isinstance(self.plugin.config, SISPConfig) else SISPConfig()

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 on_prompt_build 事件，按状态向 extra 注入提示词标签。"""
        if params.get("name") not in (_TARGET_PROMPT, _TARGET_PROMPT_KFC):
            return EventDecision.SUCCESS, params

        config = self._get_config()
        if not config.plugin.enabled:
            return EventDecision.SUCCESS, params

        values = params.get("values", {})
        stream_id = str(values.get("stream_id", "") or "")
        if not stream_id:
            return EventDecision.SUCCESS, params

        if not await _is_in_scope(stream_id, config):
            return EventDecision.SUCCESS, params

        state = await load_state(stream_id)
        loaded_body_state = await load_body_state(stream_id)
        body_state = advance_body_phase(
            loaded_body_state,
            durations=_get_stage_durations(config),
        )

        if body_state.to_dict() != loaded_body_state.to_dict():
            await save_body_state(stream_id, body_state)

        p = config.prompts
        inject_parts: list[str] = []
        event = None

        if state.in_sex_mode:
            inject_parts.append(p.atmosphere)

            body_prompt = _body_phase_prompt(config, body_state, in_sex_mode=True)
            if body_prompt:
                inject_parts.append(body_prompt)

            passive_mode = _is_fatigue_phase(body_state)
            if not passive_mode:
                if state.n > 4:
                    inject_parts.append(p.broken)

                event = state.current_event
                event_map: dict[str, str] = {
                    "mild_climax": p.mild_climax,
                    "normal_climax": p.normal_climax,
                    "intense_climax": p.intense_climax,
                    "beg": p.beg,
                }
                if event and event in event_map:
                    inject_parts.append(event_map[event])
                    state.current_event = None
                    await save_state(stream_id, state)
        else:
            body_prompt = _body_phase_prompt(config, body_state, in_sex_mode=False)
            if not body_prompt:
                return EventDecision.SUCCESS, params
            inject_parts.append(body_prompt)

        injected = "\n".join(inject_parts)
        existing = str(values.get("extra", "") or "")
        values["extra"] = (existing + "\n" + injected) if existing else injected

        if config.plugin.debug_log:
            logger.info(
                f"[sisp] stream={stream_id[:8]} n={state.n} 注入事件={event!r}"
            )

        return EventDecision.SUCCESS, params
