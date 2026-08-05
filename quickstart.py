import os

from openai import OpenAI

import config

# 从集中配置读取（已显式加载 .env，系统环境变量优先）
client = OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL)

# 简单调用
response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[
        {"role": "system", "content": "你是一个有帮助的助手。"},
        {"role": "user", "content": "你好，请用一句话介绍LangChain是什么"},
    ],
)

print(response.choices[0].message.content)
