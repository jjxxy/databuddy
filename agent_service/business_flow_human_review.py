"""Declaration of a deliberately requested human review stop."""
from pydantic import Field,field_validator
from .models import StrictModel

class Config(StrictModel):
    instructions:str=Field(min_length=1,max_length=500,description='用户明确要求人工核对的内容，不得自行推测。')
    @field_validator('instructions')
    @classmethod
    def nonempty(cls,value):
        if not value.strip():raise ValueError('请明确人工核对的内容')
        return value.strip()

def validate(config):Config.model_validate(config)

def never_execute(context,config,inputs):
    raise ValueError('人工核对节点只能由明确的审批回执继续')
