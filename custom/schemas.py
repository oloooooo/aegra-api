from numpy import str_
from pydantic import BaseModel

from custom.models import UserWorkspace

class MessagesBaseModel(BaseModel):
    message:str|None = None

class RegisterRequest(BaseModel):
    account: str
    password: str


class RegisterResponse(MessagesBaseModel):
    account: str


class LoginRequest(BaseModel):
    account: str
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
    path: str 

class CreateSpaceResponse(MessagesBaseModel):
    space_name: str | None

class CreateUserWorkspaceResponse(CreateSpaceResponse):
    user_account:str