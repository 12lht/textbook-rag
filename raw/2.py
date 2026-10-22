SYSTEM_PROMPT = """你是一位擅长用双关语表达的专家天气预报员。

你可以使用两个工具：

- get_weather_for_location：用于获取特定地点的天气
- get_user_location：用于获取用户的位置

如果用户询问天气，请确保你知道具体位置。如果从问题中可以判断他们指的是自己所在的位置，请使用 get_user_location 工具来查找他们的位置。"""

from dotenv import load_dotenv

load_dotenv()

from dataclasses import dataclass
from langchain.tools import tool, ToolRuntime

def look_log(message: str):
    """记录日志消息。"""
    print(f"[LOG] {message}")

@tool
def get_weather_for_location(city: str) -> str:
    """获取指定城市的天气。"""
    return f"{city}总是阳光明媚！"

@dataclass
class Context:
    """自定义运行时上下文模式。"""
    user_id: str

@tool
def get_current_time() -> str:
    """获取当前时间。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@tool
def get_user_location(runtime: ToolRuntime[Context]) -> str:
    """根据用户 ID 获取用户信息。"""
    user_id = runtime.context.user_id
    return "Florida" if user_id == "1" else "SF"

from langchain.chat_models import init_chat_model

model = init_chat_model(
    "deepseek:deepseek-flash",
    temperature=0.5,
    timeout=10,
    max_tokens=1000
)

from langgraph.checkpoint.memory import InMemorySaver

checkpointer = InMemorySaver()

from langchain.agents import create_agent

agent = create_agent(
    model=model,
    system_prompt=SYSTEM_PROMPT,
    tools=[get_user_location, get_weather_for_location, get_current_time],
    context_schema=Context,
    checkpointer=checkpointer
)

config = {"configurable": {"thread_id": "1"}}

def run_agent(user_input: str) -> None:
    print(f"\n>>> 用户：{user_input}")
    for update in agent.stream(
        {"messages": [{"role": "user", "content": user_input}]},
        config=config,
        context=Context(user_id="1"),
        stream_mode="updates"
    ):
        for node_name, state_update in update.items():
            print(f"\n--- {node_name} ---")
            for message in state_update.get("messages", []):
                message.pretty_print()

run_agent("外面的天气怎么样？")
run_agent("你能告诉我现在的时间吗？")
run_agent("谢谢！")