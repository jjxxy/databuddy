"""Agent-facing extension discovery. Configuration stays out of model tools."""
from . import extensions

DEFINITIONS={
 'discover_skills':(extensions.SkillQuery,False,'按业务目标查找当前用户安装的技能和管理员预置技能；目录不是执行权限，也不自动运行脚本'),
 'load_skill':(extensions.SkillTarget,False,'按需读取已启用技能的SKILL.md与文件目录，固定本轮包版本和摘要；技能指导只能使用现有授权工具'),
 'read_skill_file':(extensions.SkillFile,False,'分页读取本轮已加载技能的参考文件或脚本原文，核对固定摘要；读取不等于执行脚本'),
}


def execute(turn,name,args):
    if name=='discover_skills':return extensions.list_skills(args.get('query',''))
    if name=='load_skill':return extensions.load_skill(turn,args['skill_id'])
    if name=='read_skill_file':return extensions.read_skill_file(turn,extensions.SkillFile.model_validate(args))
    raise KeyError(name)
