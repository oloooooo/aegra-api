from ast import List
from typing import Literal

from pydantic import BaseModel
Permission = Literal["read", "write"]

class MessagesBaseModel(BaseModel):
    message: str | None = None


class RegisterRequest(BaseModel):
    identity: str
    display_name: str 
    org_id: str | None = None
    email: str | None = None
    password: str


class RegisterResponse(MessagesBaseModel):
    user_identity: str


class LoginRequest(BaseModel):
    identity: str
    password: str


class TokenResponse(MessagesBaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class ListUserSpaceResponse(MessagesBaseModel):
    space: list[str]
    message: str | None = None


class CreateSpaceInput(BaseModel):
    space_name: str
    # 已废弃：目录由 space_name 派生（见 shared/knowledge/spec.py），此字段仅为老客户端兼容
    path: str = ""


class CreateSpaceResponse(MessagesBaseModel):
    space_name: str | None


class CreateUserSpaceResponse(CreateSpaceResponse):
    user_identity: str


class UploadFileResponse(BaseModel):
    """上传成功的会话文件（ADR-0013）：path 是 agent 眼里的路径。"""

    name: str
    path: str
    bytes: int
    uploaded_at: str
    ttl_minutes: int

class UploadTableResponse(BaseModel):
    """上传成功的大表（ADR-0015）：字节在对象存储，行在 `session_object`。"""

    name: str
    path: str
    space: str
    bytes: int
    sha256: str
    indexed: bool = False
