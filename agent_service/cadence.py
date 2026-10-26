"""Parse explicit supported schedules server-side; the model cannot invent a cron."""
import re

def parse(text,embedded=False):
    if re.search(r'[？?]|不要|不用|不需要|取消|暂停|能否|是否|如何|怎么|为什么|解释',text):return None
    clock=r'(?:上午|早上|早晨|下午|晚上)?\s*(\d{1,2})[点时:：](?:(\d{1,2})分?)?'
    action=r'\s*(?:自动)?(?:采集|更新|刷新|同步|运行|重跑)+(?:数据|一次|应用)?'
    matches=[]
    for kind,pattern in [('daily',r'每天'+clock+action),('weekdays',r'(?:每个工作日|工作日)'+clock+action),('weekly',r'每周([一二三四五六日天1-7])'+clock+action),('monthly',r'每月(\d{1,2})[日号]'+clock+action),('hourly',r'每(?:(\d{1,2})个?)?小时'+action)]:
        matches.extend((kind,m) for m in re.finditer(pattern,text))
    if not matches:return None
    if len(matches)!=1:raise ValueError('发现多个更新时间，请明确采用哪一个计划')
    kind,m=matches[0]
    if not embedded:
        rest=(text[:m.start()]+text[m.end():]).strip(' ，,。！!：:')
        if rest not in ('','请','帮我','设置','设置为','安排','这个应用','请帮我'):return None
    if kind=='hourly':
        hours=int(m[1] or 1)
        if hours not in (1,2,3,4,6,8,12):raise ValueError('按整点更新支持每1、2、3、4、6、8或12小时；其他间隔请指定每日或每周时间')
        cron='0 */'+str(hours)+' * * *';label='每'+str(hours)+'小时（按时区整点）'
    else:
        index=2 if kind in ('weekly','monthly') else 1
        hour=int(m[index]);minute=int(m[index+1] or 0)
        if not 0<=hour<=23 or not 0<=minute<=59:raise ValueError('更新时间应为0～23时、0～59分')
        if any(v in m[0] for v in ('上午','早上','早晨','下午','晚上')) and hour>12:raise ValueError('上午/下午请使用0～12点，或直接使用24小时制')
        if any(v in m[0] for v in ('下午','晚上')) and hour<12:hour+=12
        day='*';prefix='每天'
        if kind=='weekdays':day='1-5';prefix='工作日'
        if kind=='weekly':
            number='一二三四五六日'.find(m[1])+1 if m[1] in '一二三四五六日' else 7 if m[1]=='天' else int(m[1])
            day=str(number%7);prefix='每周'+m[1]
        cron=f'{minute} {hour} * * {day}';label=prefix+f' {hour:02d}:{minute:02d}'
        if kind=='monthly':
            date=int(m[1])
            if not 1<=date<=31:raise ValueError('每月日期应为1至31日')
            cron=f'{minute} {hour} {date} * *';label=f'每月{date}日 {hour:02d}:{minute:02d}'
            if date>28:label+='（当月无此日期时不运行）'
    return {'cron':cron,'label':label,'span':(m.start(),m.end())}


def remove(text):
    value=parse(text,embedded=True)
    return text[:value['span'][0]]+text[value['span'][1]:] if value else text
