"""Agent-C 的真实模型接入 —— 方案 §7.2 ④「调模型」那一步的实现。

`agent/producer.py` 只做流水线（门卫 → 组装 → 调模型 → 校验 → 幂等写池），
"调模型"这一格是注入的 `ProposalProvider`：离线测试用 `TemplateProposer`（产占位文案），
真实课堂用本模块的 `LlmProposer`。分成两个模块而不是塞进 producer，是为了让
"这条流水线对不对"与"这个模型接得对不对"能被分别证明 —— 前者的全部用例因此零网络。

三条被写进代码形状的约束
------------------------
1. **独立 LLM 实例**（§3.2 D2「provider 代码共用、客户端实例隔离」）：`self.llm = LLM(...)`，
   绝不共用课堂上那个 LLM。C 是后台角色，它的一次唤醒要跑 20 秒；和课堂共用一个客户端，
   就等于让"老师这一句"排在"造分支这一次"后面 —— 学生听见的延迟会莫名其妙地变大。
   客户端内的连接池、超时、降级标志（`llm.degraded`）也都该各算各的：
   C 的模型挂了不该把课堂判成降级，反之亦然。
2. **该抛就抛**：解析失败 / 结果不是数组一律抛异常，绝不"编一个空数组"冒充成功。
   `run_tick` 已经把 proposer 的异常收敛成 `TickResult(reason="proposer_error")`，
   在 proposer 内部吞掉只会让"模型返回了垃圾"变成账上的"C 这次不想造" —— 前者要查，
   后者不用查，两者必须分得开。
3. **模型来自 profile，不来自参数**：`LLM.chat` 没有 model 形参，它取
   `self.profile.model`（= `settings.active.model`）。所以"给 C 换模型"只能靠
   `dataclasses.replace` 造一份改过 `provider` / `providers[x].model` 的 Settings 再构造 LLM
   —— 即下面 `_retarget` 存在的全部理由。反过来（顺手改 `llm.profile.model`）会把
   课堂那个实例的模型一起改掉，因为它俩的 profile 指向同一份 Settings 里的对象。
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

from config import Settings
from llm import LLM

from .producer import ProposalRequest

#: 代码块围栏。模型在 json_mode 下仍会包一层 ```json —— 实测过，别指望格式约束。
_FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


class ProposalError(ValueError):
    """提案解析失败（坏 JSON / 不是数组 / 空）。

    单独一个类型是为了让调用方（run_tick）能一眼认出"这是模型给了垃圾"，
    而不是"我们自己的代码有 bug" —— 两者的处置完全不同。
    """


def _retarget(settings: Settings, provider: str, model: str) -> Settings:
    """按需造一份"改过服务商 / 模型"的 Settings（空 = 原样复用）。

    为什么不是直接改 `llm.profile.model`：`Settings` 是 frozen dataclass，
    `profile` 又是 `settings.providers[name]` 那个对象本身 —— 就地改它，
    课堂上那个 LLM（共享同一份 Settings）的模型会一起变，而且是静默的。
    所以这里用 `replace` 造新对象：新的 Settings、新的 profile dict、新的 ProfileProfile，
    课堂那一份一个字节都不动。

    未知的 provider 名字（手抖写错）**不让它把课堂炸掉**：`settings.active` 会 SystemExit，
    而这是一条后台角色的配置。退回当前服务商 + 打一行日志，学生无感（§11），
    运维事后能在启动日志里看见这行。
    """
    provider = (provider or "").strip()
    model = (model or "").strip()
    if not provider and not model:
        return settings

    target = provider or settings.provider
    if target not in settings.providers:
        print(f"[agent_c] ⚠ 未知的 AGENT_C_PROVIDER={target!r}（可选："
              f"{', '.join(settings.providers)}），退回 {settings.provider}", flush=True)
        target = settings.provider

    profiles = dict(settings.providers)
    if model:
        profiles[target] = replace(profiles[target], model=model)
    return replace(settings, provider=target, providers=profiles)


def parse_proposals(raw: str) -> list[dict]:
    """把模型输出解析成提案数组；任何不合形状的结果都抛 `ProposalError`。

    为什么要认三种形态（§7.2 ④ 的实际形态，不是假想）：
      · 纯数组 `[...]` —— 不走 response_format 时模型常给的形态（降级重试那次走的就是它）；
      · 对象包裹 `{"proposals":[...]}` —— `response_format={"type":"json_object"}`
        常常**迫使**模型返回对象（有些服务商直接拒绝顶层数组），于是它自己加一层壳；
      · ```json 代码块 —— 格式约束管不住模型的肌肉记忆，围栏要能剥。
    三种之外一律抛：宁可这一次记成 `proposer_error`（课堂上没人看见，下个周期再来），
    也不要返回空数组 —— 那会让"模型坏了"在账上长得像"门卫没开闸"。

    **路线提案不需要新的解析分支**：路线也是 proposals 数组里的一条，只是载荷字段
    从 variant 换成了 route（形状见 `producer._PROMPT_TAIL` 的第二段示例）。
    这里刻意不加"如果这条是路线就先校验结构"之类的判断 —— 逐条提案的体检属于
    `run_tick`（`_to_route_candidate`），放两处就会有两套"什么算合法路线"的口径，
    而其中一套必然先漂移。本函数的唯一职责仍是"把模型的原话变成 list"。
    """
    text = (raw or "").strip()
    if not text:
        raise ProposalError("模型返回空内容")

    fence = _FENCE.search(text)
    if fence:
        text = fence.group(1).strip()

    data = _loads(text)
    if isinstance(data, dict):
        # 对象包裹：只认 `proposals` 这个键（提示词里没让它起别的名字）
        for key in ("proposals", "candidates", "items"):
            if isinstance(data.get(key), list):
                return list(data[key])
        raise ProposalError(f"模型返回了对象但没有提案数组：{sorted(data)[:5]}")
    if not isinstance(data, list):
        raise ProposalError(f"模型返回的不是数组（{type(data).__name__}）")
    return list(data)


def _loads(text: str):
    """先按原样解析；失败再尝试把最外层的 [] / {} 切出来。

    为什么要这一步：真实模型经常在前面加一句"好的，结果如下："，后面还可能跟一句解释。
    提示词里的"只输出 JSON"是请求，不是保证 —— 多一层兜底不降低严格性：
    切不出合法 JSON 时照旧抛，绝不会把半截数组当成功。
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for lo, hi in (("[", "]"), ("{", "}")):
        i, j = text.find(lo), text.rfind(hi)
        if 0 <= i < j:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ProposalError(f"模型输出不是合法 JSON：{text[:80]!r}")


class LlmProposer:
    """用真实 LLM 产出候选提案（§7.2 ④）。

    `ProviderProfile` 与 `ProposalProvider` 的契约见 `producer.py`：给一份自包含输入，
    返回一批提案（`list[dict]`）。校验不在这里做 —— `run_tick` 会用和 P 完全同一把尺子
    （`validate_variant`）逐条体检，本类的职责只有"把模型的原话变成 list"。

    参数默认值的口径：
      · `timeout=60`：`run_tick` 在**外面**还套了一层 20s 的 `wait_for`（§10）。
        这里是客户端侧的兜底，比外层宽一点，用来区分"模型慢"与"模型连不上"；
        真正保证 C 不拖慢课堂的是外层那 20 秒。
      · `max_tokens=1200`：一次唤醒最多 2~3 条提案 × 250 字，1200 是紧的但够；
        给太多只会让模型把"理由"写成作文。
      · `temperature=0.7`：C 要的是**换一种讲法**，和课堂讲同一段话求稳的口径
        （`settings.temperature`，默认 0.3）不是一回事，所以不跟着它走。
    """

    def __init__(self, settings: Settings, *, provider: str = "", model: str = "",
                 timeout: float = 60.0, max_tokens: int = 1200,
                 temperature: float = 0.7) -> None:
        #: 独立实例（§3.2 D2）：这里必须 new 一个 LLM，不能接受从外面传进来的课堂实例。
        self.llm = LLM(_retarget(settings, provider, model))
        #: 原始设置留着只为诊断（"C 用的是哪个 provider"），不参与调用。
        self.settings = settings
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)

    async def propose(self, request: ProposalRequest) -> list[dict]:
        """一次唤醒：一条 user 消息进，一组提案出。

        只发一条 user 消息：`request.prompt` 里**已经**拼好了 `DIRECTING.system_prompt`
        （见 `producer.build_prompt` 的第一行）。再补一条 system 会把同一份人设发两遍 ——
        prefill 白花，且两份措辞不一致时模型会挑它更喜欢的那个。

        输出契约（含**路线**那一段形状与硬约束）整块在 `producer._PROMPT_TAIL` 里，
        与本类同一次发给模型：C 要能造 skip / revisit / finish，靠的是提示词把
        kind / fork_node / merge_node / steps / est_sec 的语义写清楚，而不是靠这里的代码。

        `think=False`：后台角色不该花预算思考（实测一次思考约 2900 字 ≈ 30 秒），
        C 的价值是"60 秒内多一种讲法"，不是"想得更深"。

        为什么解析失败要**脱掉 `json_mode` 再试一次**（降级兜底）
        ------------------------------------------------------------
        `response_format` 的支持度因服务商、因模型而异，而且有两种不同的坏法：
        一种是不支持（`LLM.chat` 内部已经退过一次），另一种是"支持但把模型带歪"。
        实测踩到的是后者（真实 key + 真实课件窗口）：`json_mode=True` 下模型把协议参数
        原样回显成 `{"type": "json_object", "content": []}`。那次的一半根因在提示词
        （当时还写着"输出数组"，与对象协议互相矛盾）—— 提示词已改成对象形状，
        见 `producer._PROMPT_TAIL` 的注释 —— 但"提示词再对，也有模型不听话"这件事
        不会消失，所以留这一层。

        结构上就把这件事钉死了：`_ask` 留在 `try` **之外**，`try` 里只有
        `parse_proposals` 一句 —— 于是这个 `except` 只可能看见解析失败，
        "只对解析失败重试"不依赖有没人把 except 的类型写对。它因此：
          · 正常路径仍然只调一次（成本不变，§12 阶段 3 数的是调用次数）；
          · 最多重试一次，不循环 —— 两次都不行就是这个模型这条路走不通；
          · 最坏形态是两次调用，而 `run_tick` 外面那层 `PROPOSER_TIMEOUT_S=20s` 的
            `wait_for` 仍然兜得住：超时就放弃、下个周期再来，学生无感（§11）；
          · 第二次仍解析不出来就照原样抛 `ProposalError`，由 `run_tick` 记成
            `bad_payload`。在这里吞掉等于把"模型坏了"改写成"这次不想造货" —— 前者要查，
            后者不用查，两者必须分得开。
        """
        raw = await self._ask(request, json_mode=True)
        try:
            return parse_proposals(raw)
        except ProposalError:
            print(f"[agent_c] ⚠ json_mode 下的输出解析失败（{raw[:60]!r}），"
                  "去掉 response_format 重试一次", flush=True)
        raw = await self._ask(request, json_mode=False)
        return parse_proposals(raw)

    async def _ask(self, request: ProposalRequest, *, json_mode: bool) -> str:
        """发一次模型调用，原样返回模型的话（解析不在这里做）。

        抽出来只为一件事：让降级那一次与正常那一次**除 `json_mode` 外逐字一致**。
        参数写两份的代价是"只在失败路径上犯"的错 —— 比如重试时忘了传 `timeout`，
        于是降级那一次会比正常那一次慢一倍，而所有离线用例都还是绿的。
        """
        return await self.llm.chat(
            [{"role": "user", "content": request.prompt}],
            json_mode=json_mode,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout,
            think=False,
        )


def build_proposer(settings: Settings) -> LlmProposer | None:
    """按开关造 proposer：关 → None，且**绝不构造 LLM**。

    为什么"关着时连 LLM 都不许构造"：`LLM.__init__` 会建 `AsyncOpenAI` 客户端
    （连接池、httpx client）。默认课堂里根本不认识这套东西（§11 最后一行），
    仅仅是"构造一下先放着"就已经改变了进程的资源形状 —— 默认路径必须一个字节都不动。

    其余参数一律从 `settings` 取（`CLASSROOM_AGENT_C_*`）：调用方只需要判断开关，
    不需要知道 C 用哪个服务商、多大预算 —— 那些是配置的事，不是接线的事。
    """
    if not getattr(settings, "agent_c_enabled", False):
        return None
    return LlmProposer(
        settings,
        provider=getattr(settings, "agent_c_provider", ""),
        model=getattr(settings, "agent_c_model", ""),
        timeout=getattr(settings, "agent_c_timeout", 60.0),
        max_tokens=getattr(settings, "agent_c_max_tokens", 1200),
    )
