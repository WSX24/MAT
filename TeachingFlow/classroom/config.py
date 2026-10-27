"""配置：从环境变量 / .env 读取。"""

from __future__ import annotations

import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path


def ensure_utf8_console() -> None:
    """把 stdout/stderr 切到 UTF-8，让中文 Windows（GBK 控制台）打得出 emoji。

    为什么必须由代码兜底，不能只靠启动脚本：控制台编码是 GBK 时，
    `print("✅ …")` 直接抛 UnicodeEncodeError —— 而课堂的课程笔记、自检报告里
    全是 ✅❌⚠️，于是 `import web` 在导入期就炸，服务连 bind 的机会都没有
    （实测事故）；`python run.py test/check/audit` 的输出被管道/重定向接走时
    同样会崩，而那条路径上根本没有 powershell 脚本来设 PYTHONUTF8。

    用 errors="replace" 而不是默认的 strict：个别编码不了的字符宁可在输出里变成
    "?"，也不要再抛 —— 报告里少一个符号，远好过整个进程起不来。

    幂等、且**绝不抛**：流被换成没有 reconfigure 的对象（pytest 捕获、宿主重定向）
    时直接跳过；任何异常都吞掉，配置打印不该成为启动拦路虎。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - 换流/已关闭都可能抛，一律放过
            pass


# 在**模块导入期**就修好控制台编码：config 是所有模块的公共依赖（web.py / course.py /
# run.py 都先 import 它），这一步先执行，后面的 emoji 打印就天然安全。
ensure_utf8_console()

try:  # python-dotenv 可选
    from dotenv import load_dotenv

    # 先加载默认 .env（作为底稿）。
    # 再加载 DSH_ENV_FILE 指定的那份额外覆盖上去 —— 注意必须 override=True：
    # load_dotenv 默认**不覆盖已存在的变量**，所以如果反过来，
    # 普通 .env 里已有的键会把 --env 那份的值挡住（实测：换配置只生效了一半）。
    load_dotenv()
    _extra = os.environ.get("DSH_ENV_FILE", "").strip()
    if _extra:
        if not load_dotenv(_extra, override=True):
            print(f"[config] ⚠ 找不到配置文件：{_extra}")
except Exception as exc:  # pragma: no cover
    print(f"[config] ⚠ .env 读取失败：{exc}")

ROOT = Path(__file__).resolve().parent

#: 仓库根：classroom/ 的上两级（TeachingFlow/classroom → TeachingFlow → <仓库>）。
#: 模型 key 只该有**一份**出处 —— 仓库根的 PPT-Agent/.env（TeachingFlow 读的也是它），
#: 课堂再抄一份就等于"换个地方改配置、另一处不生效"。
REPO_ROOT = ROOT.parent.parent

#: 与 TeachingFlow/core/config.py 读的是同一个文件（MAT 里唯一必填的模型 key 在这里）
REPO_ENV_FILE = REPO_ROOT / "PPT-Agent" / ".env"


def repo_env_file() -> Path:
    """仓库根那份 .env 的路径。做成函数是为了让"它从哪儿推导出来"看得见、可被测试钉住。"""
    return REPO_ENV_FILE


def _env_is_set(key: str) -> bool:
    """环境变量"真的配了"才算配置：空串 / 纯空白一律视为没配。

    为什么单独判一次：dotenv 读 `DEEPSEEK_API_KEY=`（.env.example 里就长这样）
    写进来的是**空串**，`k in os.environ` 会为真 —— 于是仓库根那份真 key
    永远补不进来，深集成静默退化成"只解析"（实测踩过）。
    """
    return bool(os.environ.get(key, "").strip())


def load_repo_env(path: Path | None = None) -> Path | None:
    """把仓库根的 PPT-Agent/.env **补缺**地读进环境变量：已有的**非空**值一个都不覆盖。

    为什么自己解析、不用 load_dotenv(override=False)：规则必须与
    TeachingFlow/core/config.py::load_env_file 一模一样 —— 同一份文件被两边读，
    解析规则一分叉就会出现"命令行能跑、课堂跑不了"（带引号的值、空值算不算配置）。

    读不到就返回 None，绝不抛：这份文件没入库，缺了是**正常状态**；
    真读失败也只打一行告警 —— 配置读不全不该让课堂起不来。
    """
    p = Path(path) if path is not None else repo_env_file()
    try:
        if not p.is_file():
            return None
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            # 空串/纯空白当成"没配"（见 _env_is_set）：否则一份带 `KEY=` 的
            # .env 就能把仓库根那份真 key 挡在门外。
            if k and v and not _env_is_set(k):
                os.environ[k] = v
    except OSError as exc:
        print(f"[config] ⚠ 仓库根 .env 读取失败：{exc}")
        return None
    return p


# 位置有讲究：必须在 load_dotenv()（含 DSH_ENV_FILE 那份额外覆盖）之后。
# 先到先得 —— 本目录 .env 与 --env 指定的那份说话，这里只补它们没提到的缺口。
load_repo_env()

#: 思考模式的三种取值：
#:   off    —— 所有调用都不思考（**默认**）
#:   answer —— 只有深度答疑/重讲思考，短话术和分流不思考
#:   all    —— 全部思考（实验用；短话术也会变慢，会明显拖坏让位体验）
#:
#: 默认给 off 不是保守，是实测结论：本机 9B 思考型模型答一句要 **约 30 秒**
#: （思考约 2900 字），而段末让位的设计前提是让位延迟只有几秒 ——
#: 30 秒/答会让"学生提问→听到作答"变得不可接受。要开就用 answer。
THINKING_MODES = ("off", "answer", "all")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = _env(key).lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_bool_default(key: str, default: bool) -> bool:
    """认不出来的值回退**默认值**，而不是悄悄变成 False。

    为什么不复用 _env_bool()：它对非法值一律返回 False。对"默认开"的开关
    （小模型的启用 / 走小模型就是这种）来说，手抖写成 INTENT_SMALL_ENABLED=enable
    就等于把功能关掉了，而界面上只是"保存了好像没生效"，日志里一个字都没有。
    """
    v = _env(key).lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


@dataclass(frozen=True)
class ProviderProfile:
    """一个 OpenAI 兼容服务商的连接信息。"""

    name: str
    base_url: str
    api_key: str
    model: str
    #: 怎么向这家服务商表达"思考开/关"。目前实现了 `reasoning_effort`。
    thinking_style: str = "reasoning_effort"
    #: 每个请求都带上的额外请求头（目前只有 OpenCode Go 需要，见下面 opencode_go）
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    provider: str
    providers: dict[str, ProviderProfile]
    temperature: float
    timeout: float

    tts_enabled: bool
    tts_voice: str
    tts_rate: str

    probe_window: float
    max_clarify_asks: int
    max_rephrase: int
    unit_char_warn: int

    #: 思考模式：off（默认）| answer | all —— 见 THINKING_MODES 的说明
    llm_thinking: str = "off"
    #: 关闭思考时传给服务商的值（Ollama 实测只有 "none" 真的关得掉）
    thinking_off_value: str = "none"
    #: 开启思考时传给服务商的值；空字符串 = 不传（用服务商默认，Ollama 默认是开）
    thinking_on_value: str = ""
    #: 开思考时**额外**加给 max_tokens 的预算。思考会把预算吃光导致正文为空，
    #: 所以这笔预算不是优化，是必需品（实测一次思考约 2900 字）。
    think_token_budget: int = 2048

    host: str = "127.0.0.1"
    port: int = 8000

    #: 课件来源：优先 DECK_ID（已解析的 decks/<id>/），其次 PPTX_FILE（现场解析）。
    #: 两者都空 → 用原型自带的手写演示课件（旧行为不变）。
    deck_id: str = ""
    pptx_file: str = ""
    #: 只上前面 N 页（演示/联调用：42 页的完整课件一节课讲不完）
    pptx_max_slides: int = 0

    #: 千问语音转文字（Qwen ASR / DashScope 兼容接口）配置
    asr_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    asr_api_key: str = ""
    asr_model: str = "qwen3-asr-flash"

    #: 小模型（意图路由）的五项。base_url 空 = 自动复用当前服务商但强制关闭思考，
    #: 由 phrases.INTENT_ROUTER 在启动时从这里灌进去 —— 以前它只在运行时
    #: configure，进程一重启配置就没了（表现是"改好的小模型又变回主模型"）。
    #: 两个开关同样必须落盘：界面上关掉了、重启又自己打开，看着就是"保存没生效"，
    #: 实际是被默认值悄悄盖回去了。
    intent_small_base_url: str = ""
    intent_small_model: str = ""
    intent_small_api_key: str = ""
    intent_small_enabled: bool = True
    intent_small_use_small_llm: bool = True

    cache_dir: Path = field(default_factory=lambda: ROOT / ".cache")

    #: 双脑三体（J + C）的总开关，**默认关**：§11 最后一行是硬门槛 ——
    #: 现有课堂必须能在完全不认识这套东西的前提下照常跑完一整节课。
    #: 所以新机制默认关、显式开；关着时装配点除了这一行判断之外一行都不执行
    #: （方向不能反：默认开的话，"没接对"会变成"每个班的课都少讲一段"）。
    director_enabled: bool = False
    #: C 的唤醒周期（秒，§10 参数表给的是 60s）。装配层不启动后台循环时它不产生任何作用；
    #: 落在这里是为了让"多久醒一次"有唯一出处，而不是散在调用点的字面量里。
    agent_c_interval: float = 60.0

    #: C 的真实模型接入（§7.2 ④）。**默认关**，与 director_enabled 是两道独立的闸：
    #: 打开了决策层不等于要给 C 再接一个模型连接 —— 两者失败的爆炸半径完全不同
    #: （J 挂 = 课堂起不来；C 挂 = 少几条候选，§11）。关着时连 LLM 实例都不许构造
    #: （构造就会建 httpx 连接池），所以这个开关必须能单独关。
    agent_c_enabled: bool = False
    #: C 用哪个服务商 / 模型：**空 = 复用课堂那一份**（LLM_PROVIDER 与该 provider 的模型）。
    #: 只有在"课堂用小模型讲、C 用大模型造分支"这类分工下才需要单独配。
    agent_c_provider: str = ""
    agent_c_model: str = ""
    #: 单次模型调用的客户端侧超时。生产端外面还套着 run_tick 的 20s（§10），
    #: 这里比它宽一点，用来区分"模型慢"与"模型连不上"。
    agent_c_timeout: float = 60.0
    #: 单次生成的最大 token。一次唤醒最多 2~3 条提案 × 250 字，给多了只会换来作文。
    agent_c_max_tokens: int = 1200

    # -- 便捷访问 ---------------------------------------------------------
    @property
    def active(self) -> ProviderProfile:
        if self.provider not in self.providers:
            raise SystemExit(
                f"[config] 未知的 LLM_PROVIDER={self.provider!r}，"
                f"可选：{', '.join(self.providers)}"
            )
        return self.providers[self.provider]

    def think_for(self, role: str) -> bool:
        """这个角色要不要开思考。

        `role` 只有两类：`"answer"`（答疑/重讲，值得为答对而慢）
        和 `"phrase"`（过渡/重入/分流，学生正在等十几个字，绝不能慢）。

        实测本机 9B 模型思考一次约 30 秒（见 README 的实测表），
        所以默认 `off` —— 30 秒/答会摧毁段末让位。
        """
        if self.llm_thinking == "all":
            return True
        if self.llm_thinking == "answer":
            return role == "answer"
        return False

    def describe(self) -> str:
        a = self.active
        key = "已设置" if a.api_key else "未设置"
        return f"{a.name} · {a.model} · {a.base_url} · key:{key} · 思考:{self.llm_thinking}"


#: GUI 服务商面板允许写回的键：服务商 → (base_url, model, api_key)。
#: 只有这三个 + LLM_PROVIDER 会被面板碰，别的配置一律不代手改。
PROVIDER_ENV_KEYS: dict[str, tuple[str, str, str]] = {
    "ollama": ("OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_API_KEY"),
    "opencode": ("OPENCODE_BASE_URL", "OPENCODE_MODEL", "OPENCODE_API_KEY"),
    "opencode_go": ("OPENCODE_GO_BASE_URL", "OPENCODE_GO_MODEL", "OPENCODE_GO_API_KEY"),
    # 与 TeachingFlow 共用同一把 key：那三行就写在仓库根的 PPT-Agent/.env 里
    # （见 load_repo_env），所以面板对 deepseek **不强迫**再填一遍 key。
    "deepseek": ("DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY"),
}

#: 千问语音转文字（Qwen ASR）面板允许写回 `.env` 的键：(base_url, model, api_key)
ASR_ENV_KEYS: tuple[str, str, str] = (
    "QWEN_ASR_BASE_URL",
    "QWEN_ASR_MODEL",
    "QWEN_ASR_API_KEY",
)

#: 小模型（意图路由）面板允许写回 `.env` 的键：(base_url, model, api_key)
INTENT_SMALL_ENV_KEYS: tuple[str, str, str] = (
    "INTENT_SMALL_BASE_URL",
    "INTENT_SMALL_MODEL",
    "INTENT_SMALL_API_KEY",
)

#: 小模型的两个开关，跟上面三项分开：它们走三态布尔（见 _env_bool_default），
#: 而那三项是字符串。硬合成一个五元组会让每个使用点都要切片，反而更容易错位。
INTENT_SMALL_SWITCH_ENV_KEYS: tuple[str, str] = (
    "INTENT_SMALL_ENABLED",
    "INTENT_SMALL_USE_SMALL_LLM",
)


def env_file() -> Path:
    """GUI 该写回哪份配置：`--env` 指定的那份，否则默认 `.env`。

    跟 `load_dotenv()` 的优先级保持一致 —— 否则"界面里改了、启动时读的是另一份"，
    表现就是"保存了但没生效"，这类错最难查。
    """
    extra = os.environ.get("DSH_ENV_FILE", "").strip()
    return Path(extra) if extra else ROOT / ".env"


def save_env_values(path: Path, values: dict[str, str]) -> Path:
    """把少数几个键写回 `.env`：只改这几行，注释、顺序、其它配置一字不动。

    为什么不用 dotenv 整份重写：`.env` 里的注释是这个项目的说明书
    （端口为什么改过、思考型模型为什么必须关思考…），重写一遍就全丢了。
    键不存在时追加到文件末尾，并留一行来源标记。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 换行符必须用 newline="" 原样读写。踩过的坑：`write_text()` 在 Windows 上
    # 会把每个 `\n` 翻译成 CRLF，于是"只改了 1 行"的一次保存变成整份文件全变
    # （原文件是 LF，写回去是 CRLF）—— diff 一片红，真正改的那两行反而看不见了。
    raw = ""
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as f:
            raw = f.read()
    eol = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines() if raw else []
    pending = dict(values)
    out: list[str] = []
    for line in lines:
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = m.group(1) if m else ""
        if key in pending:
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    if pending:
        if out and out[-1].strip():
            out.append("")
        out.append("# --- 由 GUI 服务商面板写入 ---")
        out.extend(f"{k}={v}" for k, v in pending.items())
    # 原文件以换行结尾（绝大多数情况）就保持这个习惯；本来没有就不硬加
    text = eol.join(out)
    if not raw or raw.endswith(("\n", "\r\n")):
        text += eol
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


def apply_env_values(values: dict[str, str]) -> None:
    """把刚写盘的键同步进**当前进程**的环境变量。

    只写文件是不够的：`load_dotenv()` 默认**不覆盖**已存在的环境变量，所以
    进程启动时读进去的旧值会一直赢 —— 表现是"界面上点了保存，服务商没变"
    （实测踩到：.env 里已经写成 opencode_go，/api/state 还报 ollama）。
    """
    for k, v in values.items():
        os.environ[k] = v


def deepseek_profile() -> ProviderProfile:
    """DeepSeek —— MAT 的出片链路（TeachingFlow）用的就是这一份 key。

    thinking_style 默认 "off" 而不是 "reasoning_effort"，这是查过调用点之后的取舍：
    llm.py::_think_kwargs 只在 style == "reasoning_effort" 时才往请求体里塞
    reasoning_effort，而 DeepSeek 的 chat 接口不认这个参数（多塞一个字段换来的是
    400，而不是"思考更聪明"）。要试就设 DEEPSEEK_THINKING_STYLE=reasoning_effort。
    """
    return ProviderProfile(
        name="deepseek",
        base_url=_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=_env("DEEPSEEK_API_KEY"),
        model=_env("DEEPSEEK_MODEL", "deepseek-chat"),
        thinking_style=_env("DEEPSEEK_THINKING_STYLE", "off"),
    )


def load_settings(provider: str | None = None) -> Settings:
    """读当前配置。

    `provider` 只服务一种场景：临时**换一家**。给 bridge 用 —— 配置的那家连不上、
    而仓库根 .env 里有 DeepSeek key 时，备课临时走 deepseek，不必去改全局环境变量
    （os.environ 一改，正在上课的那个进程也跟着变）。不传就读 LLM_PROVIDER，
    既有调用点的语义一个字都没变。
    """
    chosen = (provider or _env("LLM_PROVIDER", "ollama") or "ollama").lower()
    providers = {
        "ollama": ProviderProfile(
            name="ollama",
            base_url=_env("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"),
            api_key=_env("OLLAMA_API_KEY", "ollama"),
            model=_env("OLLAMA_MODEL", "qwen2.5:7b"),
            thinking_style=_env("OLLAMA_THINKING_STYLE", "reasoning_effort"),
        ),
        "opencode": ProviderProfile(
            name="opencode",
            base_url=_env("OPENCODE_BASE_URL", "https://opencode.ai/zen/v1"),
            api_key=_env("OPENCODE_API_KEY"),
            model=_env("OPENCODE_MODEL", "claude-sonnet-4-5"),
            thinking_style=_env("OPENCODE_THINKING_STYLE", "reasoning_effort"),
        ),
        # OpenCode **Go 套餐**是订阅通道，端点和 Zen（按量付费）不是同一条：
        #   Zen  https://opencode.ai/zen/v1       按量计费
        #   Go   https://opencode.ai/zen/go/v1    订阅额度（$10/月）
        # 所以做成**独立服务商**，而不是在 opencode 上改 model —— 改 model 只会
        # 打到 Zen 那条更贵的通道上，额度一分都省不到。
        "opencode_go": ProviderProfile(
            name="opencode_go",
            base_url=_env("OPENCODE_GO_BASE_URL", "https://opencode.ai/zen/go/v1"),
            #: 没单独配 Go 的 key 就用 Zen 那把：两边是同一个账号体系
            api_key=_env("OPENCODE_GO_API_KEY") or _env("OPENCODE_API_KEY"),
            #: DeepSeek V4.1 在 Go 里的模型 id（旧写法 deepseek-v4-flash 仍被接受）
            model=_env("OPENCODE_GO_MODEL", "deepseek-v4.1-flash"),
            thinking_style=_env("OPENCODE_GO_THINKING_STYLE", "reasoning_effort"),
            headers={
                # Go 强制要求这个头（缺了直接 400 MissingSessionID，实测过）：
                # 它拿它做路由与 prompt-cache 亲和性，所以要求"会话内稳定、
                # 会话间不同"。默认每次进程启动生成一个，可用 env 钉死。
                "x-opencode-session": _env("OPENCODE_GO_SESSION") or uuid.uuid4().hex,
                # 官方要求第三方客户端自报家门（出兼容问题时他们靠这个定位）
                "User-Agent": _env("OPENCODE_GO_USER_AGENT",
                                   "classroom-prototype/0.1"),
            },
        ),
    }
    # deepseek 是**按需出现**的，不是无条件塞进 providers：课堂 GUI 把 providers
    # 逐行渲染成服务商面板，条目数是对外契约（tests/test_modelcfg.py 钉住
    # ollama/opencode/opencode_go 三行）。用哪个就出现哪个 —— 选中它时
    # Settings.active 解得开、面板不多一行；没选中时要用它，直接调 deepseek_profile()。
    if chosen == "deepseek":
        providers["deepseek"] = deepseek_profile()
    thinking = (_env("LLM_THINKING", "off") or "off").lower()
    if thinking not in THINKING_MODES:
        print(f"[config] ⚠ LLM_THINKING={thinking!r} 不认识，按 off 处理"
              f"（可选：{' / '.join(THINKING_MODES)}）")
        thinking = "off"
    return Settings(
        provider=chosen,
        providers=providers,
        temperature=_env_float("LLM_TEMPERATURE", 0.3),
        timeout=_env_float("LLM_TIMEOUT", 120.0),
        tts_enabled=_env_bool("TTS_ENABLED", True),
        tts_voice=_env("TTS_VOICE", "zh-CN-YunxiNeural"),
        tts_rate=_env("TTS_RATE", "+0%"),
        probe_window=_env_float("CLASSROOM_PROBE_WINDOW", 12.0),
        max_clarify_asks=_env_int("CLASSROOM_MAX_CLARIFY_ASKS", 2),
        max_rephrase=_env_int("CLASSROOM_MAX_REPHRASE", 4),
        unit_char_warn=_env_int("CLASSROOM_UNIT_CHAR_WARN", 250),
        llm_thinking=thinking,
        thinking_off_value=_env("LLM_THINKING_OFF_VALUE", "none"),
        thinking_on_value=_env("LLM_THINKING_ON_VALUE", ""),
        think_token_budget=_env_int("LLM_THINK_TOKEN_BUDGET", 2048),
        # 这两个原来漏了 —— 于是 .env 里的 HOST/PORT 从来没生效过（一直跑默认 8000），
        # 表现是"改了端口没反应"或"两个课堂抢同一个端口"。
        host=_env("HOST", "127.0.0.1") or "127.0.0.1",
        port=_env_int("PORT", 8000),
        deck_id=_env("DECK_ID"),
        pptx_file=_env("PPTX_FILE"),
        pptx_max_slides=_env_int("PPTX_MAX_SLIDES", 0),
        asr_base_url=_env("QWEN_ASR_BASE_URL",
                          "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        asr_api_key=_env("QWEN_ASR_API_KEY") or _env("DASHSCOPE_API_KEY"),
        asr_model=_env("QWEN_ASR_MODEL", "qwen3-asr-flash") or "qwen3-asr-flash",
        intent_small_base_url=_env("INTENT_SMALL_BASE_URL"),
        intent_small_model=_env("INTENT_SMALL_MODEL"),
        intent_small_api_key=_env("INTENT_SMALL_API_KEY"),
        intent_small_enabled=_env_bool_default("INTENT_SMALL_ENABLED", True),
        intent_small_use_small_llm=_env_bool_default("INTENT_SMALL_USE_SMALL_LLM", True),
        # 默认 False / 60s：不配置就等于这套东西不存在（见 Settings 里的说明）。
        director_enabled=_env_bool("CLASSROOM_DIRECTOR", False),
        agent_c_interval=_env_float("CLASSROOM_AGENT_C_INTERVAL", 60.0),
        # C 的真实模型接入：开着才构造 LLM（见 Settings 里的说明）。
        # 这里刻意不进 PROVIDER_ENV_KEYS：那张表是 GUI 面板写回用的，
        # 面板只该改"服务商的 base_url/model/api_key"，不该碰角色分工。
        agent_c_enabled=_env_bool("CLASSROOM_AGENT_C", False),
        agent_c_provider=_env("CLASSROOM_AGENT_C_PROVIDER"),
        agent_c_model=_env("CLASSROOM_AGENT_C_MODEL"),
        agent_c_timeout=_env_float("CLASSROOM_AGENT_C_TIMEOUT", 60.0),
        agent_c_max_tokens=_env_int("CLASSROOM_AGENT_C_MAX_TOKENS", 1200),
    )
