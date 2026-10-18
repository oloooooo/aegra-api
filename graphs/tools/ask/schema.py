"""ask 域 schema：追问工具的输入（问题 + 2-4 个选项）与输出（用户选了啥）。"""

from typing import Annotated, Any, Optional

from pydantic import BaseModel, Field


class QuestionOption(BaseModel):
    """单个选项：label 给用户看，description 解释选它意味着什么。"""

    label: str = Field(description="选项显示文本，1-5 个词")
    description: Optional[str] = Field(default=None, description="选这个选项意味着什么，有什么取舍")


class AskQuestionArgs(BaseModel):
    """ask_user_question 的输入。"""

    question: str = Field(description="追问的具体问题，以问号结尾")
    options: list[QuestionOption] = Field(min_length=2, max_length=4, description="2-4 个选项，推荐的放第一个")
    header: Optional[str] = Field(default=None, description="极短标签（≤8 字），如“选文件”")


class AskUserQuestionOutput(BaseModel):
    """ask_user_question 的输出：tool 返回给模型的 JSON 即此模型的序列化。"""

    answer: str = Field(description="用户的答复：选中的选项 label，或自由输入的原文")
    extra: Optional[dict[str, Any]] = Field(default=None, description="预留：前端回传的附加字段，原样透传")
