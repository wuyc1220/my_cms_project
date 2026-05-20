from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str
    captcha_id: str
    captcha_code: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str
    confirm_password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    display_name: str
    username: str
    force_change_password: bool = False


class CaptchaResponse(BaseModel):
    captcha_id: str


class UserInfo(BaseModel):
    id: int
    username: str
    display_name: str | None
    status: str
    role_codes: list[str] = []

    model_config = {"from_attributes": True}
