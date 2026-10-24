"""Phone actions use host-owned device identity and fresh observed elements."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MobileObserve(BaseModel):
    model_config = ConfigDict(extra='forbid')


class MobileAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: str = Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_.:-]+$')
    action: Literal['home', 'back', 'open_app', 'open_url', 'tap', 'type', 'swipe']
    observation_id: str = Field(default='', max_length=64)
    ref: str = Field(default='', max_length=16)
    app: Literal['qq', 'browser'] | None = None
    url: str = Field(default='', max_length=2048)
    text: str = Field(default='', max_length=2000)
    direction: Literal['up', 'down', 'left', 'right'] | None = None
    submit: bool = False

    @model_validator(mode='after')
    def consistent(self):
        if self.action in {'tap', 'type', 'swipe'} and not self.observation_id:
            raise ValueError('fresh_observation_required')
        if self.action in {'tap', 'type'} and not self.ref:
            raise ValueError('observed_element_ref_required')
        if self.action == 'open_app' and self.app is None:
            raise ValueError('app_required')
        if self.action == 'open_url':
            from urllib.parse import urlsplit
            parsed = urlsplit(self.url)
            if parsed.scheme != 'https' or parsed.hostname not in {'bing.com','www.bing.com','cn.bing.com'} or parsed.username or parsed.password or parsed.port not in {None,443}:
                raise ValueError('only_public_https_bing_urls')
        if self.action == 'swipe' and self.direction is None:
            raise ValueError('direction_required')
        return self
