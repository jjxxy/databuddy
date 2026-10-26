import fs from 'node:fs/promises';
import path from 'node:path';
import {Workbook,SpreadsheetFile} from '@oai/artifact-tool';

const output=path.resolve(process.argv[2]);
const inputs=JSON.parse(await fs.readFile(path.join(output,'input-data.json'),'utf8'));
const wb=Workbook.create();
const detail=wb.worksheets.add('销售明细');
const refund=wb.worksheets.add('退款记录');
const budget=wb.worksheets.add('月度预算');
function frame(sheet,lastCol,title,note,lastRow){
 sheet.showGridLines=false;
 sheet.getRange(`A1:${lastCol}1`).merge();sheet.getRange('A1').values=[[title]];
 sheet.getRange(`A2:${lastCol}2`).merge();sheet.getRange('A2').values=[[note]];
 sheet.getRange(`A1:${lastCol}${Math.max(20,lastRow)}`).format.font={name:'Arial',size:10,color:'#202020'};
 sheet.getRange('A1').format.font={name:'Arial',size:14,bold:true,color:'#202020'};
 sheet.getRange(`A3:${lastCol}4`).format={fill:'#283F54',font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},horizontalAlignment:'center',verticalAlignment:'center',wrapText:true};
 sheet.getRange(`A1:${lastCol}2`).format.rowHeight=28;
 sheet.getRange(`A3:${lastCol}4`).format.rowHeight=24;
 sheet.getRange(`A1:${lastCol}${lastRow}`).format.columnWidth=17;
 sheet.freezePanes.freezeRows(4);
}
frame(detail,'I','2026年10月销售订单底稿','合成验收资料。原始订单范围含9月及11月，金额为元；I列为数量×单价×折扣率的缓存公式。',inputs.detail.length+4);
for(const [col,label] of [['A','订单号'],['B','订单日期'],['C','办事处'],['D','客户'],['E','订单状态']]){detail.getRange(`${col}3:${col}4`).merge();detail.getRange(`${col}3`).values=[[label]];}
detail.getRange('F3:I3').merge();detail.getRange('F3').values=[['订单收入计算']];
detail.getRange('F4:I4').values=[['数量','单价（元）','折扣率','订单收入（元）']];
detail.getRange(`A5:H${inputs.detail.length+4}`).values=inputs.detail.map(row=>[row.order_id,new Date(row.date+'T00:00:00Z'),row.office,row.customer,row.status,row.quantity,row.unit_price,row.discount]);
// Integer cents/basis points avoid binary half-cent ambiguity in the cache engine.
detail.getRange('I5').formulas=[['=ROUND(F5*ROUND(G5*100,0)*ROUND(H5*10000,0)/10000,0)/100']];detail.getRange(`I5:I${inputs.detail.length+4}`).fillDown();
detail.getRange(`B5:B${inputs.detail.length+4}`).setNumberFormat('yyyy-mm-dd');
detail.getRange(`F5:F${inputs.detail.length+4}`).setNumberFormat('#,##0');
detail.getRange(`G5:G${inputs.detail.length+4}`).setNumberFormat('#,##0.00');
detail.getRange(`H5:H${inputs.detail.length+4}`).setNumberFormat('0.00%');
detail.getRange(`I5:I${inputs.detail.length+4}`).setNumberFormat('#,##0.00;(#,##0.00);"-"');
detail.getRange(`F5:H${inputs.detail.length+4}`).format.font.color='#0000FF';
frame(refund,'E','2026年10月退款底稿','合成验收资料。退款金额单位为万元，按关联订单号归属，存在同一订单多次退款。',inputs.refunds.length+4);
for(const [col,label] of [['A','退款单号'],['B','关联订单号'],['C','退款日期'],['D','退款状态'],['E','退款金额（万元）']]){refund.getRange(`${col}3:${col}4`).merge();refund.getRange(`${col}3`).values=[[label]];}
refund.getRange(`A5:E${inputs.refunds.length+4}`).values=inputs.refunds.map(row=>[row.refund_id,row.order_id,new Date(row.date+'T00:00:00Z'),row.status,row.amount_wan]);
refund.getRange(`C5:C${inputs.refunds.length+4}`).setNumberFormat('yyyy-mm-dd');
refund.getRange(`E5:E${inputs.refunds.length+4}`).setNumberFormat('#,##0.000000');refund.getRange(`E5:E${inputs.refunds.length+4}`).format.font.color='#0000FF';
frame(budget,'D','2026年10月净收入预算','合成验收资料。预算金额单位为万元，净收入预算与订单收入扣已完成退款同口径。',inputs.budgets.length+4);
for(const [col,label] of [['A','月份'],['B','办事处']]){budget.getRange(`${col}3:${col}4`).merge();budget.getRange(`${col}3`).values=[[label]];}
budget.getRange('C3:D3').merge();budget.getRange('C3').values=[['净收入预算']];budget.getRange('C4:D4').values=[['金额（万元）','预算口径']];
budget.getRange(`A5:D${inputs.budgets.length+4}`).values=inputs.budgets.map(row=>[new Date(row.month+'-01T00:00:00Z'),row.office,row.amount_wan,'订单收入减已完成退款']);
budget.getRange(`A5:A${inputs.budgets.length+4}`).setNumberFormat('yyyy-mm');budget.getRange(`C5:C${inputs.budgets.length+4}`).setNumberFormat('#,##0.000000');budget.getRange(`C5:C${inputs.budgets.length+4}`).format.font.color='#0000FF';budget.getRange('D5:D16').format.columnWidth=29;
wb.recalculate();
const inspection=await wb.inspect({kind:'region',sheetId:'销售明细',range:'E5:I9',maxChars:2500,tableMaxRows:5,tableMaxCols:5,include:'values,formulas'});
const errors=await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:20},maxChars:2500});
await fs.writeFile(path.join(output,'artifact-inspection.ndjson'),inspection.ndjson+'\n'+errors.ndjson);
for(const sheet of ['销售明细','退款记录','月度预算']){
 const image=await wb.render({sheetName:sheet,range:sheet==='销售明细'?'A1:I13':sheet==='退款记录'?'A1:E13':'A1:D13',scale:1.5,format:'png'});
 await fs.writeFile(path.join(output,`fixture-${sheet}.png`),new Uint8Array(await image.arrayBuffer()));
}
await (await SpreadsheetFile.exportXlsx(wb)).save(path.join(output,'complex_excel.xlsx'));
console.log(JSON.stringify({fixture:'complex_excel.xlsx',sheets:3,detail_rows:inputs.detail.length,refund_rows:inputs.refunds.length,budget_rows:inputs.budgets.length,formulas:inputs.detail.length}));
