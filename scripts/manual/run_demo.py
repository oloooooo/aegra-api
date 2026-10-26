import asyncio
from pprint import pprint

from langgraph_sdk import get_client


client = get_client(url="http://127.0.0.1:2026")
assistant_id = "supervisor"


async def main():
    thread = await client.threads.create()
    thread_id = thread["thread_id"]
    print("thread_id =", thread_id)

    first = await client.runs.wait(
        thread_id,
        assistant_id,
        input={
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "请把这个任务交给 researcher 异步处理："
                        "用后台任务总结 async subagent 的关键行为。"
                    ),
                }
            ]
        },
    )
    print("\\n=== first response ===")
    pprint(first)

    second = await client.runs.wait(
        thread_id,
        assistant_id,
        input={
            "messages": [
                {
                    "role": "user",
                    "content": "刚才那个后台任务现在进展如何？",
                }
            ]
        },
    )
    print("\\n=== second response ===")
    pprint(second)

    third = await client.runs.wait(
        thread_id,
        assistant_id,
        input={
            "messages": [
                {
                    "role": "user",
                    "content": "补充约束：完成时请把答案写成 3 条 bullet。",
                }
            ]
        },
    )
    print("\\n=== third response ===")
    pprint(third)


asyncio.run(main())