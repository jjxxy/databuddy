"""Compare an Atlas full-result CSV or XLSX with an independent finance oracle.

Python 3 standard library only. Does not call Atlas, LLMs, execute Excel formulas,
or modify the export. CSV must be UTF-8; XLSX formulas require numeric caches.
"""
import argparse
import csv
from decimal import Decimal, InvalidOperation
import io
import json
from pathlib import Path, PurePosixPath
import sys
import xml.etree.ElementTree as ET
import zipfile

HERE = Path(__file__).resolve().parent
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
PKG_REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'
RATIOS = {'毛利率', '收入达成率'}
COUNTS = {'收入笔数', '成本笔数'}
KEYS = ('月份', '办事处')
SCENARIOS = ('baseline_sep','baseline_oct','baseline_both','corrected_oct','include_rd_sep')


def _part(base, target):
    if target.startswith('/'):
        return target.lstrip('/')
    parts = []
    for p in (PurePosixPath(base).parent / target).parts:
        if p == '..':
            if not parts:
                raise ValueError('XLSX内部路径越界')
            parts.pop()
        elif p != '.':
            parts.append(p)
    return '/'.join(parts)


def _xlsx_rows(path, sheet_name):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries)>10000 or sum(e.file_size for e in entries)>200*1024*1024:
            raise ValueError('XLSX超过校验器体积上限')
        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        rels = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
        targets = {r.get('Id'):_part('xl/workbook.xml',r.get('Target','')) for r in rels if r.tag==PKG_REL+'Relationship'}
        sheets = workbook.find(NS+'sheets')
        choices = [s for s in sheets if sheet_name is None or s.get('name')==sheet_name]
        if len(choices)!=1:
            raise ValueError('XLSX需用 --sheet 明确选择结果表页签；可选：'+', '.join(s.get('name','') for s in sheets))
        part = targets[choices[0].get(REL+'id')]
        strings=[]
        if 'xl/sharedStrings.xml' in archive.namelist():
            for item in ET.fromstring(archive.read('xl/sharedStrings.xml')):
                strings.append(''.join(n.text or '' for n in item.iter(NS+'t')))
        document=ET.fromstring(archive.read(part))
        rows=[]
        for row in document.iter(NS+'row'):
            values={}
            for cell in row:
                if cell.tag!=NS+'c':
                    continue
                coordinate=cell.get('r','')
                col=''.join(ch for ch in coordinate if ch.isalpha())
                index=0
                for ch in col:
                    index=index*26+ord(ch)-64
                if index<=0:
                    raise ValueError('XLSX单元格坐标无效')
                node=cell.find(NS+'v')
                kind=cell.get('t','n')
                if kind=='e':
                    raise ValueError('XLSX含Excel错误：'+coordinate)
                if cell.find(NS+'f') is not None and (node is None or node.text is None):
                    raise ValueError('XLSX公式缓存缺失，不能把公式文本当计算结果：'+coordinate)
                if kind=='s':
                    value=strings[int(node.text)] if node is not None else ''
                elif kind=='inlineStr':
                    value=''.join(n.text or '' for n in cell.iter(NS+'t'))
                else:
                    value=node.text if node is not None and node.text is not None else ''
                values[index-1]=value
            rows.append([values.get(i,'') for i in range(max(values,default=-1)+1)])
        return rows


def load_export(path, headers, sheet=None):
    path=Path(path)
    if path.suffix.lower()=='.csv':
        with path.open(encoding='utf-8-sig',newline='') as stream:
            rows=list(csv.reader(stream))
    elif path.suffix.lower()=='.xlsx':
        rows=_xlsx_rows(path,sheet)
    else:
        raise ValueError('只接受完整CSV或XLSX导出，不接受截图、Markdown或报告正文')
    # Find exactly one complete header row; notes above it are permitted.
    candidates=[(i,row) for i,row in enumerate(rows) if set(headers).issubset({str(v).strip() for v in row})]
    if len(candidates)!=1:
        raise ValueError('结果需且仅需一行完整规范表头；请按验收指令导出，不要只给预览或分页数据')
    top, header=candidates[0]
    normalized=[str(v).strip() for v in header]
    if any(normalized.count(h)!=1 for h in headers):
        raise ValueError('规范表头有重复列名')
    indexes={h:normalized.index(h) for h in headers}
    output=[]
    for line,row in enumerate(rows[top+1:],top+2):
        if not any(str(v).strip() for v in row):
            continue
        value={h:str(row[i]).strip() if i<len(row) else '' for h,i in indexes.items()}
        if not any(value.values()):
            continue
        if not all(value[k] for k in KEYS):
            raise ValueError('结果第'+str(line)+'行缺少月份/办事处，不能静默跳过')
        output.append(value)
    return output


def compare(actual, expected):
    differences=[]
    def indexed(rows,label):
        result={}
        for row in rows:
            key=tuple(str(row[k]) for k in KEYS)
            if key in result:
                differences.append(label+'存在重复业务键：'+repr(key))
            result[key]=row
        return result
    observed=indexed(actual,'实际结果')
    required=indexed(expected,'标准答案')
    for key in sorted(required.keys()-observed.keys()):
        differences.append('缺少结果行：'+repr(key))
    for key in sorted(observed.keys()-required.keys()):
        differences.append('多出结果行：'+repr(key))
    for key in sorted(observed.keys()&required.keys()):
        for column,gold in required[key].items():
            if column in KEYS:
                continue
            value=observed[key].get(column,'')
            try:
                number=Decimal(str(value))
                answer=Decimal(str(gold))
                if not number.is_finite():
                    raise InvalidOperation
            except InvalidOperation:
                differences.append(repr(key)+' '+column+'不是有限原子数值：'+repr(value))
                continue
            tolerance=Decimal('0.000000005') if column in RATIOS else Decimal('0') if column in COUNTS else Decimal('0.000001')
            if abs(number-answer)>tolerance:
                differences.append(repr(key)+' '+column+'应为'+str(gold)+'，实际'+str(value))
    return differences


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('export',type=Path)
    parser.add_argument('--scenario',required=True,choices=SCENARIOS)
    parser.add_argument('--sheet',help='XLSX含多个页签时必填，使用结果表页签名称')
    args=parser.parse_args(argv)
    try:
        expected=json.loads((HERE/'expected'/'gold.json').read_text(encoding='utf-8'))[args.scenario]
        actual=load_export(args.export,list(expected[0]),args.sheet)
        errors=compare(actual,expected)
    except (OSError,ValueError,KeyError,IndexError,zipfile.BadZipFile,ET.ParseError) as exc:
        print(json.dumps({'passed':False,'scenario':args.scenario,'error':str(exc)},ensure_ascii=False,indent=2))
        return 2
    print(json.dumps({'passed':not errors,'scenario':args.scenario,'rows':len(actual),'differences':errors,
                      'scope':'仅核对导出原子数值；报告来源、审批、重启、复用、权限等须按业务手册另验'},ensure_ascii=False,indent=2))
    return 1 if errors else 0


if __name__=='__main__':
    sys.exit(main())
