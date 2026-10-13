from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
import os
load_dotenv()
model = ChatOpenAI(
    model=os.environ.get("OPENAI_MODEL", ""),
    api_key=os.environ.get("OPENAI_API_KEY", ""),  # pyright: ignore[reportArgumentType]
    base_url=os.environ.get("OPENAI_BASE_URL", "")
)